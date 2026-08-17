from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_segment_feedback_tests.db"
)

from database import (
    Base,
    Feedback,
    SessionLocal,
    Task,
    TaskSegment,
    engine,
)
from feedback_service import (
    finalize_completed,
    finalize_incomplete,
    save_efficiency_draft,
)
from scheduler import _find_finished_segments, _mark_segment_feedback_requested


TZ = ZoneInfo("Asia/Taipei")


class FakeCalendar:
    def __init__(self, events):
        self.events = events
        self.completed = []
        self.incomplete = []

    def get_event(self, event_id):
        start, end = self.events[event_id]
        return {
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    def update_event_feedback(self, event_id, efficiency, mental):
        self.completed.append((event_id, efficiency, mental))

    def update_event_incomplete(self, event_id, reason):
        self.incomplete.append((event_id, reason))


class SegmentFeedbackTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def _split_task(self, *, past=False):
        base = (
            datetime.now(TZ).replace(second=0, microsecond=0) - timedelta(hours=3)
            if past
            else datetime(2026, 8, 13, 9, tzinfo=TZ)
        )
        with SessionLocal() as session:
            task = Task(
                task_name="分段報告",
                estimated_minutes=120,
                status="scheduled",
                discord_user_id="123",
                allow_split=True,
                scheduled_start=base.replace(tzinfo=None),
                scheduled_end=(base + timedelta(hours=4)).replace(tzinfo=None),
            )
            session.add(task)
            session.flush()
            first = TaskSegment(
                task_id=task.id,
                segment_index=1,
                scheduled_start=base.replace(tzinfo=None),
                scheduled_end=(base + timedelta(hours=1)).replace(tzinfo=None),
                event_id="segment-1",
            )
            second = TaskSegment(
                task_id=task.id,
                segment_index=2,
                scheduled_start=(base + timedelta(hours=3)).replace(tzinfo=None),
                scheduled_end=(base + timedelta(hours=4)).replace(tzinfo=None),
                event_id="segment-2",
            )
            session.add_all([first, second])
            session.commit()
            return task.id, first.id, second.id, base

    def test_each_segment_uses_latest_calendar_time_and_updates_parent(self):
        task_id, first_id, second_id, base = self._split_task()
        moved_start = base + timedelta(hours=1)
        moved_end = moved_start + timedelta(minutes=45)
        calendar = FakeCalendar(
            {
                "segment-1": (moved_start, moved_end),
                "segment-2": (base + timedelta(hours=3), base + timedelta(hours=4)),
            }
        )
        _mark_segment_feedback_requested(first_id)
        save_efficiency_draft(task_id, 123, 4, first_id)
        result = finalize_completed(task_id, 3, first_id, calendar=calendar)

        with SessionLocal() as session:
            feedback = session.scalar(
                session.query(Feedback)
                .filter(Feedback.segment_id == first_id)
                .statement
            )
            task = session.get(Task, task_id)
            first = session.get(TaskSegment, first_id)
            second = session.get(TaskSegment, second_id)
            self.assertEqual(feedback.actual_minutes, 45)
            self.assertEqual(feedback.scheduled_start, moved_start.replace(tzinfo=None))
            self.assertEqual(feedback.rating_method, "segment_two_stage")
            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "scheduled")
            self.assertEqual(task.status, "scheduled")
        self.assertEqual(result.task_status, "scheduled")
        self.assertEqual(calendar.completed, [("segment-1", 4, 3)])

    def test_incomplete_segment_keeps_other_segment_and_marks_partial_at_end(self):
        task_id, first_id, second_id, base = self._split_task()
        calendar = FakeCalendar(
            {
                "segment-1": (base, base + timedelta(hours=1)),
                "segment-2": (base + timedelta(hours=3), base + timedelta(hours=4)),
            }
        )
        _mark_segment_feedback_requested(first_id)
        save_efficiency_draft(task_id, 123, 4, first_id)
        finalize_completed(task_id, 4, first_id, calendar=calendar)
        _mark_segment_feedback_requested(second_id)
        result = finalize_incomplete(
            task_id, "臨時事件中斷", second_id, calendar=calendar
        )

        with SessionLocal() as session:
            task = session.get(Task, task_id)
            first = session.get(TaskSegment, first_id)
            second = session.get(TaskSegment, second_id)
            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "incomplete")
            self.assertEqual(task.status, "partially_completed")
            self.assertEqual(
                session.query(Feedback)
                .filter(Feedback.task_id == task_id)
                .count(),
                2,
            )
        self.assertEqual(result.task_status, "partially_completed")
        self.assertEqual(calendar.incomplete, [("segment-2", "臨時事件中斷")])

    def test_finished_segment_is_requested_without_waiting_for_last_segment(self):
        task_id, first_id, _second_id, _base = self._split_task(past=True)
        rows = _find_finished_segments()
        first = next(row for row in rows if row[1] == first_id)
        self.assertEqual(first[:4], (task_id, first_id, 1, 2))


if __name__ == "__main__":
    unittest.main()
