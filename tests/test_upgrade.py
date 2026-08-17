from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_upgrade_tests.db"
)

from availability import mode_for_day, work_window_for_day
from database import Base, SessionLocal, Task, TaskSegment, WorkSchedule, engine
from ml_engine import TaskScorePredictor
from nlp_router import merge_supplement, parse_bot_entry, parse_bot_intent
from planning_service import attempt_schedule_task, clear_outside_horizon_failures
from temporal_parser import parse_local_fields


TZ = ZoneInfo("Asia/Taipei")


def model_payload(**updates):
    payload = {
        "action": "fixed",
        "task_name": "meeting",
        "duration_minutes": 60,
        "date": "2026-11-02",
        "time": "14:00",
        "end_time": None,
        "deadline": None,
        "frequency": None,
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
    payload.update(updates)
    return payload


class TemporalParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 23, 18, 14, tzinfo=TZ)

    def test_next_thursday_is_next_calendar_week(self):
        fields = parse_local_fields(
            "下星期四下午兩點 meeting，一個小時", self.now
        )
        self.assertEqual(fields.date_value, date(2026, 7, 30))
        self.assertEqual(fields.clock.hour, 14)
        self.assertEqual(fields.duration_minutes, 60)

    def test_unqualified_same_weekday_after_time_uses_next_week(self):
        fields = parse_local_fields("星期四下午兩點 meeting 一個半小時", self.now)
        self.assertEqual(fields.date_value, date(2026, 7, 30))
        self.assertEqual(fields.duration_minutes, 90)

    def test_time_range_calculates_duration(self):
        fields = parse_local_fields("兩點到三點安排討論", self.now)
        self.assertEqual(fields.duration_minutes, 60)

    def test_local_date_overrides_wrong_model_date_and_flags_conflict(self):
        result = parse_bot_intent(
            "meeting在下星期四下午兩點，預計60分鐘",
            now=self.now,
            model_response=model_payload(),
        )
        self.assertEqual(result["date"], "2026-07-30")
        self.assertTrue(any("日期解析衝突" in value for value in result["ambiguities"]))

    def test_missing_duration_is_not_guessed(self):
        result = parse_bot_intent(
            "下星期四下午兩點安排 meeting",
            now=self.now,
            model_response=model_payload(duration_minutes=None),
        )
        self.assertIn("duration_minutes", result["missing_fields"])
        merged = merge_supplement(result, "一個半小時", now=self.now)
        self.assertEqual(merged["duration_minutes"], 90)
        self.assertNotIn("duration_minutes", merged["missing_fields"])


    def test_fixed_title_removes_conversational_fillers(self):
        result = parse_bot_intent(
            "請幫我安排下星期四下午兩點開始要meeting，這是固定行程，預計要一個小時",
            now=self.now,
            model_response=model_payload(task_name="要meeting 預計要"),
        )
        self.assertEqual(result["task_name"], "meeting")

    def test_safe_entry_returns_only_action_and_clean_title(self):
        result = parse_bot_entry(
            "請幫我安排下星期四下午兩點開始要meeting，這是固定行程，預計要一個小時",
            now=self.now,
            model_response={"action": "fixed", "task_name": "要meeting 預計要"},
        )
        self.assertEqual(result, {"action": "fixed", "task_name": "meeting"})

    def test_safe_entry_ignores_all_scheduling_fields(self):
        result = parse_bot_entry(
            "新增期末報告",
            now=self.now,
            model_response={
                "action": "add",
                "task_name": "期末報告",
                "date": "2099-01-01",
                "time": "02:00",
                "duration_minutes": 999,
            },
        )
        self.assertEqual(result, {"action": "add", "task_name": "期末報告"})


class FakeCalendar:
    def __init__(self, slots_by_day):
        self.slots_by_day = slots_by_day
        self.events = {}
        self.next_id = 1

    def get_free_slots(self, target_day):
        return [dict(value) for value in self.slots_by_day.get(target_day, [])]

    def create_event(self, title, start, end, description=None):
        event_id = f"event-{self.next_id}"
        self.next_id += 1
        self.events[event_id] = (title, start, end)
        return event_id

    def delete_event(self, event_id):
        self.events.pop(event_id, None)


class PlanningTests(unittest.TestCase):
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
                            start_minute=540,
                            end_minute=1320,
                        )
                    )
            session.commit()

    def _task(self, minutes, allow_split=False, min_segment_minutes=30):
        with SessionLocal() as session:
            task = Task(
                task_name="測試任務",
                estimated_minutes=minutes,
                status="pending",
                available_from=datetime(2026, 8, 5, 9, 0),
                deadline=datetime(2026, 8, 5, 18, 0),
                allow_split=allow_split,
                min_segment_minutes=min_segment_minutes,
            )
            session.add(task)
            session.commit()
            return task.id

    def test_contiguous_task_is_immediately_scheduled(self):
        day = date(2026, 8, 5)
        calendar = FakeCalendar(
            {day: [{"start": datetime(2026, 8, 5, 9, tzinfo=TZ), "end": datetime(2026, 8, 5, 12, tzinfo=TZ)}]}
        )
        task_id = self._task(60)
        result = attempt_schedule_task(
            task_id,
            calendar=calendar,
            predictor=TaskScorePredictor(None, None),
            now=datetime(2026, 8, 4, 18, tzinfo=TZ),
        )
        self.assertTrue(result.scheduled)
        with SessionLocal() as session:
            self.assertEqual(session.get(Task, task_id).status, "scheduled")

    def test_startup_cleanup_removes_existing_outside_horizon_failure(self):
        with SessionLocal() as session:
            task = Task(
                task_name="未來閱讀", estimated_minutes=60, status="pending",
                available_from=datetime(2026, 8, 12, 8, 0),
                deadline=datetime(2026, 8, 12, 22, 0),
                schedule_failure_reason="no_free_slot",
                schedule_failure_details="{}",
                schedule_failure_notified_reason="no_free_slot",
                schedule_failure_notified_at=datetime(2026, 8, 4, 18, 0),
            )
            session.add(task)
            session.commit()
            task_id = task.id
        cleared = clear_outside_horizon_failures(
            datetime(2026, 8, 4, 18, tzinfo=TZ)
        )
        self.assertEqual(cleared, 1)
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            self.assertIsNone(task.schedule_failure_reason)
            self.assertIsNone(task.schedule_failure_notified_reason)


    def test_outside_horizon_waits_without_failure_notification(self):
        with SessionLocal() as session:
            task = Task(
                task_name="未來閱讀", estimated_minutes=60, status="pending",
                available_from=datetime(2026, 8, 12, 8, 0),
                deadline=datetime(2026, 8, 12, 22, 0),
                schedule_failure_reason="no_free_slot",
                schedule_failure_details="{}",
                schedule_failure_notified_reason="no_free_slot",
                schedule_failure_notified_at=datetime(2026, 8, 4, 18, 0),
            )
            session.add(task)
            session.commit()
            task_id = task.id
        result = attempt_schedule_task(
            task_id, calendar=FakeCalendar({}),
            predictor=TaskScorePredictor(None, None),
            now=datetime(2026, 8, 4, 18, tzinfo=TZ),
        )
        self.assertEqual(result.failure_reason, "outside_horizon")
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            self.assertIsNone(task.schedule_failure_reason)
            self.assertIsNone(task.schedule_failure_notified_reason)


    def test_split_requires_explicit_permission(self):
        day = date(2026, 8, 5)
        slots = [
            {"start": datetime(2026, 8, 5, 9, tzinfo=TZ), "end": datetime(2026, 8, 5, 10, tzinfo=TZ)},
            {"start": datetime(2026, 8, 5, 14, tzinfo=TZ), "end": datetime(2026, 8, 5, 15, tzinfo=TZ)},
        ]
        calendar = FakeCalendar({day: slots})
        task_id = self._task(120, allow_split=True)
        result = attempt_schedule_task(
            task_id,
            calendar=calendar,
            predictor=TaskScorePredictor(None, None),
            now=datetime(2026, 8, 4, 18, tzinfo=TZ),
        )
        self.assertTrue(result.scheduled)
        self.assertEqual(result.segment_count, 2)
        with SessionLocal() as session:
            segments = list(
                session.query(TaskSegment).filter(TaskSegment.task_id == task_id)
            )
            self.assertEqual(len(segments), 2)


class MigrationTests(unittest.TestCase):
    def test_old_schema_is_upgraded_without_losing_history(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY, task_name VARCHAR NOT NULL,
                    estimated_minutes INTEGER NOT NULL, status VARCHAR NOT NULL,
                    deadline DATETIME, event_id VARCHAR
                );
                CREATE TABLE feedback (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,
                    scheduled_start DATETIME NOT NULL, scheduled_end DATETIME NOT NULL,
                    time_of_day VARCHAR NOT NULL, efficiency_score INTEGER NOT NULL,
                    mental_score INTEGER NOT NULL
                );
                INSERT INTO tasks VALUES (1, '歷史任務', 30, 'completed', NULL, 'event-old');
                INSERT INTO feedback VALUES (1, 1, '2026-07-01 10:00:00',
                    '2026-07-01 10:30:00', '10:00', 4, 4);
                """
            )
            connection.commit()
            connection.close()
            environment = dict(os.environ)
            environment["DATABASE_URL"] = f"sqlite:///{database_path}"
            subprocess.run(
                [sys.executable, "-c", "import database"],
                cwd=project,
                env=environment,
                check=True,
            )
            connection = sqlite3.connect(database_path)
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT rating_method FROM feedback WHERE id = 1"
                ).fetchone()[0],
                "legacy_overall",
            )


if __name__ == "__main__":
    unittest.main()
