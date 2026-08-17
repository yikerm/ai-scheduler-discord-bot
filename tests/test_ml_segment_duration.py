from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_ml_segment_duration.db"
)

import ml_engine
from database import Base, Feedback, SessionLocal, Task, engine


class MLSegmentDurationTests(unittest.TestCase):
    def test_training_uses_actual_segment_minutes(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            for index in range(20):
                task = Task(
                    task_name=f"分段任務 {index}",
                    estimated_minutes=180,
                    status="completed",
                )
                session.add(task)
                session.flush()
                session.add(
                    Feedback(
                        task_id=task.id,
                        actual_minutes=30,
                        scheduled_start=datetime(2026, 8, 13, 9),
                        scheduled_end=datetime(2026, 8, 13, 9, 30),
                        time_of_day="09:00",
                        efficiency_score=4,
                        mental_score=4,
                        completion_status="completed",
                        rating_method="segment_two_stage",
                    )
                )
            session.commit()
        original = ml_engine._features
        with patch("ml_engine._features", wraps=original) as feature_spy:
            predictor = ml_engine.train_and_predict()
        self.assertEqual(predictor.valid_feedback_count, 20)
        self.assertTrue(
            any(call.args[0] == 30 for call in feature_spy.call_args_list)
        )
        self.assertFalse(
            any(call.args[0] == 180 for call in feature_spy.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
