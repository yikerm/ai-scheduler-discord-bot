from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/ai_scheduler_bot_structured_plan_tests.db"
)

from bot import _plan_entries
from database import Base, SessionLocal, Task, TaskSegment, engine
from structured_plan import PlanEntry, StructuredPlanView, format_plan_pages


TZ = ZoneInfo("Asia/Taipei")


async def _load(_days: int) -> list[PlanEntry]:
    return []


class PlanFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_has_range_selector_and_pagination_buttons(self):
        view = StructuredPlanView(
            user_id=123,
            load_callback=_load,
            initial_entries=[],
            days=7,
        )
        self.assertEqual(len(view.children), 3)
        self.assertEqual(
            [option.value for option in view.children[0].options],
            ["1", "3", "7", "14", "30"],
        )
        self.assertTrue(view.children[1].disabled)
        self.assertTrue(view.children[2].disabled)

    async def test_entries_are_grouped_and_show_segments_and_locks(self):
        entries = [
            PlanEntry(
                datetime(2026, 8, 6, 9, 0),
                datetime(2026, 8, 6, 10, 0),
                "看醫生",
                locked=True,
            ),
            PlanEntry(
                datetime(2026, 8, 6, 14, 0),
                datetime(2026, 8, 6, 14, 45),
                "報告",
                segment_index=1,
                segment_count=2,
            ),
        ]
        page = format_plan_pages(entries, 7)[0]
        self.assertIn("08/06（週四）", page)
        self.assertIn("看醫生 🔒", page)
        self.assertIn("報告（1/2）", page)

    async def test_long_plan_is_paginated_under_discord_limit(self):
        start = datetime(2026, 8, 6, 8, 0)
        entries = [
            PlanEntry(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                "很長的任務名稱" * 10,
            )
            for index in range(40)
        ]
        pages = format_plan_pages(entries, 30)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) < 2000 for page in pages))


class PlanQueryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_only_scheduled_bot_tasks_and_real_segments_are_returned(self):
        now = datetime.now(TZ).replace(tzinfo=None)
        with SessionLocal() as session:
            fixed = Task(
                task_name="固定行程",
                estimated_minutes=60,
                status="scheduled",
                scheduled_start=now + timedelta(hours=2),
                scheduled_end=now + timedelta(hours=3),
                discord_user_id="123",
                is_fixed=True,
                is_locked=True,
            )
            split = Task(
                task_name="分段報告",
                estimated_minutes=90,
                status="scheduled",
                scheduled_start=now + timedelta(hours=4),
                scheduled_end=now + timedelta(hours=8, minutes=45),
                discord_user_id="123",
                allow_split=True,
            )
            pending = Task(
                task_name="等待排程",
                estimated_minutes=30,
                status="pending",
                deadline=now + timedelta(days=1),
                discord_user_id="123",
            )
            session.add_all([fixed, split, pending])
            session.flush()
            session.add_all(
                [
                    TaskSegment(
                        task_id=split.id,
                        segment_index=1,
                        scheduled_start=now + timedelta(hours=4),
                        scheduled_end=now + timedelta(hours=4, minutes=45),
                        event_id="segment-1",
                    ),
                    TaskSegment(
                        task_id=split.id,
                        segment_index=2,
                        scheduled_start=now + timedelta(hours=8),
                        scheduled_end=now + timedelta(hours=8, minutes=45),
                        event_id="segment-2",
                    ),
                ]
            )
            session.commit()

        entries = _plan_entries(123, 3)
        self.assertEqual([entry.title for entry in entries], ["固定行程", "分段報告", "分段報告"])
        self.assertEqual(
            [entry.segment_index for entry in entries], [None, 1, 2]
        )
        self.assertTrue(entries[0].locked)


if __name__ == "__main__":
    unittest.main()
