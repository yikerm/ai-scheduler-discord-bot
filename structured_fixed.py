"""Discord structured fixed-event creation wizard used by /fixed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord


TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAYS = "一二三四五六日"
SubmitCallback = Callable[[dict], Awaitable[tuple[str, discord.ui.View | None]]]


def _duration_minutes(value: str) -> int:
    cleaned = value.strip().replace("分鐘", "").strip()
    if not cleaned.isdigit():
        raise ValueError("時長請輸入整數分鐘數。")
    minutes = int(cleaned)
    if not 5 <= minutes <= 720:
        raise ValueError("固定行程時長必須介於 5 到 720 分鐘。")
    return minutes


class StructuredFixedView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        channel_id: int,
        title: str,
        submit_callback: SubmitCallback,
        now: datetime | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.user_id = user_id
        self.channel_id = channel_id
        self.title = " ".join(title.split())
        self.submit_callback = submit_callback
        self.now = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
        self.event_date: date | None = None
        self.hour: int | None = None
        self.minute = 0
        self.duration_minutes: int | None = None
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的固定行程表單。", ephemeral=True)
        return False

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(FixedDateSelect(self))
        self.add_item(FixedHourSelect(self))
        self.add_item(FixedMinuteSelect(self))
        self.add_item(FixedDurationSelect(self))
        self.add_item(CreateFixedButton(self))
        self.add_item(CancelFixedButton())

    def _start(self) -> datetime | None:
        if self.event_date is None or self.hour is None:
            return None
        return datetime.combine(
            self.event_date, time(self.hour, self.minute), tzinfo=TIMEZONE
        )

    def summary(self) -> str:
        if self.event_date is None:
            date_text = "尚未選擇"
        else:
            date_text = (
                f"{self.event_date:%Y-%m-%d}"
                f"（週{WEEKDAYS[self.event_date.weekday()]}）"
            )
        time_text = (
            f"{self.hour:02d}:{self.minute:02d}"
            if self.hour is not None
            else "尚未選擇"
        )
        duration_text = (
            f"{self.duration_minutes} 分鐘"
            if self.duration_minutes is not None
            else "尚未選擇"
        )
        end_text = "尚未能計算"
        start = self._start()
        if start and self.duration_minutes:
            end = start + timedelta(minutes=self.duration_minutes)
            end_text = end.strftime("%Y-%m-%d %H:%M")
        return (
            "📌 **新增固定行程**\n"
            f"事項：{self.title}\n"
            f"日期：{date_text}\n"
            f"開始時間：{time_text}\n"
            f"時長：{duration_text}\n"
            f"結束時間：{end_text}\n\n"
            "固定行程建立後會鎖定，排程器不會自動移動。"
        )

    def build_payload(self) -> dict:
        if not self.title:
            raise ValueError("行程名稱不可空白。")
        if self.event_date is None:
            raise ValueError("請先選擇日期。")
        if self.hour is None:
            raise ValueError("請先選擇開始小時。")
        if self.duration_minutes is None:
            raise ValueError("請先選擇行程時長。")
        start = self._start()
        assert start is not None
        if start <= self.now:
            raise ValueError("固定行程的開始時間必須晚於現在。")
        return {
            "action": "fixed",
            "task_name": self.title,
            "date": self.event_date.isoformat(),
            "time": f"{self.hour:02d}:{self.minute:02d}",
            "duration_minutes": self.duration_minutes,
            "missing_fields": [],
            "ambiguities": [],
        }


class FixedDateSelect(discord.ui.Select):
    def __init__(self, parent: StructuredFixedView) -> None:
        self.parent_view = parent
        today = parent.now.date()
        options: list[discord.SelectOption] = []
        for offset in range(22):
            target = date.fromordinal(today.toordinal() + offset)
            prefix = (
                "今天"
                if offset == 0
                else "明天"
                if offset == 1
                else target.strftime("%m/%d")
            )
            options.append(
                discord.SelectOption(
                    label=f"{prefix}（週{WEEKDAYS[target.weekday()]}）",
                    value=target.isoformat(),
                    default=parent.event_date == target,
                )
            )
        options.append(
            discord.SelectOption(label="自訂其他日期…", value="custom", emoji="✏️")
        )
        super().__init__(placeholder="1. 選擇日期", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(CustomFixedDateModal(self.parent_view))
            return
        self.parent_view.event_date = date.fromisoformat(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class FixedHourSelect(discord.ui.Select):
    def __init__(self, parent: StructuredFixedView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=f"{hour:02d} 時",
                value=str(hour),
                default=parent.hour == hour,
            )
            for hour in range(24)
        ]
        super().__init__(placeholder="2. 選擇開始小時", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.hour = int(self.values[0])
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class FixedMinuteSelect(discord.ui.Select):
    def __init__(self, parent: StructuredFixedView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=f"{minute:02d} 分",
                value=str(minute),
                default=parent.minute == minute,
            )
            for minute in range(0, 60, 5)
        ]
        super().__init__(placeholder="3. 選擇開始分鐘", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.minute = int(self.values[0])
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class FixedDurationSelect(discord.ui.Select):
    VALUES = (15, 30, 45, 60, 90, 120, 180, 240)

    def __init__(self, parent: StructuredFixedView) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=f"{value} 分鐘",
                value=str(value),
                default=parent.duration_minutes == value,
            )
            for value in self.VALUES
        ]
        options.append(
            discord.SelectOption(label="自訂分鐘數…", value="custom", emoji="✏️")
        )
        super().__init__(placeholder="4. 選擇行程時長", options=options, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "custom":
            await interaction.response.send_modal(
                CustomFixedDurationModal(self.parent_view)
            )
            return
        self.parent_view.duration_minutes = int(value)
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class CreateFixedButton(discord.ui.Button):
    def __init__(self, parent: StructuredFixedView) -> None:
        self.parent_view = parent
        super().__init__(
            label="建立固定行程",
            emoji="✅",
            style=discord.ButtonStyle.success,
            row=4,
        )

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
                content=f"無法建立固定行程：{exc}\n\n{self.parent_view.summary()}",
                view=self.parent_view,
            )
            return
        self.parent_view.stop()
        await interaction.edit_original_response(content=content, view=next_view)


class CancelFixedButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="取消", emoji="✖️", style=discord.ButtonStyle.secondary, row=4
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StructuredFixedView)
        self.view.stop()
        await interaction.response.edit_message(content="已取消建立固定行程。", view=None)


class CustomFixedDateModal(discord.ui.Modal, title="自訂固定行程日期"):
    value = discord.ui.TextInput(
        label="日期", placeholder="例如：2026-08-26", required=True
    )

    def __init__(self, parent: StructuredFixedView) -> None:
        super().__init__(timeout=300)
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            target = date.fromisoformat(str(self.value).strip().replace("/", "-"))
        except ValueError:
            await interaction.response.send_message(
                "日期格式請使用 YYYY-MM-DD，例如 2026-08-26。", ephemeral=True
            )
            return
        if target < self.parent_view.now.date():
            await interaction.response.send_message("日期不能早於今天。", ephemeral=True)
            return
        self.parent_view.event_date = target
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )


class CustomFixedDurationModal(discord.ui.Modal, title="自訂固定行程時長"):
    value = discord.ui.TextInput(
        label="總分鐘數", placeholder="例如：180", required=True
    )

    def __init__(self, parent: StructuredFixedView) -> None:
        super().__init__(timeout=300)
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.parent_view.duration_minutes = _duration_minutes(str(self.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.summary(), view=self.parent_view
        )
