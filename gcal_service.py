"""Google Calendar Service Account wrapper."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import (
    BUSY_CALENDAR_IDS,
    CALENDAR_ID,
    EXTERNAL_CALENDAR_URLS,
    GOOGLE_APPLICATION_CREDENTIALS,
)
from external_calendar import ExternalCalendarError, get_external_busy_periods


CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GREEN_COLOR_ID = "10"
RED_COLOR_ID = "11"


class GoogleCalendarService:
    def __init__(
        self,
        calendar_id: str | None = None,
        busy_calendar_ids: tuple[str, ...] | list[str] | None = None,
        external_calendar_urls: tuple[str, ...] | list[str] | None = None,
        timezone: str = "Asia/Taipei",
    ) -> None:
        credentials_path = GOOGLE_APPLICATION_CREDENTIALS
        if not credentials_path:
            raise RuntimeError("缺少 GOOGLE_APPLICATION_CREDENTIALS。")
        if not Path(credentials_path).is_file():
            raise FileNotFoundError(f"找不到 Service Account 憑證：{credentials_path}")
        self.calendar_id = calendar_id or CALENDAR_ID
        if not self.calendar_id:
            raise RuntimeError("缺少 CALENDAR_ID。")
        self.busy_calendar_ids = tuple(
            dict.fromkeys((self.calendar_id, *(busy_calendar_ids or BUSY_CALENDAR_IDS)))
        )
        self.external_calendar_urls = tuple(
            dict.fromkeys(external_calendar_urls or EXTERNAL_CALENDAR_URLS)
        )
        self.timezone = ZoneInfo(timezone)
        credentials = Credentials.from_service_account_file(
            credentials_path, scopes=[CALENDAR_SCOPE]
        )
        self.service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )

    def get_free_slots(self, target_date: date | datetime | str) -> list[dict[str, datetime]]:
        day = self._to_date(target_date)
        day_start = datetime.combine(day, time.min, tzinfo=self.timezone)
        day_end = day_start + timedelta(days=1)
        response = self.service.freebusy().query(
            body={
                "timeMin": day_start.isoformat(),
                "timeMax": day_end.isoformat(),
                "timeZone": str(self.timezone),
                "items": [{"id": value} for value in self.busy_calendar_ids],
            }
        ).execute()
        calendars = response.get("calendars", {})
        unavailable = [
            value
            for value in self.busy_calendar_ids
            if calendars.get(value, {}).get("errors")
        ]
        if unavailable:
            raise RuntimeError(
                "無法讀取避衝日曆：" + ", ".join(unavailable)
                + "。請確認已分享給 Service Account。"
            )
        busy_periods = [
            period
            for calendar_id in self.busy_calendar_ids
            for period in calendars.get(calendar_id, {}).get("busy", [])
        ]
        try:
            external_periods = get_external_busy_periods(
                self.external_calendar_urls, day_start, day_end, self.timezone
            )
        except ExternalCalendarError as exc:
            raise RuntimeError(f"無法讀取外部避衝日曆：{exc}") from exc
        busy_periods.extend(
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in external_periods
        )
        merged = self._merge_busy_periods(busy_periods, day_start, day_end)
        free_slots: list[dict[str, datetime]] = []
        cursor = day_start
        for busy_start, busy_end in merged:
            if cursor < busy_start:
                free_slots.append({"start": cursor, "end": busy_start})
            cursor = max(cursor, busy_end)
        if cursor < day_end:
            free_slots.append({"start": cursor, "end": day_end})
        return free_slots

    def create_event(
        self,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str | None = None,
    ) -> str:
        if end_time <= start_time:
            raise ValueError("end_time 必須晚於 start_time。")
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": self._as_local(start_time).isoformat()},
            "end": {"dateTime": self._as_local(end_time).isoformat()},
        }
        if description:
            body["description"] = description
        event = self.service.events().insert(
            calendarId=self.calendar_id, body=body
        ).execute()
        return event["id"]

    def update_event_time(
        self,
        event_id: str,
        start_time: datetime,
        end_time: datetime,
        title: str | None = None,
    ) -> dict[str, Any]:
        event = self.get_event(event_id)
        event["start"] = {"dateTime": self._as_local(start_time).isoformat()}
        event["end"] = {"dateTime": self._as_local(end_time).isoformat()}
        if title:
            event["summary"] = title
        return self.service.events().update(
            calendarId=self.calendar_id, eventId=event_id, body=event
        ).execute()

    @staticmethod
    def _replace_feedback_description(description: str, feedback: str) -> str:
        marker = "【AI Scheduler 回饋】"
        prefix = description.split(marker, 1)[0].rstrip()
        return "\n\n".join(value for value in (prefix, marker + "\n" + feedback) if value)

    def update_event_feedback(
        self, event_id: str, efficiency: int, mental: int
    ) -> dict[str, Any]:
        event = self.get_event(event_id)
        feedback = f"完成狀態：已完成\n效率評分：{efficiency}\n精神評分：{mental}"
        event["description"] = self._replace_feedback_description(
            event.get("description", ""), feedback
        )
        event["colorId"] = GREEN_COLOR_ID
        return self.service.events().update(
            calendarId=self.calendar_id, eventId=event_id, body=event
        ).execute()

    def update_event_incomplete(
        self, event_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        event = self.get_event(event_id)
        feedback = "完成狀態：未完成\n效率評分：0\n精神評分：1"
        if reason:
            feedback += f"\n未完成原因：{reason}"
        event["description"] = self._replace_feedback_description(
            event.get("description", ""), feedback
        )
        event["colorId"] = RED_COLOR_ID
        return self.service.events().update(
            calendarId=self.calendar_id, eventId=event_id, body=event
        ).execute()

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self.service.events().get(
            calendarId=self.calendar_id, eventId=event_id
        ).execute()

    def list_event_changes(
        self, sync_token: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        events: list[dict[str, Any]] = []
        page_token: str | None = None
        next_sync_token: str | None = None
        while True:
            parameters: dict[str, Any] = {
                "calendarId": self.calendar_id,
                "showDeleted": True,
                "maxResults": 2500,
            }
            if sync_token:
                parameters["syncToken"] = sync_token
            if page_token:
                parameters["pageToken"] = page_token
            response = self.service.events().list(**parameters).execute()
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            next_sync_token = response.get("nextSyncToken") or next_sync_token
            if not page_token:
                return events, next_sync_token

    def delete_event(self, event_id: str) -> None:
        self.service.events().delete(
            calendarId=self.calendar_id, eventId=event_id
        ).execute()

    def _as_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def _parse_calendar_time(self, value: str) -> datetime:
        return self._as_local(datetime.fromisoformat(value.replace("Z", "+00:00")))

    def _merge_busy_periods(
        self,
        periods: list[dict[str, str]],
        day_start: datetime,
        day_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        normalized: list[tuple[datetime, datetime]] = []
        for period in periods:
            busy_start = self._parse_calendar_time(period["start"])
            busy_end = self._parse_calendar_time(period["end"])
            if busy_end <= day_start or busy_start >= day_end:
                continue
            normalized.append((max(busy_start, day_start), min(busy_end, day_end)))
        normalized.sort(key=lambda value: value[0])
        merged: list[tuple[datetime, datetime]] = []
        for busy_start, busy_end in normalized:
            if not merged or busy_start > merged[-1][1]:
                merged.append((busy_start, busy_end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], busy_end))
        return merged

    @staticmethod
    def _to_date(value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)
