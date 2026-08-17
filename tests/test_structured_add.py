from __future__ import annotations

import os
import unittest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_structured_add_tests.db")

from planning_service import _split_plan
from structured_add import StructuredAddView


TZ = ZoneInfo("Asia/Taipei")


async def _submit(_payload: dict):
    return "ok", None


class StructuredAddTests(unittest.IsolatedAsyncioTestCase):
    def _view(self) -> StructuredAddView:
        return StructuredAddView(
            user_id=123,
            channel_id=456,
            task_name="  完成   期末報告  ",
            submit_callback=_submit,
            now=datetime(2026, 8, 5, 10, 0, tzinfo=TZ),
        )

    async def test_form_fits_discord_component_limits(self):
        view = self._view()
        self.assertEqual(len(view.children), 9)
        self.assertLessEqual(len(view.children[0].options), 25)
        self.assertEqual({item.row for item in view.children}, {0, 1, 2, 3, 4})

    async def test_payload_uses_date_as_deadline_not_earliest_start(self):
        view = self._view()
        view.date_mode = "date"
        view.deadline_date = date(2026, 8, 6)
        view.deadline_time = time(18, 30)
        view.duration_minutes = 180
        view.allow_split = True
        view.min_segment_minutes = 45
        view.priority = 2
        payload = view.build_payload()
        self.assertIsNone(payload["date"])
        self.assertEqual(payload["deadline"], "2026-08-06T18:30:00")
        self.assertEqual(payload["duration_minutes"], 180)
        self.assertTrue(payload["allow_split"])
        self.assertEqual(payload["min_segment_minutes"], 45)
        self.assertEqual(payload["priority"], 2)

    async def test_split_requires_at_least_two_segments(self):
        view = self._view()
        view.duration_minutes = 90
        view.allow_split = True
        view.min_segment_minutes = 60
        with self.assertRaisesRegex(ValueError, "至少 120 分鐘"):
            view.build_payload()

    async def test_no_deadline_is_valid(self):
        view = self._view()
        view.duration_minutes = 60
        payload = view.build_payload()
        self.assertIsNone(payload["deadline"])
        self.assertEqual(view.task_name, "完成 期末報告")


class SplitPlannerTests(unittest.TestCase):
    def test_task_specific_minimum_is_respected(self):
        slots = [
            {"start": datetime(2026, 8, 5, 9, tzinfo=TZ), "end": datetime(2026, 8, 5, 9, 45, tzinfo=TZ)},
            {"start": datetime(2026, 8, 5, 14, tzinfo=TZ), "end": datetime(2026, 8, 5, 14, 45, tzinfo=TZ)},
        ]
        intervals = _split_plan(slots, 90, 45)
        durations = [int((end - start).total_seconds() // 60) for start, end in intervals]
        self.assertEqual(durations, [45, 45])

    def test_impossible_two_segment_minimum_is_rejected(self):
        slots = [
            {"start": datetime(2026, 8, 5, 9, tzinfo=TZ), "end": datetime(2026, 8, 5, 12, tzinfo=TZ)}
        ]
        self.assertEqual(_split_plan(slots, 90, 60), [])


if __name__ == "__main__":
    unittest.main()
