"""Two-page Discord wizard for the /repeat command."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import discord


TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
SubmitCallback = Callable[[dict], Awaitable[tuple[str, discord.ui.View | None]]]


def _minutes(value: str, *, label: str, maximum: int = 720) -> int:
    cleaned = value.strip().replace("分鐘", "").strip()
    if not cleaned.isdigit():
        raise ValueError(f"{label}請輸入整數分鐘數。")
    result = int(cleaned)
    if not 5 <= result <= maximum:
        raise ValueError(f"{label}必須介於 5 到 {maximum} 分鐘。")
    return result


def _date_label(target: date, today: date) -> str:
    offset = (target - today).days
    prefix = "今天" if offset == 0 else "明天" if offset == 1 else target.strftime("%m/%d")
    return f"{prefix}（{WEEKDAY_LABELS[target.weekday()]}）"


class StructuredRepeatView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        channel_id: int,
        title: str,
        submit_callback: SubmitCallback,
        now: datetime | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.user_id = user_id
        self.channel_id = channel_id
        self.title = " ".join(title.split())
        self.submit_callback = submit_callback
        self.now = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
        self.page = 1
        self.weekdays = set(WEEKDAY_CODES)
        self.mode = "flexible"
        self.duration_minutes: int | None = None
        self.start_date = self.now.date().fromordinal(self.now.date().toordinal() + 1)
        self.hour: int | None = None
        self.minute = 0
        self.allow_split = False
        self.min_segment_minutes = 30
        self.priority = 0
        self.expiry_mode = "ask"
        self.final_end_date: date | None = None
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的重複任務表單。", ephemeral=True)
        return False

    def refresh_components(self) -> None:
        self.clear_items()
        if self.page == 1:
            self.add_item(RepeatWeekdaySelect(self))
            self.add_item(RepeatModeSelect(self))
            self.add_item(RepeatDurationSelect(self))
            self.add_item(RepeatStartDateSelect(self))
            self.add_item(RepeatPriorityButton(self, 0, "一般"))
            self.add_item(RepeatPriorityButton(self, 1, "重要"))
            self.add_item(RepeatPriorityButton(self, 2, "緊急"))
            self.add_item(RepeatNextButton(self))
            self.add_item(RepeatCancelButton())
            return
        if self.mode == "fixed":
            self.add_item(RepeatHourSelect(self))
            self.add_item(RepeatMinuteSelect(self))
            self.add_item(RepeatExpirySelect(self, row=2))
            self.add_item(RepeatEndDateSelect(self, row=3))
        else:
            self.add_item(RepeatSplitSelect(self))
            self.add_item(RepeatExpirySelect(self, row=1))
            self.add_item(RepeatEndDateSelect(self, row=2))
        self.add_item(RepeatBackButton(self))
        self.add_item(RepeatCreateButton(self))
        self.add_item(RepeatCancelButton())

    def _weekday_text(self) -> str:
        indexes = [index for index, code in enumerate(WEEKDAY_CODES) if code in self.weekdays]
        if len(indexes) == 7:
            return "每天"
        if indexes == list(range(5)):
            return "星期一到星期五"
        return "、".join(WEEKDAY_LABELS[index] for index in indexes)

    def summary(self) -> str:
        mode_text = "彈性排程" if self.mode == "flexible" else "固定時間"
        duration_text = f"{self.duration_minutes} 分鐘" if self.duration_minutes else "尚未選擇"
        lines = [
            f"🔁 **新增重複任務（第 {self.page}/2 頁）**",
            f"系列：{self.title}",
            f"星期：{self._weekday_text()}",
            f"方式：{mode_text}",
            f"每次：{duration_text}",
            f"開始：{self.start_date:%Y-%m-%d}",
            f"優先度：{('一般', '重要', '緊急')[self.priority]}",
        ]
        if self.page == 2:
            if self.mode == "fixed":
                clock = f"{self.hour:02d}:{self.minute:02d}" if self.hour is not None else "尚未選擇"
                lines.append(f"固定開始時間：{clock}")
            else:
                split = (
                    f"可以，每段至少 {self.min_segment_minutes} 分鐘"
                    if self.allow_split
                    else "不可分割"
                )
                lines.append(f"分割：{split}")
            expiry = (
                "每 14 天詢問是否延長"
                if self.expiry_mode == "ask"
                else self.final_end_date.strftime("持續到 %Y-%m-%d")
                if self.final_end_date
                else "尚未選擇結束日期"
            )
            lines.append(f"到期：{expiry}")
        lines.append("")
        lines.append("請使用下方選單完成設定。")
        return "\n".join(lines)

    def build_payload(self) -> dict:
        if not self.title:
            raise ValueError("系列名稱不可空白。")
        if not self.weekdays:
            raise ValueError("請至少選擇一個星期。")
        if self.duration_minutes is None:
            raise ValueError("請先選擇每次時長。")
        if self.start_date < self.now.date():
            raise ValueError("開始日期不能早於今天。")
        fixed_time: str | None = None
        if self.mode == "fixed":
            if self.hour is None:
                raise ValueError("請先選擇固定開始小時。")
            fixed_time = f"{self.hour:02d}:{self.minute:02d}"
            start = datetime.combine(
                self.start_date, time(self.hour, self.minute), tzinfo=TIMEZONE
            )
            if start <= self.now:
                raise ValueError("今天的固定開始時間已經過了。")
        elif self.allow_split and self.duration_minutes < 2 * self.min_segment_minutes:
            raise ValueError(
                f"若允許分割，每次時長至少需 {2 * self.min_segment_minutes} 分鐘。"
            )
        if self.expiry_mode == "fixed_end":
            if self.final_end_date is None:
                raise ValueError("請選擇系列結束日期。")
            if self.final_end_date < self.start_date:
                raise ValueError("系列結束日期不能早於開始日期。")
        frequency = ",".join(
            code for code in WEEKDAY_CODES if code in self.weekdays
        )
        return {
            "action": "repeat",
            "task_name": self.title,
            "duration_minutes": self.duration_minutes,
            "frequency": frequency,
            "date": self.start_date.isoformat(),
            "time": fixed_time,
            "allow_split": self.allow_split if self.mode == "flexible" else False,
            "min_segment_minutes": (
                self.min_segment_minutes if self.mode == "flexible" else 30
            ),
            "recurrence_end_date": (
                self.final_end_date.isoformat()
                if self.expiry_mode == "fixed_end" and self.final_end_date
                else None
            ),
            "days": None,
            "priority": self.priority,
            "missing_fields": [],
            "ambiguities": [],
        }


class RepeatWeekdaySelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=label,
                value=code,
                default=code in parent.weekdays,
            )
            for code, label in zip(WEEKDAY_CODES, WEEKDAY_LABELS, strict=True)
        ]
        super().__init__(
            placeholder="1. 選擇發生星期（可複選）",
            options=options,
            min_values=1,
            max_values=7,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.weekdays = set(self.values)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatModeSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label="彈性時間，由排程器找空檔", value="flexible", default=parent.mode == "flexible"),
            discord.SelectOption(label="每天在固定時間開始", value="fixed", default=parent.mode == "fixed"),
        ]
        super().__init__(placeholder="2. 選擇時間方式", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.mode = self.values[0]
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatDurationSelect(discord.ui.Select):
    VALUES = (15, 30, 45, 60, 90, 120, 180, 240)

    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=f"{value} 分鐘", value=str(value), default=parent.duration_minutes == value)
            for value in self.VALUES
        ]
        options.append(discord.SelectOption(label="自訂分鐘數…", value="custom", emoji="✏️"))
        super().__init__(placeholder="3. 選擇每次時長", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(RepeatDurationModal(self.parent_view))
            return
        self.parent_view.duration_minutes = int(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatStartDateSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        today = parent.now.date()
        options = []
        for offset in range(22):
            target = date.fromordinal(today.toordinal() + offset)
            options.append(
                discord.SelectOption(
                    label=_date_label(target, today),
                    value=target.isoformat(),
                    default=parent.start_date == target,
                )
            )
        options.append(discord.SelectOption(label="自訂其他日期…", value="custom", emoji="✏️"))
        super().__init__(placeholder="4. 選擇系列開始日期", options=options, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(RepeatStartDateModal(self.parent_view))
            return
        self.parent_view.start_date = date.fromisoformat(value)
        if self.parent_view.final_end_date and self.parent_view.final_end_date < self.parent_view.start_date:
            self.parent_view.final_end_date = None
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatHourSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=f"{hour:02d} 時", value=str(hour), default=parent.hour == hour)
            for hour in range(24)
        ]
        super().__init__(placeholder="1. 選擇固定開始小時", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.hour = int(self.values[0])
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatMinuteSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=f"{minute:02d} 分", value=str(minute), default=parent.minute == minute)
            for minute in (0, 15, 30, 45)
        ]
        super().__init__(placeholder="2. 選擇固定開始分鐘", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.minute = int(self.values[0])
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatSplitSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        options = [discord.SelectOption(label="不可分割", value="no", default=not parent.allow_split)]
        options.extend(
            discord.SelectOption(
                label=f"可分割，每段至少 {value} 分鐘",
                value=str(value),
                default=parent.allow_split and parent.min_segment_minutes == value,
            )
            for value in (15, 30, 45, 60)
        )
        options.append(discord.SelectOption(label="可分割，自訂最短時間…", value="custom", emoji="✏️"))
        super().__init__(placeholder="1. 選擇分割方式", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(RepeatSplitModal(self.parent_view))
            return
        self.parent_view.allow_split = value != "no"
        if self.parent_view.allow_split:
            self.parent_view.min_segment_minutes = int(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatExpirySelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView, *, row: int) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label="每 14 天詢問是否延長", value="ask", default=parent.expiry_mode == "ask"),
            discord.SelectOption(label="持續到指定日期後結束", value="fixed_end", default=parent.expiry_mode == "fixed_end"),
        ]
        super().__init__(placeholder="選擇到期方式", options=options, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.expiry_mode = self.values[0]
        if self.parent_view.expiry_mode == "ask":
            self.parent_view.final_end_date = None
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatEndDateSelect(discord.ui.Select):
    def __init__(self, parent: StructuredRepeatView, *, row: int) -> None:
        self.parent_view = parent
        options = []
        for offset in range(22):
            target = date.fromordinal(parent.start_date.toordinal() + offset)
            options.append(
                discord.SelectOption(
                    label=f"{target:%Y-%m-%d}（{WEEKDAY_LABELS[target.weekday()]}）",
                    value=target.isoformat(),
                    default=parent.final_end_date == target,
                )
            )
        options.append(discord.SelectOption(label="自訂其他結束日期…", value="custom", emoji="✏️"))
        super().__init__(
            placeholder="選擇系列結束日期",
            options=options,
            row=row,
            disabled=parent.expiry_mode != "fixed_end",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(RepeatEndDateModal(self.parent_view))
            return
        self.parent_view.final_end_date = date.fromisoformat(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatNextButton(discord.ui.Button):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        super().__init__(label="下一步", emoji="➡️", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.parent_view.duration_minutes is None:
            await interaction.response.send_message("請先選擇每次時長。", ephemeral=True)
            return
        self.parent_view.page = 2
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatPriorityButton(discord.ui.Button):
    def __init__(self, parent: StructuredRepeatView, value: int, label: str) -> None:
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


class RepeatBackButton(discord.ui.Button):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        super().__init__(label="返回修改", emoji="⬅️", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.page = 1
        self.parent_view.refresh_components()
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatCreateButton(discord.ui.Button):
    def __init__(self, parent: StructuredRepeatView) -> None:
        self.parent_view = parent
        super().__init__(label="建立重複系列", emoji="✅", style=discord.ButtonStyle.success, row=4)

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
                content=f"無法建立重複系列：{exc}\n\n{self.parent_view.summary()}",
                view=self.parent_view,
            )
            return
        self.parent_view.stop()
        await interaction.edit_original_response(content=content, view=next_view)


class RepeatCancelButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="取消", emoji="✖️", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StructuredRepeatView)
        self.view.stop()
        await interaction.response.edit_message(content="已取消建立重複系列。", view=None)


class _RepeatTextModal(discord.ui.Modal):
    def __init__(self, parent: StructuredRepeatView, *, title: str, label: str, placeholder: str) -> None:
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
        await interaction.response.edit_message(content=self.parent_view.summary(), view=self.parent_view)


class RepeatDurationModal(_RepeatTextModal):
    def __init__(self, parent: StructuredRepeatView) -> None:
        super().__init__(parent, title="自訂每次時長", label="總分鐘數", placeholder="例如：180")

    async def apply(self, value: str) -> None:
        self.parent_view.duration_minutes = _minutes(value, label="每次時長")


class RepeatSplitModal(_RepeatTextModal):
    def __init__(self, parent: StructuredRepeatView) -> None:
        super().__init__(parent, title="自訂最短分割時間", label="每段至少幾分鐘", placeholder="例如：20")

    async def apply(self, value: str) -> None:
        self.parent_view.allow_split = True
        self.parent_view.min_segment_minutes = _minutes(value, label="最短分割時間")


class RepeatStartDateModal(_RepeatTextModal):
    def __init__(self, parent: StructuredRepeatView) -> None:
        super().__init__(parent, title="自訂系列開始日期", label="開始日期", placeholder="例如：2026-09-01")

    async def apply(self, value: str) -> None:
        try:
            target = date.fromisoformat(value.strip().replace("/", "-"))
        except ValueError as exc:
            raise ValueError("日期格式請使用 YYYY-MM-DD，例如 2026-09-01。") from exc
        if target < self.parent_view.now.date():
            raise ValueError("開始日期不能早於今天。")
        self.parent_view.start_date = target
        if self.parent_view.final_end_date and self.parent_view.final_end_date < target:
            self.parent_view.final_end_date = None


class RepeatEndDateModal(_RepeatTextModal):
    def __init__(self, parent: StructuredRepeatView) -> None:
        super().__init__(parent, title="自訂系列結束日期", label="結束日期", placeholder="例如：2026-12-31")

    async def apply(self, value: str) -> None:
        try:
            target = date.fromisoformat(value.strip().replace("/", "-"))
        except ValueError as exc:
            raise ValueError("日期格式請使用 YYYY-MM-DD，例如 2026-12-31。") from exc
        if target < self.parent_view.start_date:
            raise ValueError("結束日期不能早於開始日期。")
        self.parent_view.final_end_date = target
