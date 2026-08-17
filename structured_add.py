"""Discord structured task-creation wizard used by the /add command."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import discord

from availability import work_window_for_day


TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAYS = "一二三四五六日"
SubmitCallback = Callable[[dict], Awaitable[tuple[str, discord.ui.View | None]]]


def _positive_minutes(value: str, *, label: str) -> int:
    cleaned = value.strip().replace("分鐘", "").strip()
    if not cleaned.isdigit():
        raise ValueError(f"{label}請輸入整數分鐘數。")
    minutes = int(cleaned)
    if not 5 <= minutes <= 1440:
        raise ValueError(f"{label}必須介於 5 到 1440 分鐘。")
    return minutes


class StructuredAddView(discord.ui.View):
    """Stateful, ephemeral form for a flexible task."""

    def __init__(
        self,
        *,
        user_id: int,
        channel_id: int,
        task_name: str,
        submit_callback: SubmitCallback,
        now: datetime | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.user_id = user_id
        self.channel_id = channel_id
        self.task_name = " ".join(task_name.split())
        self.submit_callback = submit_callback
        self.now = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
        self.date_mode = "none"
        self.deadline_date: date | None = None
        self.duration_minutes: int | None = None
        self.deadline_time: time | None = None
        self.allow_split = False
        self.min_segment_minutes = 30
        self.priority = 0
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的新增任務表單。", ephemeral=True)
        return False

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(DeadlineDateSelect(self))
        self.add_item(DurationSelect(self))
        self.add_item(DeadlineTimeSelect(self))
        self.add_item(SplitSelect(self))
        self.add_item(AddPriorityButton(self, 0, "一般"))
        self.add_item(AddPriorityButton(self, 1, "重要"))
        self.add_item(AddPriorityButton(self, 2, "緊急"))
        self.add_item(CreateTaskButton(self))
        self.add_item(CancelTaskButton())

    def summary(self) -> str:
        if self.date_mode == "none":
            date_text = "未指定（在未來 7 天內找時間）"
            time_text = "不適用"
        else:
            assert self.deadline_date is not None
            date_text = f"{self.deadline_date:%Y-%m-%d}（週{WEEKDAYS[self.deadline_date.weekday()]}）"
            time_text = self.deadline_time.strftime("%H:%M") if self.deadline_time else "當天作息結束時間"
        duration_text = f"{self.duration_minutes} 分鐘" if self.duration_minutes else "尚未選擇"
        split_text = (
            f"可以，每段至少 {self.min_segment_minutes} 分鐘"
            if self.allow_split
            else "不可分割"
        )
        priority_text = ("一般", "重要", "緊急")[self.priority]
        return (
            "📝 **新增一般待辦**\n"
            f"任務：{self.task_name}\n"
            f"截止日期：{date_text}\n"
            f"預估時間：{duration_text}\n"
            f"截止時間：{time_text}\n"
            f"分割：{split_text}\n\n"
            f"優先度：{priority_text}\n\n"
            "選好後按「建立任務」。日期代表截止期限，機器人可以安排在現在到期限之間。"
        )

    def build_payload(self) -> dict:
        if not self.task_name:
            raise ValueError("任務名稱不可空白。")
        if self.duration_minutes is None:
            raise ValueError("請先選擇預估時間。")
        if self.allow_split and self.duration_minutes < 2 * self.min_segment_minutes:
            raise ValueError(
                f"若允許分割，總時間至少要是最短分段的兩倍（目前需至少 {2 * self.min_segment_minutes} 分鐘）。"
            )

        deadline: datetime | None = None
        if self.date_mode != "none":
            if self.deadline_date is None:
                raise ValueError("請選擇截止日期。")
            if self.deadline_time:
                deadline = datetime.combine(self.deadline_date, self.deadline_time)
            else:
                _start, deadline, _source = work_window_for_day(self.deadline_date)
            if deadline.replace(tzinfo=TIMEZONE) <= self.now:
                raise ValueError("截止時間必須晚於現在。")

        return {
            "action": "add",
            "task_name": self.task_name,
            "duration_minutes": self.duration_minutes,
            "date": None,
            "deadline": deadline.isoformat() if deadline else None,
            "allow_split": self.allow_split,
            "min_segment_minutes": self.min_segment_minutes,
            "priority": self.priority,
            "missing_fields": [],
            "ambiguities": [],
        }


class DeadlineDateSelect(discord.ui.Select):
    def __init__(self, parent: StructuredAddView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label="沒有截止日期",
                value="none",
                emoji="♾️",
                default=parent.date_mode == "none",
            )
        ]
        today = parent.now.date()
        for offset in range(22):
            target = date.fromordinal(today.toordinal() + offset)
            prefix = "今天" if offset == 0 else "明天" if offset == 1 else target.strftime("%m/%d")
            options.append(
                discord.SelectOption(
                    label=f"{prefix}（週{WEEKDAYS[target.weekday()]}）",
                    value=target.isoformat(),
                    default=parent.date_mode != "none" and parent.deadline_date == target,
                )
            )
        options.append(discord.SelectOption(label="自訂其他日期…", value="custom", emoji="✏️"))
        super().__init__(placeholder="1. 選擇截止日期", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(CustomDateModal(self.parent_view))
            return
        if value == "none":
            self.parent_view.date_mode = "none"
            self.parent_view.deadline_date = None
        else:
            self.parent_view.date_mode = "date"
            self.parent_view.deadline_date = date.fromisoformat(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class DurationSelect(discord.ui.Select):
    VALUES = (15, 30, 45, 60, 90, 120, 180, 240)

    def __init__(self, parent: StructuredAddView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=f"{value} 分鐘",
                value=str(value),
                default=parent.duration_minutes == value,
            )
            for value in self.VALUES
        ]
        options.append(discord.SelectOption(label="自訂分鐘數…", value="custom", emoji="✏️"))
        super().__init__(placeholder="2. 選擇預估時間", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(CustomDurationModal(self.parent_view))
            return
        self.parent_view.duration_minutes = int(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class DeadlineTimeSelect(discord.ui.Select):
    VALUES = ("12:00", "15:00", "18:00", "20:00", "22:00")

    def __init__(self, parent: StructuredAddView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label="使用當天作息結束時間",
                value="work_end",
                default=parent.deadline_time is None,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=value,
                value=value,
                default=parent.deadline_time is not None
                and parent.deadline_time.strftime("%H:%M") == value,
            )
            for value in self.VALUES
        )
        options.append(discord.SelectOption(label="自訂時間…", value="custom", emoji="✏️"))
        super().__init__(
            placeholder="3. 選擇截止時間",
            options=options,
            row=2,
            disabled=parent.date_mode == "none",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(CustomTimeModal(self.parent_view))
            return
        self.parent_view.deadline_time = None if value == "work_end" else time.fromisoformat(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class SplitSelect(discord.ui.Select):
    def __init__(self, parent: StructuredAddView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label="不可分割",
                value="no",
                default=not parent.allow_split,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=f"可分割，每段至少 {value} 分鐘",
                value=str(value),
                default=parent.allow_split and parent.min_segment_minutes == value,
            )
            for value in (15, 30, 45, 60)
        )
        options.append(discord.SelectOption(label="可分割，自訂最短時間…", value="custom", emoji="✏️"))
        super().__init__(placeholder="4. 選擇分割方式", options=options, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(CustomSplitModal(self.parent_view))
            return
        self.parent_view.allow_split = value != "no"
        if self.parent_view.allow_split:
            self.parent_view.min_segment_minutes = int(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class CreateTaskButton(discord.ui.Button):
    def __init__(self, parent: StructuredAddView) -> None:
        self.parent_view = parent
        super().__init__(label="建立任務", emoji="✅", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            payload = self.parent_view.build_payload()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            content, next_view = await self.parent_view.submit_callback(payload)
        except Exception as exc:
            await interaction.edit_original_response(
                content=f"無法新增任務：{exc}\n\n{self.parent_view.summary()}",
                view=self.parent_view,
            )
            return
        self.parent_view.stop()
        await interaction.edit_original_response(content=content, view=next_view)


class AddPriorityButton(discord.ui.Button):
    def __init__(self, parent: StructuredAddView, value: int, label: str) -> None:
        self.parent_view = parent
        self.value = value
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if parent.priority == value
                else discord.ButtonStyle.secondary
            ),
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.priority = self.value
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class CancelTaskButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="取消", emoji="✖️", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StructuredAddView)
        self.view.stop()
        await interaction.response.edit_message(content="已取消新增任務。", view=None)


class _SingleInputModal(discord.ui.Modal):
    def __init__(self, parent: StructuredAddView, *, title: str, label: str, placeholder: str) -> None:
        super().__init__(title=title, timeout=300)
        self.parent_view = parent
        self.input = discord.ui.TextInput(label=label, placeholder=placeholder, required=True)
        self.add_item(self.input)

    async def apply(self, value: str) -> None:
        raise NotImplementedError

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.apply(str(self.input.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class CustomDateModal(_SingleInputModal):
    def __init__(self, parent: StructuredAddView) -> None:
        super().__init__(parent, title="自訂截止日期", label="日期", placeholder="例如：2026-08-26")

    async def apply(self, value: str) -> None:
        try:
            target = date.fromisoformat(value.strip().replace("/", "-"))
        except ValueError as exc:
            raise ValueError("日期格式請使用 YYYY-MM-DD，例如 2026-08-26。") from exc
        if target < self.parent_view.now.date():
            raise ValueError("截止日期不能早於今天。")
        self.parent_view.date_mode = "date"
        self.parent_view.deadline_date = target


class CustomDurationModal(_SingleInputModal):
    def __init__(self, parent: StructuredAddView) -> None:
        super().__init__(parent, title="自訂預估時間", label="總分鐘數", placeholder="例如：180")

    async def apply(self, value: str) -> None:
        self.parent_view.duration_minutes = _positive_minutes(value, label="預估時間")


class CustomTimeModal(_SingleInputModal):
    def __init__(self, parent: StructuredAddView) -> None:
        super().__init__(parent, title="自訂截止時間", label="24 小時制時間", placeholder="例如：18:30")

    async def apply(self, value: str) -> None:
        try:
            self.parent_view.deadline_time = time.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("時間格式請使用 HH:MM，例如 18:30。") from exc


class CustomSplitModal(_SingleInputModal):
    def __init__(self, parent: StructuredAddView) -> None:
        super().__init__(parent, title="自訂最短分割時間", label="每段至少幾分鐘", placeholder="例如：20")

    async def apply(self, value: str) -> None:
        self.parent_view.allow_split = True
        self.parent_view.min_segment_minutes = _positive_minutes(value, label="最短分割時間")
