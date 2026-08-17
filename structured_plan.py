"""Discord range selector and pagination for scheduled Bot tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import discord


LoadCallback = Callable[[int], Awaitable[list["PlanEntry"]]]
WEEKDAYS = "一二三四五六日"
PAGE_BODY_LIMIT = 1700


@dataclass(frozen=True)
class PlanEntry:
    start: datetime
    end: datetime
    title: str
    locked: bool = False
    segment_index: int | None = None
    segment_count: int | None = None


def _entry_line(entry: PlanEntry) -> str:
    end_text = (
        entry.end.strftime("%H:%M")
        if entry.end.date() == entry.start.date()
        else entry.end.strftime("%m/%d %H:%M")
    )
    segment = (
        f"（{entry.segment_index}/{entry.segment_count}）"
        if entry.segment_index and entry.segment_count
        else ""
    )
    lock = " 🔒" if entry.locked else ""
    return f"{entry.start:%H:%M}–{end_text}｜{entry.title}{segment}{lock}"


def format_plan_pages(entries: list[PlanEntry], days: int) -> list[str]:
    label = "今天" if days == 1 else f"未來 {days} 天"
    if not entries:
        return [f"🗓️ {label}沒有 Bob 已排定的任務。"]

    lines: list[str] = []
    current_date = None
    for entry in sorted(entries, key=lambda item: (item.start, item.end, item.title)):
        if entry.start.date() != current_date:
            current_date = entry.start.date()
            lines.append(
                f"\n**{current_date:%m/%d}（週{WEEKDAYS[current_date.weekday()]}）**"
            )
        lines.append(_entry_line(entry))

    bodies: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line.lstrip("\n")
        if current and len(candidate) > PAGE_BODY_LIMIT:
            bodies.append(current)
            current = line.lstrip("\n")
        else:
            current = candidate
    if current:
        bodies.append(current)

    total = len(bodies)
    return [
        f"🗓️ **{label}規劃**"
        + (f"（第 {index}/{total} 頁）" if total > 1 else "")
        + "\n"
        + body
        for index, body in enumerate(bodies, start=1)
    ]


class StructuredPlanView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        load_callback: LoadCallback,
        initial_entries: list[PlanEntry],
        days: int = 7,
    ) -> None:
        super().__init__(timeout=600)
        self.user_id = user_id
        self.load_callback = load_callback
        self.days = days
        self.pages = format_plan_pages(initial_entries, days)
        self.page_index = 0
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的規劃檢視。", ephemeral=True)
        return False

    @property
    def content(self) -> str:
        return self.pages[self.page_index]

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(PlanRangeSelect(self))
        self.add_item(PlanPreviousButton(self))
        self.add_item(PlanNextButton(self))

    async def set_days(self, interaction: discord.Interaction, days: int) -> None:
        await interaction.response.defer()
        entries = await self.load_callback(days)
        self.days = days
        self.pages = format_plan_pages(entries, days)
        self.page_index = 0
        self.refresh_components()
        await interaction.edit_original_response(content=self.content, view=self)


class PlanRangeSelect(discord.ui.Select):
    VALUES = ((1, "今天"), (3, "未來 3 天"), (7, "未來 7 天"), (14, "未來 14 天"), (30, "未來 30 天"))

    def __init__(self, parent: StructuredPlanView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=label, value=str(days), default=parent.days == days)
            for days, label in self.VALUES
        ]
        super().__init__(placeholder="選擇規劃範圍", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.set_days(interaction, int(self.values[0]))


class PlanPreviousButton(discord.ui.Button):
    def __init__(self, parent: StructuredPlanView) -> None:
        self.parent_view = parent
        super().__init__(
            label="上一頁",
            emoji="⬅️",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=parent.page_index == 0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.page_index -= 1
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.content, view=self.parent_view
        )


class PlanNextButton(discord.ui.Button):
    def __init__(self, parent: StructuredPlanView) -> None:
        self.parent_view = parent
        super().__init__(
            label="下一頁",
            emoji="➡️",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=parent.page_index >= len(parent.pages) - 1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.page_index += 1
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.content, view=self.parent_view
        )
