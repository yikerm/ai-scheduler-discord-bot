from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ml_engine import TaskScorePredictor
from rolling_optimizer import build_task_options, optimize_rolling_schedule


TZ = ZoneInfo("Asia/Taipei")


def task(
    task_id: int,
    *,
    priority: int = 0,
    deadline: datetime | None = None,
    status: str = "pending",
    allow_split: bool = False,
    minutes: int = 120,
):
    return SimpleNamespace(
        id=task_id,
        estimated_minutes=minutes,
        deadline=deadline,
        available_from=None,
        priority=priority,
        allow_split=allow_split,
        min_segment_minutes=60,
        status=status,
    )


class RollingOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 13, 9, tzinfo=TZ)
        self.predictor = TaskScorePredictor(None, None)

    def test_split_options_exist_even_when_contiguous_option_exists(self):
        slots = [
            {"start": self.start, "end": self.start + timedelta(hours=2)},
            {
                "start": self.start + timedelta(hours=4),
                "end": self.start + timedelta(hours=5),
            },
        ]
        options = build_task_options(
            task(1, allow_split=True), slots, self.predictor
        )
        self.assertTrue(any(len(option.intervals) > 1 for option in options))

    def test_higher_priority_wins_when_only_one_task_fits(self):
        slots = [
            {"start": self.start, "end": self.start + timedelta(hours=2)}
        ]
        selected = optimize_rolling_schedule(
            [task(1, priority=0), task(2, priority=2)],
            slots,
            self.predictor,
            {},
        )
        self.assertEqual(set(selected), {2})

    def test_earlier_deadline_wins_without_preferring_late_start(self):
        slots = [
            {"start": self.start, "end": self.start + timedelta(hours=2)}
        ]
        selected = optimize_rolling_schedule(
            [
                task(1, deadline=self.start + timedelta(days=1)),
                task(2, deadline=self.start + timedelta(days=5)),
            ],
            slots,
            self.predictor,
            {},
        )
        self.assertEqual(set(selected), {1})

    def test_existing_task_stays_put_when_scores_are_equal(self):
        slots = [
            {"start": self.start, "end": self.start + timedelta(hours=5)}
        ]
        current = ((self.start, self.start + timedelta(hours=2)),)
        selected = optimize_rolling_schedule(
            [task(3, status="scheduled")],
            slots,
            self.predictor,
            {3: current},
        )
        self.assertEqual(selected[3].intervals, current)
        self.assertTrue(selected[3].is_current)


if __name__ == "__main__":
    unittest.main()
