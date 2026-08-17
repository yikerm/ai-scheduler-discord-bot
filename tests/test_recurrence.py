from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_upgrade_tests.db"
)

from database import (
    Base,
    RecurrenceRule,
    ScheduleModePeriod,
    SessionLocal,
    Task,
    WorkSchedule,
    engine,
)
from nlp_router import parse_bot_intent
from recurrence_service import (
    create_rule,
    delete_series,
    extend_rule,
    generate_occurrences,
    mark_awaiting_extension,
    rules_due_for_extension,
)


TZ = ZoneInfo("Asia/Taipei")


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.deleted = []
        self.next_id = 1

    def get_free_slots(self, target_day):
        return [
            {
                "start": datetime.combine(target_day, datetime.min.time(), tzinfo=TZ),
                "end": datetime.combine(
                    target_day + timedelta(days=1), datetime.min.time(), tzinfo=TZ
                ),
            }
        ]

    def create_event(self, title, start, end, description=None):
        event_id = f"event-{self.next_id}"
        self.next_id += 1
        self.events[event_id] = (title, start, end)
        return event_id

    def delete_event(self, event_id):
        self.deleted.append(event_id)
        self.events.pop(event_id, None)


class RecurrenceTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            for mode in ("school", "not_school"):
                for day_type in ("weekday", "weekend"):
                    session.add(
                        WorkSchedule(
                            mode=mode,
                            day_type=day_type,
                            start_minute=8 * 60,
                            end_minute=22 * 60,
                        )
                    )
            session.add(
                ScheduleModePeriod(mode="not_school", effective_from=date(1970, 1, 1))
            )
            session.commit()
        self.now = datetime(2026, 8, 4, 10, tzinfo=TZ)
        self.calendar = FakeCalendar()

    def _rule(self, *, final_end=None, allow_split=False, min_segment_minutes=30):
        return create_rule(
            group_id="daily-devotion",
            user_id=123,
            channel_id=456,
            task_name="閱讀",
            minutes=60,
            frequency="daily",
            fixed_time=None,
            allow_split=allow_split,
            min_segment_minutes=min_segment_minutes,
            priority=0,
            final_end_date=final_end,
            now=self.now,
        )

    def test_unspecified_end_creates_fourteen_days_and_prompts(self):
        rule = self._rule()
        result = generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        self.assertEqual(result.created, 14)
        with SessionLocal() as session:
            dates = list(
                session.scalars(
                    session.query(Task.recurrence_date)
                    .filter(Task.recurrence_group == rule.group_id)
                    .order_by(Task.recurrence_date)
                    .statement
                )
            )
        self.assertEqual((dates[0], dates[-1]), (date(2026, 8, 5), date(2026, 8, 18)))
        due = rules_due_for_extension(date(2026, 8, 19))
        self.assertEqual([value.id for value in due], [rule.id])

    def test_flexible_occurrences_inherit_series_split_minimum(self):
        rule = self._rule(allow_split=True, min_segment_minutes=45)
        generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        with SessionLocal() as session:
            current = session.get(RecurrenceRule, rule.id)
            tasks = list(
                session.query(Task).filter(Task.recurrence_group == rule.group_id)
            )
            self.assertEqual(current.min_segment_minutes, 45)
            self.assertTrue(tasks)
            self.assertTrue(all(task.allow_split for task in tasks))
            self.assertTrue(all(task.min_segment_minutes == 45 for task in tasks))


    def test_extend_adds_exactly_fourteen_days(self):
        rule = self._rule()
        generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        mark_awaiting_extension(rule.id, datetime(2026, 8, 19, 0, 10, tzinfo=TZ))
        result = extend_rule(
            rule.id,
            calendar=self.calendar,
            now=datetime(2026, 8, 19, 8, tzinfo=TZ),
        )
        self.assertEqual(result.created, 14)
        with SessionLocal() as session:
            current = session.get(RecurrenceRule, rule.id)
            count = session.query(Task).filter(Task.recurrence_group == rule.group_id).count()
            self.assertEqual(current.cycle_end, date(2026, 9, 1))
            self.assertEqual(count, 28)

    def test_late_extension_does_not_create_past_occurrences(self):
        rule = self._rule()
        generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        mark_awaiting_extension(rule.id, datetime(2026, 8, 19, 0, 10, tzinfo=TZ))
        result = extend_rule(
            rule.id, calendar=self.calendar,
            now=datetime(2026, 8, 25, 8, tzinfo=TZ),
        )
        self.assertEqual(result.created, 14)
        with SessionLocal() as session:
            current = session.get(RecurrenceRule, rule.id)
            dates = list(session.scalars(
                session.query(Task.recurrence_date)
                .filter(Task.recurrence_group == rule.group_id)
                .order_by(Task.recurrence_date).statement
            ))
        self.assertEqual(current.cycle_end, date(2026, 9, 7))
        self.assertNotIn(date(2026, 8, 19), dates)
        self.assertEqual(len(dates), 28)


    def test_fixed_end_only_generates_rolling_window(self):
        rule = self._rule(final_end=date(2026, 9, 30))
        first = generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        self.assertEqual(first.created, 14)
        second = generate_occurrences(
            rule.id,
            calendar=self.calendar,
            now=datetime(2026, 8, 10, 8, tzinfo=TZ),
        )
        self.assertEqual(second.created, 6)
        with SessionLocal() as session:
            count = session.query(Task).filter(Task.recurrence_group == rule.group_id).count()
            self.assertEqual(count, 20)

    def test_delete_series_preserves_completed_history(self):
        rule = self._rule()
        generate_occurrences(rule.id, calendar=self.calendar, now=self.now)
        with SessionLocal() as session:
            first = session.scalar(
                session.query(Task)
                .filter(Task.recurrence_group == rule.group_id)
                .order_by(Task.recurrence_date)
                .statement
            )
            first.status = "completed"
            session.commit()
            completed_id = first.id
        name, deleted = delete_series(
            rule.group_id, 123, calendar=self.calendar, now=self.now
        )
        self.assertEqual(name, "閱讀")
        self.assertEqual(deleted, 13)
        with SessionLocal() as session:
            self.assertIsNotNone(session.get(Task, completed_id))
            self.assertEqual(session.get(RecurrenceRule, rule.id).status, "ended")


if __name__ == "__main__":
    unittest.main()
