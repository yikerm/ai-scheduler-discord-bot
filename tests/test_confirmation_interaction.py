from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_confirmation_tests.db"
)

from bot import ActionConfirmationView, ExecutionOutcome


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False

    async def defer(self) -> None:
        self.deferred = True


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.edits: list[dict] = []
        self.followup = SimpleNamespace(send=AsyncMock())
        self.message = SimpleNamespace(edit=AsyncMock())

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class ConfirmationInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_removes_buttons_and_updates_original_response(self):
        row = SimpleNamespace(
            id=9,
            payload_json='{"action": "fixed"}',
            channel_id="456",
        )
        interaction = FakeInteraction()
        view = ActionConfirmationView(request_id=9, user_id=123)
        with (
            patch("bot._load_pending", return_value=row),
            patch("bot._delete_pending") as delete_pending,
            patch(
                "bot._execute_intent",
                new=AsyncMock(return_value=ExecutionOutcome("📌 已建立固定行程「meeting」。")),
            ),
        ):
            await view.handle(interaction, "confirm")

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits[0]["view"], None)
        self.assertIn("正在執行", interaction.edits[0]["content"])
        self.assertEqual(
            interaction.edits[-1]["content"], "📌 已建立固定行程「meeting」。"
        )
        self.assertIsNone(interaction.edits[-1]["view"])
        delete_pending.assert_called_once_with(9)

    async def test_execution_failure_restores_retry_buttons(self):
        row = SimpleNamespace(
            id=10,
            payload_json='{"action": "fixed"}',
            channel_id="456",
        )
        interaction = FakeInteraction()
        view = ActionConfirmationView(request_id=10, user_id=123)
        with (
            patch("bot._load_pending", return_value=row),
            patch("bot._delete_pending") as delete_pending,
            patch(
                "bot._execute_intent",
                new=AsyncMock(side_effect=ValueError("指定時段已有行程")),
            ),
        ):
            await view.handle(interaction, "confirm")

        delete_pending.assert_not_called()
        self.assertIn("操作失敗", interaction.edits[-1]["content"])
        self.assertIsInstance(interaction.edits[-1]["view"], ActionConfirmationView)


if __name__ == "__main__":
    unittest.main()
