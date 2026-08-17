"""Structured Discord UI for deleting unfinished Bot tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import discord


DeleteCallback = Callable[[int, str], Awaitable[str]]
PAGE_SIZE = 25


@dataclass(frozen=True)
class DeleteEntry:
    task_id: int
    task_number: int
    title: str
    status: str
    duration_minutes: int
    scheduled_start: datetime | None = None
    deadline: datetime | None = None
    recurrence_group: str | None = None
    is_fixed: bool = False
    segment_count: int = 0


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _status_label(entry: DeleteEntry) -> str:
    if entry.status == "pending":
        return "等待排程"
    if entry.recurrence_group:
        return "重複行程"
    if entry.is_fixed:
        return "固定行程"
    if entry.segment_count:
        return f"分割任務（{entry.segment_count} 段）"
    return "已排程"


def _when(entry: DeleteEntry) -> str:
    value = entry.scheduled_start or entry.deadline
    if not value:
        return "未指定日期"
    prefix = "排定" if entry.scheduled_start else "截止"
    return f"{prefix} {value:%Y-%m-%d %H:%M}"


def format_delete_summary(entry: DeleteEntry) -> str:
    scope = (
        "\n這是重複任務，請選擇只刪除此行程或刪除整個系列。"
        if entry.recurrence_group
        else "\n確認後會刪除資料庫紀錄及所有對應的 Google Calendar 行程。"
    )
    return (
        "🗑️ **確認刪除**\n"
        f"編號：{entry.task_number}\n"
        f"任務：{entry.title}\n"
        f"類型：{_status_label(entry)}\n"
        f"時間：{_when(entry)}\n"
        f"時長：{entry.duration_minutes} 分鐘"
        f"{scope}"
    )


class StructuredDeleteView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        entries: list[DeleteEntry],
        delete_callback: DeleteCallback,
    ) -> None:
        super().__init__(timeout=600)
        if not entries:
            raise ValueError("刪除選單至少需要一個任務。")
        self.user_id = user_id
        self.entries = entries
        self.delete_callback = delete_callback
        self.page_index = 0
        self.selected_id: int | None = None
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的刪除選單。", ephemeral=True)
        return False

    @property
    def page_count(self) -> int:
        return (len(self.entries) + PAGE_SIZE - 1) // PAGE_SIZE

    @property
    def page_entries(self) -> list[DeleteEntry]:
        start = self.page_index * PAGE_SIZE
        return self.entries[start : start + PAGE_SIZE]

    @property
    def selected_entry(self) -> DeleteEntry | None:
        return next(
            (entry for entry in self.entries if entry.task_id == self.selected_id),
            None,
        )

    @property
    def content(self) -> str:
        selected = self.selected_entry
        if selected:
            return format_delete_summary(selected)
        page = (
            f"（第 {self.page_index + 1}/{self.page_count} 頁）"
            if self.page_count > 1
            else ""
        )
        return (
            f"🗑️ **刪除任務**{page}\n"
            "請先從選單選擇一個等待排程或已排程的任務。\n"
            "已結束、等待評分及已有歷史評分的任務不會出現在這裡。"
        )

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(DeleteTaskSelect(self))
        self.add_item(DeletePreviousButton(self))
        self.add_item(DeleteNextButton(self))
        selected = self.selected_entry
        if selected and selected.recurrence_group:
            self.add_item(DeleteActionButton(self, "occurrence", "只刪除此行程", discord.ButtonStyle.danger))
            self.add_item(DeleteActionButton(self, "series", "刪除整個系列", discord.ButtonStyle.danger))
        elif selected:
            self.add_item(DeleteActionButton(self, "occurrence", "確認刪除", discord.ButtonStyle.danger))
        self.add_item(DeleteCancelButton(self))

    async def select(self, interaction: discord.Interaction, task_id: int) -> None:
        self.selected_id = task_id
        self.refresh_components()
        await interaction.response.edit_message(content=self.content, view=self)

    async def change_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page_index += delta
        self.selected_id = None
        self.refresh_components()
        await interaction.response.edit_message(content=self.content, view=self)

    async def delete(self, interaction: discord.Interaction, scope: str) -> None:
        selected = self.selected_entry
        if not selected:
            await interaction.response.send_message("請先選擇任務。", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            result = await self.delete_callback(selected.task_id, scope)
            self.stop()
            await interaction.edit_original_response(content=result, view=None)
        except Exception as exc:
            await interaction.edit_original_response(
                content=f"刪除失敗：{exc}\n\n{format_delete_summary(selected)}",
                view=self,
            )


class DeleteTaskSelect(discord.ui.Select):
    def __init__(self, parent: StructuredDeleteView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=_short(f"編號 {entry.task_number}｜{entry.title}", 100),
                value=str(entry.task_id),
                description=_short(
                    f"{_status_label(entry)}｜{_when(entry)}｜{entry.duration_minutes} 分鐘",
                    100,
                ),
                default=entry.task_id == parent.selected_id,
            )
            for entry in parent.page_entries
        ]
        super().__init__(placeholder="選擇要刪除的任務", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.select(interaction, int(self.values[0]))


class DeletePreviousButton(discord.ui.Button):
    def __init__(self, parent: StructuredDeleteView) -> None:
        self.parent_view = parent
        super().__init__(label="上一頁", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1, disabled=parent.page_index == 0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.change_page(interaction, -1)


class DeleteNextButton(discord.ui.Button):
    def __init__(self, parent: StructuredDeleteView) -> None:
        self.parent_view = parent
        super().__init__(label="下一頁", emoji="➡️", style=discord.ButtonStyle.secondary, row=1, disabled=parent.page_index >= parent.page_count - 1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.change_page(interaction, 1)


class DeleteActionButton(discord.ui.Button):
    def __init__(self, parent: StructuredDeleteView, scope: str, label: str, style: discord.ButtonStyle) -> None:
        self.parent_view = parent
        self.scope = scope
        super().__init__(label=label, style=style, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.delete(interaction, self.scope)


class DeleteCancelButton(discord.ui.Button):
    def __init__(self, parent: StructuredDeleteView) -> None:
        self.parent_view = parent
        super().__init__(label="取消", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.stop()
        await interaction.response.edit_message(content="已取消刪除。", view=None)
