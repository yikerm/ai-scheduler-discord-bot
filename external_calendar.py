"""讀取外部 iCal/ICS 訂閱來源，轉成忙碌時段。"""

from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar


class ExternalCalendarError(RuntimeError):
    """外部訂閱來源無法安全讀取或解析。"""


def get_external_busy_periods(
    urls: tuple[str, ...],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    """下載所有 iCal 來源，回傳與查詢區間重疊的事件時段。"""
    periods: list[tuple[datetime, datetime]] = []
    for url in urls:
        try:
            request = Request(url, headers={"User-Agent": "AI-Scheduler-Bot/1.0"})
            with urlopen(request, timeout=15) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ExternalCalendarError(f"無法下載外部日曆來源：{exc}") from exc

        try:
            calendar = Calendar.from_ical(payload)
            events = recurring_ical_events.of(calendar).between(start, end)
        except Exception as exc:
            raise ExternalCalendarError(
                "外部網址沒有回傳可解析的 iCal/ICS 日曆資料。"
            ) from exc

        for event in events:
            event_start = _as_local(event.decoded("DTSTART"), timezone)
            event_end = _as_local(event.decoded("DTEND"), timezone)
            if event_end <= start or event_start >= end:
                continue
            periods.append((max(event_start, start), min(event_end, end)))
    return periods


def _as_local(value: datetime, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        # 全天事件的 date 值會被視為當地 00:00；DTEND 遵守 iCal 的排他結束規則。
        value = datetime.combine(value, datetime.min.time())
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)
