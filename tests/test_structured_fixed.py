from __future__ import annotations

import os
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_structured_fixed_tests.db"
)

from structured_fixed import CreateFixedButton, StructuredFixedView


TZ = ZoneInfo("Asia/Taipei")


async def _submit(_payload: dict):
    return "created", None


class StructuredFixedTests(unittest.IsolatedAsyncioTestCase):
    def _view(self, callback=_submit) -> StructuredFixedView:
        return StructuredFixedView(
            user_id=123,
            channel_id=456,
            title="  看   醫生  ",
            submit_callback=callback,
            now=datetime(2026, 8, 5, 10, 0, tzinfo=TZ),
        )

    async def test_form_fits_discord_component_limits(self):
        view = self._view()
        self.assertEqual(len(view.children), 6)
        self.assertEqual(len(view.children[0].options), 23)
        self.assertEqual(len(view.children[1].options), 24)
        self.assertEqual(len(view.children[2].options), 12)
        self.assertEqual({item.row for item in view.children}, {0, 1, 2, 3, 4})

    async def test_payload_uses_selected_date_and_five_minute_time(self):
        view = self._view()
        view.event_date = date(2026, 8, 6)
        view.hour = 18
        view.minute = 20
        view.duration_minutes = 90
        payload = view.build_payload()
        self.assertEqual(payload["action"], "fixed")
        self.assertEqual(payload["task_name"], "看 醫生")
        self.assertEqual(payload["date"], "2026-08-06")
        self.assertEqual(payload["time"], "18:20")
        self.assertEqual(payload["duration_minutes"], 90)

    async def test_past_start_is_rejected(self):
        view = self._view()
        view.event_date = date(2026, 8, 5)
        view.hour = 9
        view.duration_minutes = 30
        with self.assertRaisesRegex(ValueError, "晚於現在"):
            view.build_payload()

    async def test_summary_calculates_cross_midnight_end(self):
        view = self._view()
        view.event_date = date(2026, 8, 6)
        view.hour = 23
        view.minute = 30
        view.duration_minutes = 90
        self.assertIn("2026-08-07 01:00", view.summary())

    async def test_submit_error_keeps_form_for_retry(self):
        async def fail(_payload: dict):
            raise ValueError("指定時段已有行程")

        view = self._view(fail)
        view.event_date = date(2026, 8, 6)
        view.hour = 18
        view.duration_minutes = 60
        button = next(
            item for item in view.children if isinstance(item, CreateFixedButton)
        )
        interaction = AsyncMock()
        await button.callback(interaction)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertIs(kwargs["view"], view)
        self.assertIn("指定時段已有行程", kwargs["content"])


if __name__ == "__main__":
    unittest.main()
