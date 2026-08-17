from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_feedback_calendar_time.db"
)

from database import Base, Feedback, SessionLocal, Task, engine
from feedback_service import finalize_completed, save_efficiency_draft


TZ = ZoneInfo("Asia/Taipei")


class FakeCalendar:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.updated = []

    def get_event(self, _event_id):
        return {
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": self.end.isoformat()},
        }

    def update_event_feedback(self, event_id, efficiency, mental):
        self.updated.append((event_id, efficiency, mental))


class FeedbackCalendarTimeTests(unittest.TestCase):
    def test_regular_task_uses_latest_calendar_time_when_rated(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        original = datetime(2026, 8, 13, 9, tzinfo=TZ)
        moved = datetime(2026, 8, 13, 14, tzinfo=TZ)
        with SessionLocal() as session:
            task = Task(
                task_name="一般任務",
                estimated_minutes=60,
                status="feedback_requested",
                discord_user_id="123",
                scheduled_start=original.replace(tzinfo=None),
                scheduled_end=(original + timedelta(hours=1)).replace(tzinfo=None),
                event_id="regular-event",
            )
            session.add(task)
            session.commit()
            task_id = task.id
        calendar = FakeCalendar(moved, moved + timedelta(minutes=30))
        save_efficiency_draft(task_id, 123, 5)
        finalize_completed(task_id, 4, calendar=calendar)
        with SessionLocal() as session:
            feedback = session.scalar(
                session.query(Feedback).filter(Feedback.task_id == task_id).statement
            )
            self.assertEqual(feedback.scheduled_start, moved.replace(tzinfo=None))
            self.assertEqual(feedback.actual_minutes, 30)
            self.assertIsNone(feedback.segment_id)
        self.assertEqual(calendar.updated, [("regular-event", 5, 4)])


if __name__ == "__main__":
    unittest.main()
