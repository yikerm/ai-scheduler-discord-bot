from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_recurrence_semantics.db"
)

from database import (
    Base,
    ScheduleModePeriod,
    SessionLocal,
    WorkSchedule,
    engine,
)
from nlp_router import parse_bot_intent
from recurrence_service import create_rule, generate_occurrences


TZ = ZoneInfo("Asia/Taipei")


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.next_id = 1

    def get_free_slots(self, target_day):
        return [{
            "start": datetime.combine(target_day, datetime.min.time(), tzinfo=TZ),
            "end": datetime.combine(
                target_day + timedelta(days=1), datetime.min.time(), tzinfo=TZ
            ),
        }]

    def create_event(self, title, start, end, description=None):
        event_id = f"event-{self.next_id}"
        self.next_id += 1
        self.events[event_id] = (title, start, end)
        return event_id


class RecurrenceSemanticsTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            for mode in ("school", "not_school"):
                for day_type in ("weekday", "weekend"):
                    session.add(WorkSchedule(
                        mode=mode,
                        day_type=day_type,
                        start_minute=8 * 60,
                        end_minute=22 * 60,
                    ))
            session.add(ScheduleModePeriod(
                mode="not_school", effective_from=date(1970, 1, 1)
            ))
            session.commit()
        self.now = datetime(2026, 8, 4, 10, tzinfo=TZ)
        self.calendar = FakeCalendar()

    def test_fixed_time_is_exactly_fourteen_calendar_days(self):
        rule = create_rule(
            group_id="fixed-daily",
            user_id=123,
            channel_id=456,
            task_name="晚間複習",
            minutes=30,
            frequency="daily",
            fixed_time="20:00",
            allow_split=False,
            priority=0,
            final_end_date=None,
            now=self.now,
        )
        result = generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        self.assertEqual(result.created, 14)
        self.assertEqual(rule.cycle_end, date(2026, 8, 17))

    def test_duration_is_counted_from_actual_start(self):
        rule = create_rule(
            group_id="five-days",
            user_id=123,
            channel_id=456,
            task_name="復健",
            minutes=30,
            frequency="daily",
            fixed_time=None,
            allow_split=False,
            priority=0,
            final_end_date=None,
            duration_days=5,
            now=self.now,
        )
        result = generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        self.assertEqual(result.created, 5)
        self.assertEqual(rule.final_end_date, date(2026, 8, 9))

    def test_parser_separates_end_date_from_start_date(self):
        model = {
            "action": "repeat",
            "task_name": "閱讀",
            "duration_minutes": 60,
            "date": None,
            "time": None,
            "end_time": None,
            "deadline": None,
            "frequency": "daily",
            "recurrence_end_date": "2026-09-30",
            "days": None,
            "query": None,
            "plan_days": None,
            "task_number": None,
            "allow_split": False,
            "priority": 0,
            "missing_fields": [],
            "ambiguities": [],
            "settings": {},
        }
        parsed = parse_bot_intent(
            "每天閱讀一個小時，持續到九月三十日",
            now=self.now,
            model_response=model,
        )
        self.assertEqual(parsed["recurrence_end_date"], "2026-09-30")
        self.assertIsNone(parsed["date"])


    def test_weekday_range_is_not_a_series_end_date(self):
        model = {
            "action": "repeat", "task_name": "語言練習",
            "duration_minutes": 30, "date": "2026-08-10", "time": None,
            "end_time": None, "deadline": None, "frequency": "mon-fri",
            "recurrence_end_date": "2026-08-10", "days": None,
            "query": None, "plan_days": None, "task_number": None,
            "allow_split": False, "priority": 0, "missing_fields": [],
            "ambiguities": [], "settings": {},
        }
        parsed = parse_bot_intent(
            "星期一到星期五每天語言練習 30 分鐘",
            now=self.now, model_response=model,
        )
        self.assertEqual(parsed["frequency"], "mon-fri")
        self.assertIsNone(parsed["date"])
        self.assertIsNone(parsed["recurrence_end_date"])

    def test_repeat_can_have_both_start_and_end_dates(self):
        model = {
            "action": "repeat", "task_name": "閱讀",
            "duration_minutes": 60, "date": "2026-09-01", "time": None,
            "end_time": None, "deadline": None, "frequency": "daily",
            "recurrence_end_date": "2026-09-30", "days": None,
            "query": None, "plan_days": None, "task_number": None,
            "allow_split": False, "priority": 0, "missing_fields": [],
            "ambiguities": [], "settings": {},
        }
        parsed = parse_bot_intent(
            "從 9 月 1 日開始每天閱讀一小時，持續到 9 月 30 日",
            now=self.now, model_response=model,
        )
        self.assertEqual(parsed["date"], "2026-09-01")
        self.assertEqual(parsed["recurrence_end_date"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
