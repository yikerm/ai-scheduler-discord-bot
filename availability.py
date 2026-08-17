"""Dynamic work windows for school/non-school weekdays and weekends."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from database import ScheduleModePeriod, ScheduleOverride, SessionLocal, WorkSchedule


VALID_MODES = {"school", "not_school"}
VALID_DAY_TYPES = {"weekday", "weekend"}


def _validate_minutes(start_minute: int, end_minute: int) -> tuple[int, int]:
    if not 0 <= start_minute < 24 * 60:
        raise ValueError("開始時間必須介於 00:00 到 23:59。")
    if end_minute <= start_minute:
        end_minute += 24 * 60
    if end_minute - start_minute > 20 * 60:
        raise ValueError("單日可排程範圍不可超過 20 小時。")
    return start_minute, end_minute


def mode_for_day(target_day: date) -> str:
    with SessionLocal() as session:
        period = session.scalar(
            select(ScheduleModePeriod)
            .where(ScheduleModePeriod.effective_from <= target_day)
            .order_by(ScheduleModePeriod.effective_from.desc())
            .limit(1)
        )
    return period.mode if period else "not_school"


def work_window_for_day(target_day: date) -> tuple[datetime, datetime, str]:
    """Return naive local start/end datetimes and the rule that selected them."""
    with SessionLocal() as session:
        override = session.get(ScheduleOverride, target_day)
        if override:
            start_minute, end_minute = override.start_minute, override.end_minute
            source = "override"
        else:
            mode = mode_for_day(target_day)
            day_type = "weekday" if target_day.weekday() < 5 else "weekend"
            row = session.scalar(
                select(WorkSchedule).where(
                    WorkSchedule.mode == mode,
                    WorkSchedule.day_type == day_type,
                )
            )
            start_minute = row.start_minute if row else 9 * 60
            end_minute = row.end_minute if row else 22 * 60
            source = f"{mode}:{day_type}"
    start = datetime.combine(target_day, datetime.min.time()) + timedelta(
        minutes=start_minute
    )
    end = datetime.combine(target_day, datetime.min.time()) + timedelta(
        minutes=end_minute
    )
    return start, end, source


def set_work_schedule(
    mode: str, day_type: str, start_minute: int, end_minute: int
) -> None:
    if mode not in VALID_MODES:
        raise ValueError("作息模式必須是 school 或 not_school。")
    if day_type not in VALID_DAY_TYPES:
        raise ValueError("日期類型必須是 weekday 或 weekend。")
    start_minute, end_minute = _validate_minutes(start_minute, end_minute)
    with SessionLocal() as session:
        row = session.scalar(
            select(WorkSchedule).where(
                WorkSchedule.mode == mode,
                WorkSchedule.day_type == day_type,
            )
        )
        if row is None:
            row = WorkSchedule(mode=mode, day_type=day_type)
            session.add(row)
        row.start_minute = start_minute
        row.end_minute = end_minute
        session.commit()


def set_mode(mode: str, effective_from: date) -> None:
    if mode not in VALID_MODES:
        raise ValueError("作息模式必須是 school 或 not_school。")
    with SessionLocal() as session:
        row = session.scalar(
            select(ScheduleModePeriod).where(
                ScheduleModePeriod.effective_from == effective_from
            )
        )
        if row is None:
            row = ScheduleModePeriod(mode=mode, effective_from=effective_from)
            session.add(row)
        else:
            row.mode = mode
        session.commit()


def set_day_override(target_day: date, start_minute: int, end_minute: int) -> None:
    start_minute, end_minute = _validate_minutes(start_minute, end_minute)
    with SessionLocal() as session:
        row = session.get(ScheduleOverride, target_day)
        if row is None:
            row = ScheduleOverride(day=target_day)
            session.add(row)
        row.start_minute = start_minute
        row.end_minute = end_minute
        session.commit()


def minutes_label(value: int) -> str:
    day_suffix = "（隔日）" if value >= 24 * 60 else ""
    value %= 24 * 60
    return f"{value // 60:02d}:{value % 60:02d}{day_suffix}"


def schedule_summary(target_day: date | None = None) -> str:
    day = target_day or date.today()
    active_mode = mode_for_day(day)
    labels = {
        ("school", "weekday"): "開學平日",
        ("school", "weekend"): "開學六日",
        ("not_school", "weekday"): "未開學平日",
        ("not_school", "weekend"): "未開學六日",
    }
    with SessionLocal() as session:
        rows = list(session.scalars(select(WorkSchedule)))
    values = {(row.mode, row.day_type): row for row in rows}
    lines = [f"目前模式：{'開學' if active_mode == 'school' else '未開學'}"]
    for key, label in labels.items():
        row = values.get(key)
        start, end = (row.start_minute, row.end_minute) if row else (540, 1320)
        lines.append(f"{label}：{minutes_label(start)}–{minutes_label(end)}")
    return "\n".join(lines)
