from __future__ import annotations

import os
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo


os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_upgrade_tests.db"
)

from availability import (
    set_mode,
    set_work_schedule,
    work_window_for_day,
)
from bot import _confirmation_summary, _finalize_feedback, _finalize_incomplete, _save_efficiency_draft
from database import (
    Base,
    Feedback,
    SessionLocal,
    Task,
    WorkSchedule,
    engine,
)
from ml_engine import train_and_predict
from nlp_router import parse_bot_intent
from settings_parser import parse_schedule_settings


TZ = ZoneInfo("Asia/Taipei")


class NoopCalendar:
    def __init__(self, *args, **kwargs):
        pass

    def update_event_feedback(self, *args, **kwargs):
        return {}

    def update_event_incomplete(self, *args, **kwargs):
        return {}


class DatabaseCase(unittest.TestCase):
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


class FeedbackTests(DatabaseCase):
    def _feedback_task(self, name="回饋任務") -> int:
        with SessionLocal() as session:
            task = Task(
                task_name=name,
                estimated_minutes=60,
                status="feedback_requested",
                scheduled_start=datetime(2026, 8, 4, 10),
                scheduled_end=datetime(2026, 8, 4, 11),
            )
            session.add(task)
            session.commit()
            return task.id

    @patch("bot.GoogleCalendarService", NoopCalendar)
    def test_two_stage_feedback_stores_distinct_scores(self):
        task_id = self._feedback_task()
        _save_efficiency_draft(task_id, 123, 4)
        _finalize_feedback(task_id, 2)
        with SessionLocal() as session:
            feedback = session.scalar(
                session.query(Feedback).filter(Feedback.task_id == task_id).statement
            )
            self.assertEqual(feedback.efficiency_score, 4)
            self.assertEqual(feedback.mental_score, 2)
            self.assertEqual(feedback.rating_method, "two_stage")
            self.assertEqual(session.get(Task, task_id).status, "completed")

    @patch("bot.GoogleCalendarService", NoopCalendar)
    def test_incomplete_uses_zero_efficiency_and_low_mental(self):
        task_id = self._feedback_task()
        _finalize_incomplete(task_id, "臨時事件中斷")
        with SessionLocal() as session:
            feedback = session.scalar(
                session.query(Feedback).filter(Feedback.task_id == task_id).statement
            )
            self.assertEqual(feedback.efficiency_score, 0)
            self.assertEqual(feedback.mental_score, 1)
            self.assertEqual(feedback.completion_status, "incomplete")
            self.assertEqual(session.get(Task, task_id).status, "incomplete")


class SettingsAndModelTests(DatabaseCase):
    def test_four_schedule_profiles_and_future_mode(self):
        set_work_schedule("school", "weekday", 7 * 60, 23 * 60)
        set_work_schedule("school", "weekend", 9 * 60, 24 * 60)
        set_mode("school", date(2026, 9, 1))
        weekday_start, weekday_end, _ = work_window_for_day(date(2026, 9, 1))
        weekend_start, weekend_end, _ = work_window_for_day(date(2026, 9, 5))
        self.assertEqual((weekday_start.hour, weekday_end.hour), (7, 23))
        self.assertEqual(weekend_start.hour, 9)
        self.assertEqual(weekend_end.date(), date(2026, 9, 6))

    def test_chinese_effective_date_does_not_confuse_weekday_profile(self):
        now = datetime(2026, 7, 23, tzinfo=TZ)
        profile = parse_schedule_settings(
            "開學時星期一到星期五早上七點到晚上十一點，星期六日早上九點到凌晨十二點",
            now,
        )
        self.assertNotIn("effective_date", profile)
        self.assertEqual(len(profile["updates"]), 2)
        switch = parse_schedule_settings("從九月一日開始使用開學作息", now)
        self.assertEqual(switch["effective_date"], "2026-09-01")

    def test_deadline_phrase_remains_flexible_add(self):
        now = datetime(2026, 7, 23, 18, tzinfo=TZ)
        model = {
            "action": "add", "task_name": "完成報告", "duration_minutes": 45,
            "date": "2026-11-02", "time": "18:00", "end_time": None,
            "deadline": "2026-11-02T18:00:00", "frequency": None,
            "days": None, "query": None, "plan_days": None, "task_number": None,
            "allow_split": False, "priority": 0, "missing_fields": [],
            "ambiguities": [], "settings": {},
        }
        result = parse_bot_intent(
            "明天下午六點前完成報告，預計45分鐘",
            now=now,
            model_response=model,
        )
        self.assertEqual(result["action"], "add")
        self.assertEqual(result["deadline"], "2026-07-24T18:00:00")

    def test_schedule_confirmation_lists_both_profiles(self):
        summary = _confirmation_summary({
            "action": "schedule_settings",
            "settings": {
                "mode": "not_school",
                "updates": [
                    {"mode": "not_school", "day_type": "weekday", "start_time": "06:30", "end_time": "17:30"},
                    {"mode": "not_school", "day_type": "weekend", "start_time": "11:15", "end_time": "19:45"},
                ],
            },
        })
        self.assertIn("未開學・星期一到星期五：06:30–17:30", summary)
        self.assertIn("未開學・星期六日：11:15–19:45", summary)
        self.assertNotIn("頻率", summary)


    def test_ml_stays_disabled_before_twenty_new_feedback(self):
        with SessionLocal() as session:
            for index in range(19):
                task = Task(
                    task_name=f"任務 {index}", estimated_minutes=30,
                    status="completed",
                    scheduled_start=datetime(2026, 8, 1, 9),
                    scheduled_end=datetime(2026, 8, 1, 9, 30),
                )
                session.add(task)
                session.flush()
                session.add(
                    Feedback(
                        task_id=task.id,
                        scheduled_start=task.scheduled_start,
                        scheduled_end=task.scheduled_end,
                        time_of_day="09:00",
                        efficiency_score=4,
                        mental_score=3,
                        completion_status="completed",
                        rating_method="two_stage",
                    )
                )
            session.commit()
        predictor = train_and_predict()
        self.assertEqual(predictor.valid_feedback_count, 19)
        self.assertIsNone(predictor.efficiency_model)


if __name__ == "__main__":
    unittest.main()
