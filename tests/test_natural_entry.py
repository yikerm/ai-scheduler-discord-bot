from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_natural_entry_tests.db"
)

from bot import (
    _natural_entry_outcome,
    _natural_read_action,
    _natural_reply,
)
from structured_add import StructuredAddView
from structured_fixed import StructuredFixedView
from structured_repeat import StructuredRepeatView


class NaturalEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_opens_blank_structured_form_instead_of_writing(self):
        payload = {
            "action": "add",
            "task_name": "期末報告",
            "date": "2026-08-20",
            "time": "18:00",
            "duration_minutes": 180,
        }
        with patch("bot._execute_intent", new=AsyncMock()) as execute:
            outcome = await _natural_entry_outcome(payload, 123, 456, "新增報告")

        self.assertIsInstance(outcome.view, StructuredAddView)
        self.assertEqual(outcome.view.task_name, "期末報告")
        self.assertEqual(outcome.view.date_mode, "none")
        self.assertIsNone(outcome.view.duration_minutes)
        execute.assert_not_awaited()

    async def test_fixed_ignores_model_time_until_user_selects_it(self):
        payload = {
            "action": "fixed",
            "task_name": "meeting",
            "date": "2027-02-11",
            "time": "14:00",
            "duration_minutes": 60,
        }
        outcome = await _natural_entry_outcome(payload, 123, 456, "安排 meeting")

        self.assertIsInstance(outcome.view, StructuredFixedView)
        self.assertEqual(outcome.view.title, "meeting")
        self.assertIsNone(outcome.view.event_date)
        self.assertIsNone(outcome.view.hour)
        self.assertIsNone(outcome.view.duration_minutes)

    async def test_repeat_opens_structured_repeat_form(self):
        payload = {
            "action": "repeat",
            "task_name": "閱讀",
            "frequency": "daily",
            "duration_minutes": 60,
        }
        outcome = await _natural_entry_outcome(payload, 123, 456, "每天閱讀")

        self.assertIsInstance(outcome.view, StructuredRepeatView)
        self.assertEqual(outcome.view.title, "閱讀")
        self.assertIsNone(outcome.view.duration_minutes)

    async def test_reschedule_only_guides_to_google_calendar(self):
        payload = {
            "action": "reschedule",
            "task_name": "文件撰寫",
            "date": "2026-08-08",
            "time": "15:00",
        }
        with patch("bot._route_payload", new=AsyncMock()) as route:
            outcome = await _natural_entry_outcome(payload, 123, 456, "改期")

        self.assertIn("Google Calendar", outcome.content)
        route.assert_not_awaited()

    async def test_common_tasks_query_skips_gemini(self):
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            reply=AsyncMock(),
        )
        with (
            patch("bot.parse_bot_entry", side_effect=AssertionError("Gemini should not run")),
            patch("bot._find_tasks_by_text", return_value=[]),
            patch("bot._format_tasks", return_value="沒有任務"),
        ):
            await _natural_reply(message, "請列出所有尚未完成的任務")

        message.reply.assert_awaited_once_with("沒有任務", view=None)

    def test_local_read_only_classification(self):
        self.assertEqual(_natural_read_action("請列出所有尚未完成的任務"), "tasks")
        self.assertEqual(_natural_read_action("顯示未來規劃"), "plan")
        self.assertIsNone(_natural_read_action("幫我安排一個任務"))


if __name__ == "__main__":
    unittest.main()
