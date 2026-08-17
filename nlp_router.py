"""Unified Gemini intent parsing with deterministic local time validation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import GEMINI_API_KEY
from temporal_parser import parse_date_text, parse_local_fields


MODEL_NAME = "gemini-2.5-flash"
TIMEZONE = ZoneInfo("Asia/Taipei")
ACTIONS = {
    "add",
    "tasks",
    "plan",
    "fixed",
    "repeat",
    "delete",
    "reset",
    "reschedule",
    "schedule_settings",
    "unknown",
}


def _local_action(text: str, local) -> str | None:
    compact = text.casefold()
    if any(token in compact for token in ("清除所有", "全部刪除", "重置")):
        return "reset"
    if any(token in compact for token in ("改期", "改到", "移到", "換到", "重新安排")):
        return "reschedule"
    if any(token in compact for token in ("刪除", "取消", "移除")):
        return "delete"
    if any(token in compact for token in ("作息", "開學期間", "未開學", "沒開學", "現在是開學", "切換成開學", "可以排到")):
        return "schedule_settings"
    if any(token in compact for token in ("規劃", "行程表", "未來幾天")) and any(
        token in compact for token in ("查看", "看", "列出", "哪些", "什麼")
    ):
        return "plan"
    if any(token in compact for token in ("任務清單", "尚未完成", "有哪些任務", "查詢任務")):
        return "tasks"
    if local.recurrence:
        return "repeat"
    if local.date_value and local.clock:
        if any(token in compact for token in ("以前", "之前", "截止", "前完成", "前做完")):
            return "add"
        return "fixed"
    if any(token in compact for token in ("新增", "建立待辦", "加入任務", "收錄任務")):
        return "add"
    return None


def _clean_title(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(str(value).split()).strip(" ，,。；;：:")
    cleaned = re.sub(r"^(?:我)?(?:想)?要\s*", "", cleaned)
    cleaned = re.sub(r"\s*(?:預計要|預計|大約|約|需要|要花)\s*$", "", cleaned)
    cleaned = re.sub(r"\s*(?:這是)?固定行程\s*$", "", cleaned)
    return cleaned.strip() or None

def _fallback_title(text: str) -> str | None:
    value = re.sub(r"<@!?\d+>", "", text)
    value = re.sub(
        r"^(?:請|麻煩)?(?:幫我)?(?:新增|安排|建立|記錄|收錄)?",
        "",
        value,
    )
    value = re.sub(
        r"(?:今天|明天|後天|本?(?:週|星期|禮拜)[一二三四五六日天]|下(?:個)?(?:週|星期|禮拜)[一二三四五六日天])",
        " ",
        value,
    )
    value = re.sub(
        r"(?:凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|晚間)?\s*[0-9零〇一二兩三四五六七八九十百]+\s*[點時](?:半|[0-9零〇一二兩三四五六七八九十百]+分)?",
        " ",
        value,
    )
    value = re.sub(
        r"(?:預計|大約|約|需要|要花)?\s*[0-9零〇一二兩三四五六七八九十百]+\s*(?:個)?(?:半)?\s*(?:小時|鐘頭|分鐘)",
        " ",
        value,
    )
    value = re.sub(r"(?:開始|以前|前|截止|這是固定行程|固定行程)", " ", value)
    value = re.sub(r"[，,。；;：:]", " ", value)
    value = " ".join(value.split()).strip()
    return _clean_title(value)


def _schema() -> dict[str, Any]:
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    nullable_integer = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "task_name": nullable_string,
            "duration_minutes": nullable_integer,
            "date": nullable_string,
            "time": nullable_string,
            "end_time": nullable_string,
            "deadline": nullable_string,
            "frequency": nullable_string,
            "recurrence_end_date": nullable_string,
            "days": nullable_integer,
            "query": nullable_string,
            "plan_days": nullable_integer,
            "task_number": nullable_integer,
            "allow_split": {"type": "boolean"},
            "priority": {"type": "integer"},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "settings": {"type": "object"},
        },
        "required": [
            "action", "task_name", "duration_minutes", "date", "time",
            "end_time", "deadline", "frequency", "recurrence_end_date", "days", "query",
            "plan_days", "task_number", "allow_split", "priority",
            "missing_fields", "ambiguities", "settings",
        ],
        "additionalProperties": False,
    }


def _gemini_payload(user_text: str, now: datetime) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("缺少 GEMINI_API_KEY。")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("缺少 google-genai；請先安裝新版 Gemini SDK。") from exc

    prompt = f"""你是台灣繁體中文 Discord 排程助理的語意解析器。
只能回傳符合 Schema 的 JSON，不可加入說明，也不可執行操作。

目前 Asia/Taipei：{now.strftime('%Y-%m-%d %H:%M:%S %A')}
規則：
- add 是沒有固定開始時間的一般待辦；fixed 是指定某日某時開始的固定行程。
- repeat 是每天、每週或指定星期重複；時間可為 null，代表彈性重複任務。
- repeat 若使用者說「持續到／直到／截至某日」，將該日填入 recurrence_end_date；未指定則為 null。
- repeat 若使用者說「從某日開始」，將開始日填入 date；date 不可與 recurrence_end_date 混用。
- tasks/plan 是查詢；delete/reset/reschedule 是高風險操作。
- schedule_settings 用於開學／未開學、平日／六日作息或單日可排程時間。
- 不可猜測持續時間。使用者未提供時必須填 null 並列入 missing_fields。
- 不確定日期、指涉對象或意圖時，保留原意並列入 ambiguities，不可編造。
- 下週、下星期、下禮拜指下一個星期一開始的曆週。
- settings 可包含 mode（school/not_school）、effective_date、day_type
  （weekday/weekend）、start_time、end_time、target_date、updates（多組作息）。

使用者訊息：{user_text}"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=512,
            response_mime_type="application/json",
            response_json_schema=_schema(),
        ),
    )
    try:
        return json.loads(response.text)
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Gemini 未回傳有效的結構化 JSON。") from exc


def _entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "task_name": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
        },
        "required": ["action", "task_name"],
        "additionalProperties": False,
    }


def _gemini_entry_payload(user_text: str, now: datetime) -> dict[str, Any]:
    """Classify an @Bob entry without asking the model to parse scheduling fields."""
    if not GEMINI_API_KEY:
        raise RuntimeError("缺少 GEMINI_API_KEY。")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("缺少 google-genai；請先安裝新版 Gemini SDK。") from exc

    prompt = f"""你是台灣繁體中文 Discord 排程助理的入口分類器。
只能回傳符合 Schema 的 JSON，不可加入說明，也不可執行操作。

目前 Asia/Taipei：{now.strftime('%Y-%m-%d %H:%M:%S %A')}
只需要完成兩件事：
1. 判斷 action：add、tasks、plan、fixed、repeat、delete、reset、reschedule、schedule_settings 或 unknown。
2. add、fixed、repeat 時擷取簡潔的事項名稱；其他 action 的 task_name 可為 null。

不要解析或回傳日期、時間、時長、星期、分割方式、期限、任務編號或作息內容。
add 是一般待辦；fixed 是指定時間行程；repeat 是週期任務；schedule_settings 是作息調整。
不確定時使用 unknown，不可猜測。

使用者訊息：{user_text}"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=128,
            response_mime_type="application/json",
            response_json_schema=_entry_schema(),
        ),
    )
    try:
        return json.loads(response.text)
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Gemini 未回傳有效的入口分類 JSON。") from exc


def parse_bot_entry(
    user_text: str,
    *,
    now: datetime | None = None,
    model_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return only the action and optional title for the safe @Bob entry."""
    if not user_text.strip():
        raise ValueError("訊息不可為空白。")
    now = now or datetime.now(TIMEZONE)
    local = parse_local_fields(user_text, now)
    deterministic_action = _local_action(user_text, local)
    no_title_actions = {
        "tasks",
        "plan",
        "delete",
        "reset",
        "reschedule",
        "schedule_settings",
    }
    if model_response is None and deterministic_action in no_title_actions:
        model_response = {"action": deterministic_action, "task_name": None}
    elif model_response is None:
        try:
            model_response = _gemini_entry_payload(user_text, now)
        except Exception:
            if not deterministic_action:
                raise
            model_response = {
                "action": deterministic_action,
                "task_name": _fallback_title(user_text),
            }

    action = str(model_response.get("action") or "unknown")
    if action not in ACTIONS:
        action = "unknown"
    if deterministic_action:
        action = deterministic_action
    title = None
    if action in {"add", "fixed", "repeat"}:
        title = _clean_title(model_response.get("task_name")) or _fallback_title(
            user_text
        )
    return {"action": action, "task_name": title}


def _recurrence_boundaries(text: str, now: datetime) -> tuple[str | None, str | None]:
    """Return explicit recurrence start/end dates without treating weekday ranges as dates."""
    start_text = None
    end_text = None
    start_match = re.search(r"(?:從|自)\s*(.+?)\s*(?:開始|起)", text)
    if not start_match:
        start_match = re.search(
            r"((?:20\d{2}[-/]\d{1,2}[-/]\d{1,2})|(?:[0-9零〇一二兩三四五六七八九十]+月[0-9零〇一二兩三四五六七八九十]+[日號]?)|(?:\d{1,2}/\d{1,2}))\s*(?:開始|起)",
            text,
        )
    if start_match:
        start_text = start_match.group(1)

    end_match = re.search(r"(?:持續到|一直到|直到|截至|截止(?:到)?)\s*(.+)$", text)
    if not end_match:
        end_match = re.search(
            r"(?:到|至)\s*((?:20\d{2}[-/]\d{1,2}[-/]\d{1,2})|(?:[0-9零〇一二兩三四五六七八九十]+月[0-9零〇一二兩三四五六七八九十]+[日號]?)|(?:\d{1,2}/\d{1,2}))",
            text,
        )
    if end_match:
        end_text = end_match.group(1)

    start, end = None, None
    if start_text:
        value, _original, _ambiguities = parse_date_text(start_text, now)
        start = value.isoformat() if value else None
    if end_text:
        value, _original, _ambiguities = parse_date_text(end_text, now)
        end = value.isoformat() if value else None
    return start, end



def _normalise_result(
    user_text: str,
    result: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    local = parse_local_fields(user_text, now)
    action = result.get("action") if result.get("action") in ACTIONS else "unknown"
    deterministic_action = _local_action(user_text, local)
    if deterministic_action:
        action = deterministic_action

    ambiguities = [str(value) for value in result.get("ambiguities", []) if value]
    ambiguities.extend(local.ambiguities)
    local_date = local.date_value.isoformat() if local.date_value else None
    if action == "repeat":
        start_date, end_date = _recurrence_boundaries(user_text, now)
        if start_date and result.get("date") and result.get("date") != start_date:
            ambiguities.append(
                f"開始日期解析衝突：原文換算為 {start_date}，模型回傳 {result.get('date')}"
            )
        if end_date and result.get("recurrence_end_date") and result.get("recurrence_end_date") != end_date:
            ambiguities.append(
                f"截止日期解析衝突：原文換算為 {end_date}，模型回傳 {result.get('recurrence_end_date')}"
            )
        explicit_non_weekday = bool(
            local.original_date_text
            and not re.fullmatch(
                r"(?:下(?:個)?|這(?:個)?|本)?(?:週|星期|禮拜)[一二三四五六日天]",
                local.original_date_text,
            )
        )
        result["date"] = start_date or (local_date if explicit_non_weekday and not end_date else None)
        result["recurrence_end_date"] = end_date
    else:
        gemini_date = result.get("date")
        if gemini_date and local_date and gemini_date != local_date:
            ambiguities.append(
                f"日期解析衝突：原文換算為 {local_date}，模型回傳 {gemini_date}"
            )
        if local_date:
            result["date"] = local_date
    if local.clock:
        result["time"] = local.clock.strftime("%H:%M")
    if action == "add" and local_date:
        result["deadline"] = (
            f"{local_date}T{local.clock.strftime('%H:%M')}:00"
            if local.clock else None
        )
    if local.end_clock:
        result["end_time"] = local.end_clock.strftime("%H:%M")
    if local.duration_minutes:
        result["duration_minutes"] = local.duration_minutes
    if local.recurrence:
        result["frequency"] = local.recurrence
    if local.recurrence_days:
        result["days"] = local.recurrence_days
    result["allow_split"] = bool(result.get("allow_split") or local.allow_split)
    result["priority"] = max(int(result.get("priority") or 0), local.priority)
    result["action"] = action
    result["original_date_text"] = local.original_date_text
    result["original_time_text"] = local.original_time_text
    result["original_duration_text"] = local.original_duration_text

    if action in {"add", "fixed", "repeat"}:
        result["task_name"] = _clean_title(result.get("task_name")) or _fallback_title(user_text)

    missing: list[str] = []
    required = {
        "add": ("task_name", "duration_minutes"),
        "fixed": ("task_name", "date", "time", "duration_minutes"),
        "repeat": ("task_name", "frequency", "duration_minutes"),
        "reschedule": ("date", "time"),
    }.get(action, ())
    for field in required:
        if result.get(field) in (None, "", 0):
            missing.append(field)
    if action == "delete" and not (result.get("task_number") or result.get("query") or result.get("task_name")):
        missing.append("query")
    result["missing_fields"] = list(dict.fromkeys(missing))
    result["ambiguities"] = list(dict.fromkeys(ambiguities))
    return result


def parse_bot_intent(
    user_text: str,
    *,
    now: datetime | None = None,
    model_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse one new message with at most one Gemini request."""
    if not user_text.strip():
        raise ValueError("訊息不可為空白。")
    now = now or datetime.now(TIMEZONE)
    if model_response is None:
        try:
            model_response = _gemini_payload(user_text, now)
        except Exception:
            local = parse_local_fields(user_text, now)
            action = _local_action(user_text, local)
            if not action:
                raise
            model_response = {
                "action": action,
                "task_name": _fallback_title(user_text),
                "duration_minutes": None,
                "date": None,
                "time": None,
                "end_time": None,
                "deadline": None,
                "frequency": None,
                "recurrence_end_date": None,
                "days": None,
                "query": user_text if action in {"tasks", "delete", "reschedule"} else None,
                "plan_days": None,
                "task_number": None,
                "allow_split": False,
                "priority": 0,
                "missing_fields": [],
                "ambiguities": [],
                "settings": {},
            }
    return _normalise_result(user_text, dict(model_response), now)


def merge_supplement(
    payload: dict[str, Any],
    supplement: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fill deterministic missing fields without another Gemini request."""
    local = parse_local_fields(supplement, now)
    merged = dict(payload)
    if local.date_value:
        merged["date"] = local.date_value.isoformat()
    if local.clock:
        merged["time"] = local.clock.strftime("%H:%M")
    if local.end_clock:
        merged["end_time"] = local.end_clock.strftime("%H:%M")
    if local.duration_minutes:
        merged["duration_minutes"] = local.duration_minutes
    if local.recurrence:
        merged["frequency"] = local.recurrence
    if local.recurrence_days:
        merged["days"] = local.recurrence_days
    merged["allow_split"] = bool(merged.get("allow_split") or local.allow_split)
    required = {
        "add": ("task_name", "duration_minutes"),
        "fixed": ("task_name", "date", "time", "duration_minutes"),
        "repeat": ("task_name", "frequency", "duration_minutes"),
        "reschedule": ("date", "time"),
    }.get(str(merged.get("action")), ())
    merged["missing_fields"] = [field for field in required if not merged.get(field)]
    return merged
