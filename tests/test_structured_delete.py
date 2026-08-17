from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_structured_delete_tests.db"
)

from bot import _delete_entries
from database import Base, Feedback, SessionLocal, Task, TaskSegment, engine
from structured_delete import DeleteEntry, StructuredDeleteView, format_delete_summary


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False

    async def defer(self) -> None:
        self.deferred = True


class FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=123)
        self.response = FakeResponse()
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class DeleteViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_recurrence_selection_shows_occurrence_and_series_buttons(self):
        callback = AsyncMock(return_value="deleted")
        entry = DeleteEntry(
            task_id=8,
            task_number=3,
            title="閱讀",
            status="scheduled",
            duration_minutes=60,
            scheduled_start=datetime(2026, 8, 6, 9, 0),
            recurrence_group="daily-devotion",
        )
        view = StructuredDeleteView(
            user_id=123, entries=[entry], delete_callback=callback
        )
        view.selected_id = entry.task_id
        view.refresh_components()

        self.assertIn("重複任務", format_delete_summary(entry))
        self.assertEqual(
            [child.label for child in view.children[3:]],
            ["只刪除此行程", "刪除整個系列", "取消"],
        )

    async def test_delete_callback_removes_view_after_success(self):
        callback = AsyncMock(return_value="🗑️ 已刪除")
        entry = DeleteEntry(1, 1, "報告", "pending", 45)
        view = StructuredDeleteView(
            user_id=123, entries=[entry], delete_callback=callback
        )
        view.selected_id = 1
        interaction = FakeInteraction()

        await view.delete(interaction, "occurrence")

        callback.assert_awaited_once_with(1, "occurrence")
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits[-1], {"content": "🗑️ 已刪除", "view": None})


class DeleteQueryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_only_pending_and_scheduled_are_selectable_with_task_numbers(self):
        now = datetime.now().replace(microsecond=0)
        with SessionLocal() as session:
            waiting_feedback = Task(
                task_name="等待評分",
                estimated_minutes=30,
                status="feedback_requested",
                scheduled_start=now,
                discord_user_id="123",
            )
            pending = Task(
                task_name="待辦",
                estimated_minutes=45,
                status="pending",
                deadline=now + timedelta(days=1),
                discord_user_id="123",
            )
            split = Task(
                task_name="分割報告",
                estimated_minutes=60,
                status="scheduled",
                scheduled_start=now + timedelta(days=2),
                discord_user_id="123",
            )
            completed = Task(
                task_name="歷史",
                estimated_minutes=30,
                status="completed",
                discord_user_id="123",
            )
            other_user = Task(
                task_name="別人的任務",
                estimated_minutes=30,
                status="pending",
                discord_user_id="999",
            )
            session.add_all([waiting_feedback, pending, split, completed, other_user])
            session.flush()
            session.add(
                TaskSegment(
                    task_id=split.id,
                    segment_index=1,
                    scheduled_start=split.scheduled_start,
                    scheduled_end=split.scheduled_start + timedelta(minutes=30),
                    event_id="segment-delete-test",
                )
            )
            session.commit()

        entries = _delete_entries(123)

        self.assertEqual([entry.title for entry in entries], ["待辦", "分割報告"])
        self.assertEqual([entry.task_number for entry in entries], [2, 3])
        self.assertEqual(entries[1].segment_count, 1)


    def test_split_task_with_completed_segment_is_not_deletable(self):
        now = datetime.now().replace(microsecond=0)
        with SessionLocal() as session:
            task = Task(
                task_name="已開始的分割任務", estimated_minutes=90,
                status="scheduled", scheduled_start=now,
                scheduled_end=now + timedelta(hours=2), discord_user_id="123",
            )
            session.add(task)
            session.flush()
            segment = TaskSegment(
                task_id=task.id, segment_index=1, scheduled_start=now,
                scheduled_end=now + timedelta(minutes=45),
                event_id="rated-segment-delete-test", status="completed",
            )
            session.add(segment)
            session.flush()
            session.add(Feedback(
                task_id=task.id, segment_id=segment.id, actual_minutes=45,
                scheduled_start=segment.scheduled_start,
                scheduled_end=segment.scheduled_end,
                time_of_day=now.strftime("%H:%M"),
                efficiency_score=4, mental_score=4,
                rating_method="segment_two_stage",
            ))
            session.commit()

        self.assertEqual(_delete_entries(123), [])

if __name__ == "__main__":
    unittest.main()
