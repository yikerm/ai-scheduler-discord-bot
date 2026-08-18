"""Stable task-name normalization and lightweight personal task categories."""

from __future__ import annotations

import re


CATEGORY_KEYS = (
    "memory",
    "reading",
    "writing",
    "meeting",
    "spiritual",
    "exercise",
    "errand",
    "leisure",
    "other",
)

_CATEGORY_RULES = (
    ("spiritual", ("靈修", "禱告", "讀經", "聚會", "教會")),
    ("memory", ("背單字", "背英文", "背書", "記憶", "複習單字")),
    ("writing", ("寫作", "報告", "論文", "稿", "ppt", "簡報", "整理資料")),
    ("reading", ("資格考", "讀書", "閱讀", "讀英文", "讀論文", "考題", "研究")),
    ("meeting", ("meeting", "會議", "討論", "面談", "約談")),
    ("exercise", ("運動", "健身", "跑步", "游泳", "保齡球")),
    ("errand", ("銀行", "預約", "採買", "購物", "辦事", "繳費")),
    ("leisure", ("娛樂", "遊戲", "電影", "休息", "旅行")),
)


def normalize_task_name(value: str | None) -> str:
    """Normalize display variants while preserving the user's semantic title."""
    text = str(value or "").strip().casefold()
    text = re.sub(
        r"[（(]\s*(?:第\s*)?\d+\s*/\s*\d+\s*(?:段)?\s*[）)]",
        "",
        text,
    )
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)
    return text or "unknown"


def infer_task_category(value: str | None) -> str:
    key = normalize_task_name(value)
    for category, tokens in _CATEGORY_RULES:
        if any(token.casefold() in key for token in tokens):
            return category
    return "other"
