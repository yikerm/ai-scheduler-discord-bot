from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_personalized_splitting.db"
)

from database import Base, Feedback, SessionLocal, Task, engine
from ml_engine import train_and_predict
from rolling_optimizer import build_task_options
from task_features import infer_task_category, normalize_task_name


TZ = ZoneInfo("Asia/Taipei")


class PersonalizedSplittingTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_task_name_normalization_and_category(self):
        self.assertEqual(normalize_task_name("背單字（2/3）"), "背單字")
        self.assertEqual(infer_task_category("準備資格考試"), "reading")
        self.assertEqual(infer_task_category("英文寫作"), "writing")

    def test_history_can_make_split_plan_better_than_long_session(self):
        with SessionLocal() as session:
            for index in range(30):
                start = datetime(2026, 7, 1 + index % 7, 9 + index % 4)
                short = Task(
                    task_name="深度閱讀",
                    estimated_minutes=180,
                    status="completed",
                )
                session.add(short)
                session.flush()
                segment_index = index % 3 + 1
                session.add(
                    Feedback(
                        task_id=short.id,
                        actual_minutes=60,
                        task_name_key="深度閱讀",
                        task_category="reading",
                        parent_total_minutes=180,
                        segment_index=segment_index,
                        segment_count=3,
                        break_before_minutes=0 if segment_index == 1 else 30,
                        prior_task_minutes=(segment_index - 1) * 60,
                        scheduled_start=start,
                        scheduled_end=start + timedelta(minutes=60),
                        time_of_day=start.strftime("%H:%M"),
                        efficiency_score=5,
                        mental_score=5,
                        completion_status="completed",
                        rating_method="segment_two_stage",
                    )
                )

                long = Task(
                    task_name="深度閱讀",
                    estimated_minutes=180,
                    status="incomplete",
                )
                session.add(long)
                session.flush()
                session.add(
                    Feedback(
                        task_id=long.id,
                        actual_minutes=180,
                        task_name_key="深度閱讀",
                        task_category="reading",
                        parent_total_minutes=180,
                        segment_index=1,
                        segment_count=1,
                        break_before_minutes=0,
                        prior_task_minutes=0,
                        scheduled_start=start,
                        scheduled_end=start + timedelta(minutes=180),
                        time_of_day=start.strftime("%H:%M"),
                        efficiency_score=0,
                        mental_score=1,
                        completion_status="incomplete",
                        incomplete_reason="精神或體力不足",
                        rating_method="incomplete",
                    )
                )
            session.commit()

        predictor = train_and_predict()
        candidate = datetime(2026, 8, 19, 9, tzinfo=TZ)
        short_prediction = predictor.predict(
            60,
            candidate,
            task_name="深度閱讀",
            parent_total_minutes=180,
            segment_index=1,
            segment_count=3,
        )
        long_prediction = predictor.predict(
            180,
            candidate,
            task_name="深度閱讀",
            parent_total_minutes=180,
        )
        self.assertGreater(short_prediction.total, long_prediction.total + 3)
        self.assertGreater(
            short_prediction.completion_probability,
            long_prediction.completion_probability,
        )

        task = SimpleNamespace(
            id=999,
            task_name="深度閱讀",
            estimated_minutes=180,
            deadline=None,
            available_from=None,
            priority=0,
            allow_split=True,
            min_segment_minutes=60,
            status="pending",
        )
        slots = [
            {"start": candidate, "end": candidate + timedelta(minutes=60)},
            {
                "start": candidate + timedelta(hours=2),
                "end": candidate + timedelta(hours=3),
            },
            {
                "start": candidate + timedelta(hours=4),
                "end": candidate + timedelta(hours=5),
            },
            {
                "start": candidate + timedelta(hours=7),
                "end": candidate + timedelta(hours=10),
            },
        ]
        best = max(build_task_options(task, slots, predictor), key=lambda row: row.objective)
        self.assertEqual(len(best.intervals), 3)
        self.assertEqual(
            [int((end - start).total_seconds() // 60) for start, end in best.intervals],
            [60, 60, 60],
        )


if __name__ == "__main__":
    unittest.main()
