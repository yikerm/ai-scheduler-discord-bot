from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_calendar_sync_tests.db"
)

from database import Base, SessionLocal, Task, TaskSegment, engine
from scheduler import sync_calendar_tasks


class FakeCalendar:
    calendar_id = "calendar@example.com"

    def list_event_changes(self, _sync_token=None):
        return (
            [
                {
                    "id": "regular-event",
                    "status": "confirmed",
                    "summary": "手動改名後的固定行程",
                    "start": {"dateTime": "2026-08-06T11:00:00+08:00"},
                    "end": {"dateTime": "2026-08-06T12:00:00+08:00"},
                },
                {
                    "id": "segment-2",
                    "status": "confirmed",
                    "summary": "分段報告（2/2）",
                    "start": {"dateTime": "2026-08-06T17:00:00+08:00"},
                    "end": {"dateTime": "2026-08-06T17:45:00+08:00"},
                },
            ],
            "next-token",
        )


class CalendarSyncTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_manual_moves_update_and_lock_regular_and_segmented_tasks(self):
        with SessionLocal() as session:
            regular = Task(
                task_name="固定行程",
                estimated_minutes=60,
                status="scheduled",
                scheduled_start=datetime(2026, 8, 6, 10, 0),
                scheduled_end=datetime(2026, 8, 6, 11, 0),
                event_id="regular-event",
                discord_user_id="123",
            )
            split = Task(
                task_name="分段報告",
                estimated_minutes=90,
                status="scheduled",
                scheduled_start=datetime(2026, 8, 6, 14, 0),
                scheduled_end=datetime(2026, 8, 6, 16, 45),
                discord_user_id="123",
                allow_split=True,
            )
            session.add_all([regular, split])
            session.flush()
            session.add_all(
                [
                    TaskSegment(
                        task_id=split.id,
                        segment_index=1,
                        scheduled_start=datetime(2026, 8, 6, 14, 0),
                        scheduled_end=datetime(2026, 8, 6, 14, 45),
                        event_id="segment-1",
                    ),
                    TaskSegment(
                        task_id=split.id,
                        segment_index=2,
                        scheduled_start=datetime(2026, 8, 6, 16, 0),
                        scheduled_end=datetime(2026, 8, 6, 16, 45),
                        event_id="segment-2",
                    ),
                ]
            )
            session.commit()
            regular_id = regular.id
            split_id = split.id

        with patch("scheduler.GoogleCalendarService", return_value=FakeCalendar()):
            result = sync_calendar_tasks()

        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["locked"], 2)
        self.assertEqual(result["renamed"], 1)
        with SessionLocal() as session:
            regular = session.get(Task, regular_id)
            split = session.get(Task, split_id)
            second = session.query(TaskSegment).filter_by(event_id="segment-2").one()
            self.assertEqual(regular.scheduled_start, datetime(2026, 8, 6, 11, 0))
            self.assertEqual(regular.task_name, "手動改名後的固定行程")
            self.assertTrue(regular.is_locked)
            self.assertEqual(second.scheduled_start, datetime(2026, 8, 6, 17, 0))
            self.assertTrue(second.is_locked)
            self.assertTrue(split.is_locked)
            self.assertEqual(split.scheduled_start, datetime(2026, 8, 6, 14, 0))
            self.assertEqual(split.scheduled_end, datetime(2026, 8, 6, 17, 45))


if __name__ == "__main__":
    unittest.main()
