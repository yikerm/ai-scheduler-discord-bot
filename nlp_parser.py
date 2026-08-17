"""Backward-compatible task parser backed by the unified intent parser."""

from __future__ import annotations

from typing import Any

from nlp_router import parse_bot_intent


def parse_task_input(user_text: str) -> dict[str, Any]:
    parsed = parse_bot_intent(user_text)
    if parsed.get("action") not in {"add", "fixed"}:
        raise ValueError("這段文字不是一般待辦或固定行程。")
    if parsed.get("missing_fields"):
        labels = {
            "task_name": "任務名稱",
            "duration_minutes": "預計持續時間",
            "date": "日期",
            "time": "開始時間",
        }
        missing = "、".join(labels.get(field, field) for field in parsed["missing_fields"])
        raise ValueError(f"還缺少：{missing}。")
    deadline = parsed.get("deadline")
    if not deadline and parsed.get("date") and parsed.get("time"):
        deadline = f"{parsed['date']}T{parsed['time']}:00"
    return {
        "task_name": parsed["task_name"],
        "duration_minutes": int(parsed["duration_minutes"]),
        "deadline": deadline,
    }
