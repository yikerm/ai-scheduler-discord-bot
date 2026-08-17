"""Persistent Discord feedback UI for one segment of a split task."""

from __future__ import annotations

import asyncio

import discord

from database import SessionLocal, Task
from feedback_service import (
    finalize_completed,
    finalize_incomplete,
    save_efficiency_draft,
)
from planning_service import attempt_schedule_task
from temporal_parser import parse_duration


class SegmentScoreButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int, stage: str, score: int) -> None:
        super().__init__(
            label=str(score),
            style=discord.ButtonStyle.primary,
            custom_id=f"segment_feedback_{stage}_{task_id}_{segment_id}_{score}",
        )
        self.stage, self.score = stage, score

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.stage == "efficiency" and isinstance(
            self.view, SegmentEfficiencyFeedbackView
        ):
            await self.view.record(interaction, self.score)
        elif self.stage == "mental" and isinstance(
            self.view, SegmentMentalFeedbackView
        ):
            await self.view.record(interaction, self.score)


class _OwnedView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=None)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "這不是你的分段回饋邀請。", ephemeral=True
        )
        return False


class SegmentEfficiencyFeedbackView(_OwnedView):
    def __init__(
        self,
        user_id: int,
        task_id: int,
        segment_id: int,
        display_name: str,
    ) -> None:
        super().__init__(user_id)
        self.task_id, self.segment_id = task_id, segment_id
        self.display_name = display_name
        self.add_item(SegmentIncompleteButton(task_id, segment_id))
        for score in range(1, 6):
            self.add_item(
                SegmentScoreButton(task_id, segment_id, "efficiency", score)
            )

    async def record(self, interaction: discord.Interaction, score: int) -> None:
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                save_efficiency_draft,
                self.task_id,
                self.user_id,
                score,
                self.segment_id,
            )
            await interaction.edit_original_response(
                content=(
                    f"已記錄「{self.display_name}」效率 {score}/5。"
                    "這一段進行時的精神狀況如何？"
                ),
                view=SegmentMentalFeedbackView(
                    self.user_id,
                    self.task_id,
                    self.segment_id,
                    self.display_name,
                ),
            )
        except Exception as exc:
            await interaction.followup.send(f"評分失敗：{exc}", ephemeral=True)


class SegmentMentalFeedbackView(_OwnedView):
    def __init__(
        self,
        user_id: int,
        task_id: int,
        segment_id: int,
        display_name: str,
    ) -> None:
        super().__init__(user_id)
        self.task_id, self.segment_id = task_id, segment_id
        self.display_name = display_name
        for score in range(1, 6):
            self.add_item(SegmentScoreButton(task_id, segment_id, "mental", score))

    async def record(self, interaction: discord.Interaction, score: int) -> None:
        await interaction.response.defer()
        try:
            result = await asyncio.to_thread(
                finalize_completed, self.task_id, score, self.segment_id
            )
            await interaction.edit_original_response(
                content=(
                    f"已收到「{self.display_name}」的回饋："
                    f"效率 {result.efficiency}/5、精神 {score}/5。"
                ),
                view=None,
            )
        except Exception as exc:
            await interaction.followup.send(f"評分失敗：{exc}", ephemeral=True)


class SegmentIncompleteButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            label="未完成",
            style=discord.ButtonStyle.danger,
            custom_id=f"segment_feedback_incomplete_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentEfficiencyFeedbackView):
            return
        view = self.view
        await interaction.response.edit_message(
            content=(
                f"確認將「{view.display_name}」記為未完成嗎？"
                "只影響這一段；其他分段會照常保留。"
            ),
            view=SegmentIncompleteConfirmView(
                view.user_id, view.task_id, view.segment_id, view.display_name
            ),
        )


class SegmentIncompleteConfirmView(_OwnedView):
    def __init__(self, user_id: int, task_id: int, segment_id: int, display_name: str) -> None:
        super().__init__(user_id)
        self.task_id, self.segment_id = task_id, segment_id
        self.display_name = display_name
        self.add_item(
            SegmentConfirmIncompleteButton(task_id, segment_id)
        )
        self.add_item(SegmentReturnFeedbackButton(task_id, segment_id))


class SegmentConfirmIncompleteButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            label="確認未完成",
            style=discord.ButtonStyle.danger,
            custom_id=f"segment_confirm_incomplete_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentIncompleteConfirmView):
            return
        view = self.view
        await interaction.response.edit_message(
            content=f"請選擇「{view.display_name}」未完成的原因（可略過）：",
            view=SegmentIncompleteReasonView(
                view.user_id, view.task_id, view.segment_id, view.display_name
            ),
        )


class SegmentReturnFeedbackButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            label="返回評分",
            style=discord.ButtonStyle.secondary,
            custom_id=f"segment_return_feedback_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentIncompleteConfirmView):
            return
        view = self.view
        await interaction.response.edit_message(
            content=f"請評估「{view.display_name}」的完成效率：",
            view=SegmentEfficiencyFeedbackView(
                view.user_id, view.task_id, view.segment_id, view.display_name
            ),
        )


class SegmentIncompleteReasonSelect(discord.ui.Select):
    REASONS = (
        "精神或體力不足",
        "時間估計不足",
        "臨時事件中斷",
        "時段不適合",
        "任務太困難",
        "優先度改變",
        "其他",
        "略過",
    )

    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            placeholder="選擇原因",
            options=[discord.SelectOption(label=value, value=value) for value in self.REASONS],
            custom_id=f"segment_incomplete_reason_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentIncompleteReasonView):
            return
        view = self.view
        reason = None if self.values[0] == "略過" else self.values[0]
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                finalize_incomplete, view.task_id, reason, view.segment_id
            )
            await interaction.edit_original_response(
                content=(
                    f"已記錄「{view.display_name}」未完成。"
                    "其他分段不受影響；是否重新安排這一段的剩餘工作？"
                ),
                view=SegmentIncompleteRescheduleView(
                    view.user_id,
                    view.task_id,
                    view.segment_id,
                    view.display_name,
                ),
            )
        except Exception as exc:
            await interaction.followup.send(f"儲存失敗：{exc}", ephemeral=True)


class SegmentIncompleteReasonView(_OwnedView):
    def __init__(self, user_id: int, task_id: int, segment_id: int, display_name: str) -> None:
        super().__init__(user_id)
        self.task_id, self.segment_id = task_id, segment_id
        self.display_name = display_name
        self.add_item(SegmentIncompleteReasonSelect(task_id, segment_id))


class SegmentRemainingDurationModal(discord.ui.Modal, title="重新安排此分段"):
    duration = discord.ui.TextInput(
        label="剩餘工作需要多久？", placeholder="例如：60 分鐘"
    )

    def __init__(self, user_id: int, source_task_id: int, task_name: str) -> None:
        super().__init__()
        self.user_id, self.source_task_id = user_id, source_task_id
        self.task_name = task_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        minutes, _original = parse_duration(str(self.duration))
        if not minutes:
            await interaction.response.send_message(
                "無法辨識時長，請輸入例如「60 分鐘」。", ephemeral=True
            )
            return
        with SessionLocal() as session:
            task = Task(
                task_name=self.task_name,
                estimated_minutes=minutes,
                status="pending",
                discord_user_id=str(self.user_id),
                source_task_id=self.source_task_id,
            )
            session.add(task)
            session.commit()
            task_id = task.id
        result = await asyncio.to_thread(attempt_schedule_task, task_id)
        content = (
            f"✅ 已建立並排定剩餘工作：{result.start:%m/%d %H:%M}–{result.end:%H:%M}。"
            if result.scheduled
            else "✅ 已建立剩餘工作，但目前仍無法排入，已保留為 pending。"
        )
        await interaction.response.send_message(content, ephemeral=True)


class SegmentIncompleteRescheduleView(_OwnedView):
    def __init__(
        self,
        user_id: int,
        task_id: int,
        segment_id: int,
        display_name: str,
    ) -> None:
        super().__init__(user_id)
        self.task_id, self.segment_id = task_id, segment_id
        self.display_name = display_name
        self.add_item(SegmentRescheduleButton(task_id, segment_id))
        self.add_item(SegmentCloseIncompleteButton(task_id, segment_id))


class SegmentRescheduleButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            label="重新排程此段",
            style=discord.ButtonStyle.primary,
            custom_id=f"segment_reschedule_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentIncompleteRescheduleView):
            return
        await interaction.response.send_modal(
            SegmentRemainingDurationModal(
                self.view.user_id, self.view.task_id, self.view.display_name
            )
        )


class SegmentCloseIncompleteButton(discord.ui.Button):
    def __init__(self, task_id: int, segment_id: int) -> None:
        super().__init__(
            label="不再進行此段",
            style=discord.ButtonStyle.secondary,
            custom_id=f"segment_close_{task_id}_{segment_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, SegmentIncompleteRescheduleView):
            return
        await interaction.response.edit_message(
            content=f"已保留「{self.view.display_name}」的未完成歷史。",
            view=None,
        )
