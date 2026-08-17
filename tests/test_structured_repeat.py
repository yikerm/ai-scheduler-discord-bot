from __future__ import annotations

import os
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_structured_repeat_tests.db"
)

from structured_repeat import (
    RepeatMinuteSelect,
    StructuredRepeatView,
)


TZ = ZoneInfo("Asia/Taipei")


async def _submit(_payload: dict):
    return "created", None


class StructuredRepeatTests(unittest.IsolatedAsyncioTestCase):
    def _view(self) -> StructuredRepeatView:
        return StructuredRepeatView(
            user_id=123,
            channel_id=456,
            title="  每日   閱讀  ",
            submit_callback=_submit,
            now=datetime(2026, 8, 5, 10, 0, tzinfo=TZ),
        )

    async def test_first_page_uses_weekday_multiselect_and_fits_rows(self):
        view = self._view()
        self.assertEqual(len(view.children), 9)
        weekday = view.children[0]
        self.assertEqual((weekday.min_values, weekday.max_values), (1, 7))
        self.assertEqual(len([item for item in weekday.options if item.default]), 7)
        self.assertEqual({item.row for item in view.children}, {0, 1, 2, 3, 4})

    async def test_fixed_minutes_are_quarter_hour_only(self):
        view = self._view()
        view.page = 2
        view.mode = "fixed"
        view.refresh_components()
        minute_select = next(
            item for item in view.children if isinstance(item, RepeatMinuteSelect)
        )
        self.assertEqual(
            [option.value for option in minute_select.options],
            ["0", "15", "30", "45"],
        )

    async def test_flexible_payload_supports_arbitrary_weekdays_and_split(self):
        view = self._view()
        view.weekdays = {"tue", "thu", "sun"}
        view.duration_minutes = 100
        view.allow_split = True
        view.min_segment_minutes = 30
        view.priority = 1
        payload = view.build_payload()
        self.assertEqual(payload["frequency"], "tue,thu,sun")
        self.assertIsNone(payload["time"])
        self.assertEqual(payload["date"], "2026-08-06")
        self.assertTrue(payload["allow_split"])
        self.assertEqual(payload["min_segment_minutes"], 30)
        self.assertEqual(payload["priority"], 1)
        self.assertIsNone(payload["recurrence_end_date"])

    async def test_fixed_payload_uses_selected_end_date(self):
        view = self._view()
        view.mode = "fixed"
        view.duration_minutes = 60
        view.hour = 20
        view.minute = 15
        view.expiry_mode = "fixed_end"
        view.final_end_date = date(2026, 12, 31)
        payload = view.build_payload()
        self.assertEqual(payload["time"], "20:15")
        self.assertFalse(payload["allow_split"])
        self.assertEqual(payload["recurrence_end_date"], "2026-12-31")

    async def test_split_requires_two_valid_segments(self):
        view = self._view()
        view.duration_minutes = 90
        view.allow_split = True
        view.min_segment_minutes = 60
        with self.assertRaisesRegex(ValueError, "至少需 120 分鐘"):
            view.build_payload()

    async def test_fixed_start_today_cannot_be_in_past(self):
        view = self._view()
        view.start_date = date(2026, 8, 5)
        view.mode = "fixed"
        view.duration_minutes = 30
        view.hour = 9
        with self.assertRaisesRegex(ValueError, "已經過了"):
            view.build_payload()


if __name__ == "__main__":
    unittest.main()
