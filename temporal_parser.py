"""Deterministic Chinese date, clock, duration, and recurrence parsing."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
FULLWIDTH = str.maketrans("０１２３４５６７８９：／", "0123456789:/")
NUMBER_TOKEN = r"[0-9零〇一二兩三四五六七八九十百]+"


@dataclass(frozen=True)
class LocalFields:
    date_value: date | None = None
    clock: time | None = None
    end_clock: time | None = None
    duration_minutes: int | None = None
    recurrence: str | None = None
    recurrence_days: int | None = None
    allow_split: bool = False
    priority: int = 0
    original_date_text: str | None = None
    original_time_text: str | None = None
    original_duration_text: str | None = None
    ambiguities: tuple[str, ...] = ()


def chinese_number(value: str) -> int | None:
    text = value.translate(FULLWIDTH).replace("兩", "二").replace("個", "")
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not text or any(char not in digits and char not in "十百" for char in text):
        return None
    total = current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def _clock_from_match(period: str | None, hour_text: str, minute_text: str | None, half: bool) -> time | None:
    hour = chinese_number(hour_text)
    minute = 30 if half else chinese_number(minute_text or "0")
    if hour is None or minute is None or minute > 59:
        return None
    if period in {"下午", "晚上", "晚間", "傍晚"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "清晨", "上午", "早上"} and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return time(hour, minute)


def parse_clocks(text: str) -> list[tuple[time, str]]:
    normalized = text.translate(FULLWIDTH)
    pattern = re.compile(
        rf"(?P<period>凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|晚間)?\s*"
        rf"(?P<hour>{NUMBER_TOKEN})\s*[點時]"
        rf"(?:(?P<half>半)|(?P<minute>{NUMBER_TOKEN})\s*分)?"
        rf"|(?P<digital>(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d))"
    )
    values: list[tuple[time, str]] = []
    for match in pattern.finditer(normalized):
        original = match.group(0).strip()
        if match.group("digital"):
            values.append((time.fromisoformat(match.group("digital")), original))
            continue
        parsed = _clock_from_match(
            match.group("period"),
            match.group("hour"),
            match.group("minute"),
            bool(match.group("half")),
        )
        if parsed:
            values.append((parsed, original))
    return values


def parse_duration(text: str) -> tuple[int | None, str | None]:
    normalized = text.translate(FULLWIDTH)
    half_only = re.search(r"半\s*(?:個)?\s*(?:小時|鐘頭)", normalized)
    hour_match = re.search(
        rf"(?P<hours>{NUMBER_TOKEN})\s*(?:個)?\s*(?P<half>半)?\s*(?:小時|鐘頭)",
        normalized,
    )
    minute_match = re.search(rf"(?P<minutes>{NUMBER_TOKEN})\s*分鐘", normalized)
    hours = chinese_number(hour_match.group("hours")) if hour_match else 0
    minutes = chinese_number(minute_match.group("minutes")) if minute_match else 0
    if hour_match:
        if hour_match.group("half"):
            minutes = (minutes or 0) + 30
        value = (hours or 0) * 60 + (minutes or 0)
        original_end = minute_match.end() if minute_match else hour_match.end()
        original = normalized[hour_match.start():original_end]
        return (value if value > 0 else None), original
    if half_only:
        return 30, half_only.group(0)
    if minute_match:
        value = chinese_number(minute_match.group("minutes"))
        return (value if value and value > 0 else None), minute_match.group(0)

    clocks = parse_clocks(normalized)
    if len(clocks) >= 2 and re.search(r"(?:到|至|～|~|－|-)", normalized):
        start, end = clocks[0][0], clocks[1][0]
        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60
        value = end_minutes - start_minutes
        if 0 < value <= 12 * 60:
            return value, f"{clocks[0][1]}到{clocks[1][1]}"
    return None, None


def _date_for_month_day(month: int, day: int, today: date, year: int | None = None) -> date | None:
    candidate_year = year or today.year
    try:
        candidate = date(candidate_year, month, day)
    except ValueError:
        return None
    if year is None and candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_date_text(
    text: str,
    now: datetime | None = None,
    clock: time | None = None,
) -> tuple[date | None, str | None, tuple[str, ...]]:
    now = now or datetime.now(TIMEZONE)
    today = now.date()
    normalized = text.translate(FULLWIDTH)

    iso = re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", normalized)
    if iso:
        value = _date_for_month_day(int(iso.group(2)), int(iso.group(3)), today, int(iso.group(1)))
        return value, iso.group(0), () if value else ("日期不存在",)
    chinese = re.search(rf"(?:(20\d{{2}})\s*年\s*)?({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*[日號]?", normalized)
    if chinese:
        year = int(chinese.group(1)) if chinese.group(1) else None
        value = _date_for_month_day(chinese_number(chinese.group(2)), chinese_number(chinese.group(3)), today, year)
        return value, chinese.group(0), () if value else ("日期不存在",)
    slash = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", normalized)
    if slash:
        value = _date_for_month_day(int(slash.group(1)), int(slash.group(2)), today)
        return value, slash.group(0), () if value else ("日期不存在",)

    relatives = (("後天", 2), ("明天", 1), ("今天", 0), ("今日", 0))
    for token, offset in relatives:
        if token in normalized:
            return today + timedelta(days=offset), token, ()

    weekday = re.search(
        r"(?P<prefix>下(?:個)?|這(?:個)?|本)?(?:週|星期|禮拜)(?P<weekday>[一二三四五六日天])",
        normalized,
    )
    if weekday:
        target_weekday = WEEKDAYS[weekday.group("weekday")]
        prefix = weekday.group("prefix") or ""
        if prefix.startswith("下"):
            next_monday = today + timedelta(days=7 - today.weekday())
            target = next_monday + timedelta(days=target_weekday)
        elif prefix.startswith("這") or prefix == "本":
            target = today - timedelta(days=today.weekday()) + timedelta(days=target_weekday)
        else:
            delta = (target_weekday - today.weekday()) % 7
            if delta == 0 and clock and datetime.combine(today, clock, tzinfo=TIMEZONE) <= now:
                delta = 7
            target = today + timedelta(days=delta)
        return target, weekday.group(0), ()

    if "月底" in normalized:
        last_day = calendar.monthrange(today.year, today.month)[1]
        target = date(today.year, today.month, last_day)
        if target < today:
            month = 1 if today.month == 12 else today.month + 1
            year = today.year + 1 if today.month == 12 else today.year
            target = date(year, month, calendar.monthrange(year, month)[1])
        return target, "月底", ("月底未指定確切時間",)
    return None, None, ()


def parse_recurrence(text: str) -> tuple[str | None, int | None]:
    normalized = text.translate(FULLWIDTH).replace("禮拜", "週").replace("星期", "週")
    days: int | None = None
    day_match = re.search(rf"(?:未來|接下來)?\s*({NUMBER_TOKEN})\s*天", normalized)
    week_match = re.search(rf"(?:未來|接下來)?\s*({NUMBER_TOKEN})\s*(?:週|個週)", normalized)
    if day_match:
        days = chinese_number(day_match.group(1))
    elif week_match:
        weeks = chinese_number(week_match.group(1))
        days = weeks * 7 if weeks else None

    if any(token in normalized for token in ("平日", "週一到週五", "週一至週五")):
        return "mon-fri", days
    range_match = re.search(r"週([一二三四五六日天])\s*(?:到|至|[-~～])\s*週?([一二三四五六日天])", normalized)
    if range_match:
        return f"{range_match.group(1)}-{range_match.group(2)}", days
    if any(token in normalized for token in ("每天", "每日", "天天")):
        return "daily", days
    if any(token in normalized for token in ("每週", "每個週")):
        weekdays = re.findall(r"週([一二三四五六日天])", normalized)
        if weekdays:
            return ",".join(dict.fromkeys(weekdays)), days
        return "weekly", days
    if "重複" in normalized:
        weekdays = re.findall(r"週([一二三四五六日天])", normalized)
        return (",".join(dict.fromkeys(weekdays)) if weekdays else "daily"), days
    return None, days


def parse_local_fields(text: str, now: datetime | None = None) -> LocalFields:
    now = now or datetime.now(TIMEZONE)
    clocks = parse_clocks(text)
    clock = clocks[0][0] if clocks else None
    end_clock = clocks[1][0] if len(clocks) > 1 and re.search(r"(?:到|至|～|~|－|-)", text) else None
    date_value, original_date, ambiguities = parse_date_text(text, now, clock)
    duration, original_duration = parse_duration(text)
    recurrence, recurrence_days = parse_recurrence(text)
    allow_split = bool(re.search(r"(?:可以|可|允許).{0,3}(?:分段|拆分)|(?:分段|拆分).{0,3}(?:完成|安排)", text))
    priority = 2 if any(token in text for token in ("一定要", "緊急", "最高優先")) else (1 if "優先" in text else 0)
    return LocalFields(
        date_value=date_value,
        clock=clock,
        end_clock=end_clock,
        duration_minutes=duration,
        recurrence=recurrence,
        recurrence_days=recurrence_days,
        allow_split=allow_split,
        priority=priority,
        original_date_text=original_date,
        original_time_text=clocks[0][1] if clocks else None,
        original_duration_text=original_duration,
        ambiguities=ambiguities,
    )
