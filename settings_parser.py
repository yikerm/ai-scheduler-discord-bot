"""Local parsing helpers for dynamic school/weekend scheduling settings."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from temporal_parser import TIMEZONE, parse_clocks, parse_date_text


def parse_schedule_settings(text: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(TIMEZONE)
    normalized = text.replace("星期", "週").replace("禮拜", "週")
    if any(token in normalized for token in ("未開學", "沒開學", "放假模式")):
        mode = "not_school"
    elif "開學" in normalized:
        mode = "school"
    else:
        mode = None

    target_date = None
    if re.search(
        r"(?:今天|明天|後天|這週|下週|本週|[0-9零〇一二兩三四五六七八九十]+月[0-9零〇一二兩三四五六七八九十]+|20\d{2}[-/]\d{1,2}[-/]\d{1,2})",
        normalized,
    ):
        target_date, _original, _ambiguities = parse_date_text(normalized, now)

    clocks = [value.strftime("%H:%M") for value, _ in parse_clocks(normalized)]
    updates: list[dict[str, str]] = []
    has_weekday = bool(
        re.search(r"(?:週一.{0,4}週五|平日|一到五|一至五)", normalized)
    )
    has_weekend = bool(
        re.search(r"(?:週六.{0,3}(?:日|天)|六日|週末)", normalized)
    )
    if mode and has_weekday and has_weekend and len(clocks) >= 4:
        updates.extend(
            [
                {
                    "mode": mode,
                    "day_type": "weekday",
                    "start_time": clocks[0],
                    "end_time": clocks[1],
                },
                {
                    "mode": mode,
                    "day_type": "weekend",
                    "start_time": clocks[2],
                    "end_time": clocks[3],
                },
            ]
        )
    elif mode and has_weekday and len(clocks) >= 2:
        updates.append(
            {
                "mode": mode,
                "day_type": "weekday",
                "start_time": clocks[0],
                "end_time": clocks[1],
            }
        )
    elif mode and has_weekend and len(clocks) >= 2:
        updates.append(
            {
                "mode": mode,
                "day_type": "weekend",
                "start_time": clocks[0],
                "end_time": clocks[1],
            }
        )

    result: dict[str, Any] = {"mode": mode, "updates": updates}
    if target_date and mode and not updates:
        result["effective_date"] = target_date.isoformat()
    if target_date and clocks and not updates:
        result["target_date"] = target_date.isoformat()
        if len(clocks) >= 2:
            result["start_time"], result["end_time"] = clocks[0], clocks[1]
        elif re.search(r"(?:排到|直到|最晚)", normalized):
            result["end_time"] = clocks[0]
        else:
            result["start_time"] = clocks[0]
    return result
