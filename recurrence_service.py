"""Persistent recurrence rules, rolling occurrence generation, and series deletion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from availability import work_window_for_day
from config import REPEAT_HORIZON_DAYS
from database import RecurrenceRule, SessionLocal, Task
from gcal_service import GoogleCalendarService
from ml_engine import train_and_predict
from planning_service import attempt_schedule_task, database_interval_is_free


TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "一": 0,
    "tue": 1,
    "tuesday": 1,
    "二": 1,
    "wed": 2,
    "wednesday": 2,
    "三": 2,
    "thu": 3,
    "thursday": 3,
    "四": 3,
    "fri": 4,
    "friday": 4,
    "五": 4,
    "sat": 5,
    "saturday": 5,
    "六": 5,
    "sun": 6,
    "sunday": 6,
    "日": 6,
    "天": 6,
}


@dataclass(frozen=True)
class GenerationResult:
    rule_id: int
    created: int
    scheduled: int
    conflicts: int
    generated_through: date


def _db_time(value: datetime) -> datetime:
    local = value.replace(tzinfo=TIMEZONE) if value.tzinfo is None else value.astimezone(TIMEZONE)
    return local.replace(tzinfo=None)


def selected_weekdays(frequency: str, start_weekday: int) -> set[int]:
    normalized = (
        frequency.lower()
        .replace("星期", "")
        .replace("週", "")
        .replace("week", "")
        .replace("到", "-")
        .replace("至", "-")
        .replace(" ", "")
    )
    if normalized == "daily":
        return set(range(7))
    if normalized in {"weekly", "每週"}:
        return {start_weekday}
    if normalized in {"weekday", "weekdays", "mon-fri"}:
        return set(range(5))
    if "," in normalized:
        return {WEEKDAY_NAMES[value] for value in normalized.split(",")}
    if "-" in normalized:
        start_name, end_name = normalized.split("-", 1)
        start, end = WEEKDAY_NAMES[start_name], WEEKDAY_NAMES[end_name]
        return (
            set(range(start, end + 1))
            if start <= end
            else set(range(start, 7)) | set(range(end + 1))
        )
    return {WEEKDAY_NAMES[normalized]}


def occurrence_days(start: date, end: date, frequency: str) -> list[date]:
    weekdays = selected_weekdays(frequency, start.weekday())
    values: list[date] = []
    current = start
    while current <= end:
        if current.weekday() in weekdays:
            values.append(current)
        current += timedelta(days=1)
    return values


def create_rule(
    *,
    group_id: str,
    user_id: int,
    channel_id: int | None,
    task_name: str,
    minutes: int,
    frequency: str,
    fixed_time: str | None,
    allow_split: bool,
    priority: int,
    final_end_date: date | None,
    min_segment_minutes: int = 30,
    requested_start_date: date | None = None,
    duration_days: int | None = None,
    now: datetime | None = None,
) -> RecurrenceRule:
    now = now or datetime.now(TIMEZONE)
    default_start = now.date() + timedelta(days=1)
    if fixed_time and time.fromisoformat(fixed_time) > now.timetz().replace(tzinfo=None):
        default_start = now.date()
    start_date = requested_start_date or default_start
    if start_date < now.date():
        raise ValueError("重複任務的開始日期不能早於今天。")
    if (
        start_date == now.date()
        and fixed_time
        and time.fromisoformat(fixed_time) <= now.timetz().replace(tzinfo=None)
    ):
        raise ValueError("今天的固定時間已經過了，請指定未來時間。")
    if duration_days is not None:
        if duration_days < 1:
            raise ValueError("重複天數必須至少為 1。")
        final_end_date = start_date + timedelta(days=duration_days - 1)
    if final_end_date and final_end_date < start_date:
        raise ValueError("重複任務的截止日期早於第一個可建立日期。")
    renewal_mode = "fixed_end" if final_end_date else "ask"
    cycle_end = final_end_date or (
        start_date + timedelta(days=REPEAT_HORIZON_DAYS - 1)
    )
    with SessionLocal() as session:
        rule = RecurrenceRule(
            group_id=group_id,
            discord_user_id=str(user_id),
            source_channel_id=str(channel_id) if channel_id else None,
            task_name=task_name,
            estimated_minutes=minutes,
            frequency=frequency,
            fixed_time=fixed_time,
            allow_split=allow_split,
            min_segment_minutes=min_segment_minutes,
            priority=priority,
            renewal_mode=renewal_mode,
            start_date=start_date,
            cycle_end=cycle_end,
            final_end_date=final_end_date,
            status="active",
            created_at=_db_time(now),
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        session.expunge(rule)
        return rule


def _slot_is_free(calendar: GoogleCalendarService, start: datetime, end: datetime) -> bool:
    calendar_is_free = any(
        slot["start"] <= start and end <= slot["end"]
        for slot in calendar.get_free_slots(start.date())
    )
    return calendar_is_free and database_interval_is_free(start, end)


def generate_occurrences(
    rule_id: int,
    *,
    calendar: GoogleCalendarService | None = None,
    now: datetime | None = None,
) -> GenerationResult:
    now = now or datetime.now(TIMEZONE)
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        if not rule:
            raise ValueError("找不到重複規則。")
        hard_end = rule.final_end_date or rule.cycle_end
        rolling_end = min(hard_end, now.date() + timedelta(days=REPEAT_HORIZON_DAYS))
        first = max(
            rule.start_date,
            (rule.generated_through + timedelta(days=1))
            if rule.generated_through
            else rule.start_date,
        )
        if first > rolling_end or rule.status not in {"active", "awaiting_extension"}:
            return GenerationResult(rule.id, 0, 0, 0, rule.generated_through or rolling_end)
        days = occurrence_days(first, rolling_end, rule.frequency)
        snapshot = {
            "id": rule.id,
            "group_id": rule.group_id,
            "user_id": rule.discord_user_id,
            "channel_id": rule.source_channel_id,
            "name": rule.task_name,
            "minutes": rule.estimated_minutes,
            "fixed_time": rule.fixed_time,
            "allow_split": rule.allow_split,
            "min_segment_minutes": rule.min_segment_minutes,
            "priority": rule.priority,
        }

    created_ids: list[int] = []
    conflicts = 0
    for target_day in days:
        with SessionLocal() as session:
            exists = session.scalar(
                select(Task.id).where(
                    Task.recurrence_group == snapshot["group_id"],
                    Task.recurrence_date == target_day,
                )
            )
        if exists:
            continue
        if snapshot["fixed_time"]:
            start = datetime.combine(
                target_day, time.fromisoformat(snapshot["fixed_time"]), tzinfo=TIMEZONE
            )
            end = start + timedelta(minutes=snapshot["minutes"])
            if start <= now or not _slot_is_free(calendar, start, end):
                conflicts += 1
                continue
            event_id = calendar.create_event(snapshot["name"], start, end)
            with SessionLocal() as session:
                task = Task(
                    task_name=snapshot["name"],
                    estimated_minutes=snapshot["minutes"],
                    status="scheduled",
                    deadline=_db_time(end),
                    event_id=event_id,
                    discord_user_id=snapshot["user_id"],
                    source_channel_id=snapshot["channel_id"],
                    scheduled_start=_db_time(start),
                    scheduled_end=_db_time(end),
                    is_fixed=True,
                    is_locked=True,
                    recurrence_group=snapshot["group_id"],
                    recurrence_date=target_day,
                )
                session.add(task)
                session.commit()
                created_ids.append(task.id)
        else:
            start, end, _source = work_window_for_day(target_day)
            with SessionLocal() as session:
                task = Task(
                    task_name=snapshot["name"],
                    estimated_minutes=snapshot["minutes"],
                    status="pending",
                    available_from=_db_time(start),
                    deadline=_db_time(end),
                    discord_user_id=snapshot["user_id"],
                    source_channel_id=snapshot["channel_id"],
                    recurrence_group=snapshot["group_id"],
                    recurrence_date=target_day,
                    priority=snapshot["priority"],
                    allow_split=snapshot["allow_split"],
                    min_segment_minutes=snapshot["min_segment_minutes"],
                )
                session.add(task)
                session.commit()
                created_ids.append(task.id)

    scheduled = 0
    if not snapshot["fixed_time"] and created_ids:
        predictor = train_and_predict()
        for task_id in created_ids:
            result = attempt_schedule_task(
                task_id, calendar=calendar, predictor=predictor, now=now
            )
            scheduled += int(result.scheduled)

    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        rule.generated_through = rolling_end
        if rule.final_end_date and rolling_end >= rule.final_end_date:
            rule.status = "active"
        session.commit()
    return GenerationResult(rule_id, len(created_ids), scheduled, conflicts, rolling_end)


def maintain_rules(
    *, calendar: GoogleCalendarService | None = None, now: datetime | None = None
) -> list[GenerationResult]:
    now = now or datetime.now(TIMEZONE)
    with SessionLocal() as session:
        rules = list(
            session.scalars(
                select(RecurrenceRule).where(RecurrenceRule.status == "active")
            )
        )
        rule_ids = []
        for rule in rules:
            if rule.final_end_date and rule.final_end_date < now.date():
                rule.status = "ended"
            else:
                rule_ids.append(rule.id)
        session.commit()
    return [
        generate_occurrences(rule_id, calendar=calendar, now=now)
        for rule_id in rule_ids
    ]


def rules_due_for_extension(today: date | None = None) -> list[RecurrenceRule]:
    today = today or datetime.now(TIMEZONE).date()
    with SessionLocal() as session:
        rules = list(
            session.scalars(
                select(RecurrenceRule).where(
                    RecurrenceRule.renewal_mode == "ask",
                    RecurrenceRule.status == "active",
                    RecurrenceRule.cycle_end < today,
                )
            )
        )
        for rule in rules:
            session.expunge(rule)
        return rules


def mark_awaiting_extension(rule_id: int, now: datetime | None = None) -> None:
    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        if rule and rule.status == "active":
            rule.status = "awaiting_extension"
            rule.extension_notified_at = _db_time(now or datetime.now(TIMEZONE))
            session.commit()


def extend_rule(
    rule_id: int,
    *,
    days: int = REPEAT_HORIZON_DAYS,
    calendar: GoogleCalendarService | None = None,
    now: datetime | None = None,
) -> GenerationResult:
    now = now or datetime.now(TIMEZONE)
    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        if not rule or rule.renewal_mode != "ask":
            raise ValueError("此重複系列不能使用週期延長。")
        if rule.status == "ended":
            raise ValueError("此重複系列已結束。")
        if days < 1:
            raise ValueError("延長天數必須至少為 1。")
        resume_date = now.date()
        if (
            rule.fixed_time
            and time.fromisoformat(rule.fixed_time) <= now.timetz().replace(tzinfo=None)
        ):
            resume_date += timedelta(days=1)
        previous = rule.generated_through or (rule.start_date - timedelta(days=1))
        rule.generated_through = max(previous, resume_date - timedelta(days=1))
        rule.cycle_end = resume_date + timedelta(days=days - 1)
        rule.status = "active"
        rule.extension_notified_at = None
        session.commit()
    return generate_occurrences(rule_id, calendar=calendar, now=now)


def end_rule(rule_id: int) -> str:
    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        if not rule:
            raise ValueError("找不到重複系列。")
        rule.status = "ended"
        session.commit()
        return rule.task_name


def delete_series(
    group_id: str,
    user_id: int,
    *,
    calendar: GoogleCalendarService | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    now = now or datetime.now(TIMEZONE)
    now_db = _db_time(now)
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    with SessionLocal() as session:
        rule = session.scalar(
            select(RecurrenceRule).where(RecurrenceRule.group_id == group_id)
        )
        if rule and rule.discord_user_id != str(user_id):
            raise PermissionError("你只能刪除自己的重複系列。")
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.recurrence_group == group_id,
                    or_(Task.discord_user_id == str(user_id), Task.discord_user_id.is_(None)),
                    or_(
                        Task.status == "pending",
                        (Task.status == "scheduled")
                        & or_(Task.scheduled_start.is_(None), Task.scheduled_start >= now_db),
                    ),
                )
            )
        )
        name = rule.task_name if rule else (tasks[0].task_name if tasks else "重複任務")
        task_ids = [task.id for task in tasks]
        event_ids = [
            value
            for task in tasks
            for value in [task.event_id, *(segment.event_id for segment in task.segments)]
            if value
        ]

    for event_id in event_ids:
        try:
            calendar.delete_event(event_id)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in (404, 410):
                raise
    with SessionLocal() as session:
        if task_ids:
            for task in session.scalars(select(Task).where(Task.id.in_(task_ids))):
                session.delete(task)
        rule = session.scalar(
            select(RecurrenceRule).where(RecurrenceRule.group_id == group_id)
        )
        if rule:
            rule.status = "ended"
        session.commit()
    return name, len(task_ids)
