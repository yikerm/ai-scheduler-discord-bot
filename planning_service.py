"""Calendar-aware immediate, split, and multi-day task planning."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from availability import work_window_for_day
from config import MAX_REPLAN_MOVES, PLANNING_HORIZON_DAYS
from database import SessionLocal, Task, TaskSegment
from gcal_service import GoogleCalendarService
from ml_engine import (
    ScheduleDecision,
    TaskScorePredictor,
    optimize_daily_schedule,
    train_and_predict,
)
from rolling_optimizer import PlanOption, optimize_rolling_schedule


TIMEZONE = ZoneInfo("Asia/Taipei")
MIN_SEGMENT_MINUTES = 30


@dataclass(frozen=True)
class AttemptResult:
    task_id: int
    scheduled: bool
    start: datetime | None = None
    end: datetime | None = None
    segment_count: int = 0
    failure_reason: str | None = None
    failure_details: str | None = None


@dataclass(frozen=True)
class ReplanChange:
    task_id: int
    task_name: str
    old_start: str | None
    old_end: str | None
    new_start: str
    new_end: str


@dataclass(frozen=True)
class ReplanProposal:
    new_task_id: int
    changes: tuple[ReplanChange, ...]

    def to_dict(self) -> dict:
        return {
            "new_task_id": self.new_task_id,
            "changes": [asdict(change) for change in self.changes],
        }


@dataclass(frozen=True)
class RollingReplanChange:
    task_id: int
    task_name: str
    priority: int
    old_intervals: tuple[tuple[str, str], ...]
    new_intervals: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RollingReplanProposal:
    new_task_id: int | None
    changes: tuple[RollingReplanChange, ...]
    unchanged_count: int

    @property
    def moves_existing(self) -> bool:
        return any(change.old_intervals for change in self.changes)

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "new_task_id": self.new_task_id,
            "changes": [asdict(change) for change in self.changes],
            "unchanged_count": self.unchanged_count,
        }


def _local(value: datetime) -> datetime:
    return value.replace(tzinfo=TIMEZONE) if value.tzinfo is None else value.astimezone(TIMEZONE)


def _db_time(value: datetime) -> datetime:
    return _local(value).replace(tzinfo=None)


def _ceil_half_hour(value: datetime) -> datetime:
    local = _local(value).replace(second=0, microsecond=0)
    remainder = local.minute % 30
    if remainder:
        local += timedelta(minutes=30 - remainder)
    return local


def _merge_slots(slots: list[dict[str, datetime]]) -> list[dict[str, datetime]]:
    if not slots:
        return []
    ordered = sorted(slots, key=lambda item: item["start"])
    merged = [dict(ordered[0])]
    for slot in ordered[1:]:
        if slot["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], slot["end"])
        else:
            merged.append(dict(slot))
    return merged


def working_slots(
    calendar: GoogleCalendarService,
    target_day: date,
    *,
    now: datetime | None = None,
) -> list[dict[str, datetime]]:
    start_naive, end_naive, _source = work_window_for_day(target_day)
    window_start = start_naive.replace(tzinfo=TIMEZONE)
    window_end = end_naive.replace(tzinfo=TIMEZONE)
    free = list(calendar.get_free_slots(target_day))
    if window_end.date() != target_day:
        free.extend(calendar.get_free_slots(window_end.date()))
    lower = max(window_start, _ceil_half_hour(now)) if now and target_day == _local(now).date() else window_start
    clipped: list[dict[str, datetime]] = []
    for slot in free:
        start = max(_local(slot["start"]), lower)
        end = min(_local(slot["end"]), window_end)
        if start < end:
            clipped.append({"start": start, "end": end})
    return _merge_slots(clipped)


def _failure(slots: list[dict[str, datetime]], minutes: int, deadline: datetime | None) -> tuple[str, str]:
    longest = max(
        (int((slot["end"] - slot["start"]).total_seconds() // 60) for slot in slots),
        default=0,
    )
    if deadline and _local(deadline) <= datetime.now(TIMEZONE):
        reason = "deadline_too_close"
    else:
        reason = "no_free_slot"
    details = json.dumps(
        {"required_minutes": minutes, "longest_free_minutes": longest},
        ensure_ascii=False,
    )
    return reason, details


def _record_failure(task_id: int, reason: str, details: str) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            return
        task.last_schedule_attempt_at = _db_time(datetime.now(TIMEZONE))
        task.schedule_failure_reason = reason
        task.schedule_failure_details = details
        task.schedule_attempt_count = (task.schedule_attempt_count or 0) + 1
        session.commit()


def _clear_failure_state(task_id: int) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            return
        _clear_failure(task)
        task.schedule_failure_notified_at = None
        task.schedule_failure_notified_reason = None
        session.commit()



def _clear_failure(task: Task) -> None:
    task.last_schedule_attempt_at = _db_time(datetime.now(TIMEZONE))
    task.schedule_failure_reason = None
    task.schedule_failure_details = None
    task.schedule_attempt_count = 0


def _split_plan(
    slots: list[dict[str, datetime]],
    total_minutes: int,
    min_segment_minutes: int = MIN_SEGMENT_MINUTES,
) -> list[tuple[datetime, datetime]]:
    if min_segment_minutes < 1:
        raise ValueError("最短分段時間必須大於 0。")
    if total_minutes < 2 * min_segment_minutes:
        return []
    remaining = total_minutes
    result: list[tuple[datetime, datetime]] = []
    for slot in slots:
        capacity = int((slot["end"] - slot["start"]).total_seconds() // 60)
        if capacity < min_segment_minutes:
            continue
        take = remaining if remaining <= capacity else min(capacity, remaining - min_segment_minutes)
        if take < min_segment_minutes:
            continue
        result.append((slot["start"], slot["start"] + timedelta(minutes=take)))
        remaining -= take
        if remaining == 0:
            return result
    return []

def _store_contiguous(task_id: int, decision: ScheduleDecision, event_id: str) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError("找不到待排程任務。")
        task.status = "scheduled"
        task.event_id = event_id
        task.scheduled_start = _db_time(decision.start)
        task.scheduled_end = _db_time(decision.end)
        _clear_failure(task)
        session.commit()


def _store_segments(
    task_id: int,
    intervals: list[tuple[datetime, datetime]],
    event_ids: list[str],
) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError("找不到待排程任務。")
        task.status = "scheduled"
        task.event_id = None
        task.scheduled_start = _db_time(intervals[0][0])
        task.scheduled_end = _db_time(intervals[-1][1])
        _clear_failure(task)
        for index, ((start, end), event_id) in enumerate(
            zip(intervals, event_ids, strict=True), start=1
        ):
            session.add(
                TaskSegment(
                    task_id=task.id,
                    segment_index=index,
                    scheduled_start=_db_time(start),
                    scheduled_end=_db_time(end),
                    event_id=event_id,
                )
            )
        session.commit()


def attempt_schedule_task(
    task_id: int,
    *,
    calendar: GoogleCalendarService | None = None,
    predictor: TaskScorePredictor | None = None,
    now: datetime | None = None,
) -> AttemptResult:
    now = now or datetime.now(TIMEZONE)
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    predictor = predictor or train_and_predict()
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task or task.status != "pending":
            return AttemptResult(task_id, False, failure_reason="not_pending")
        session.expunge(task)

    first_day = (
        max(now.date(), _local(task.available_from).date())
        if task.available_from
        else now.date()
    )
    planning_end = now.date() + timedelta(days=PLANNING_HORIZON_DAYS)
    if first_day > planning_end:
        _clear_failure_state(task.id)
        return AttemptResult(task.id, False, failure_reason="outside_horizon")
    last_day = planning_end
    if task.deadline:
        last_day = min(last_day, _local(task.deadline).date())
    all_slots: list[dict[str, datetime]] = []
    for offset in range((last_day - first_day).days + 1):
        target_day = first_day + timedelta(days=offset)
        slots = working_slots(calendar, target_day, now=now)
        all_slots.extend(slots)
        decisions = optimize_daily_schedule([task], slots, predictor, require_all=True)
        if decisions:
            decision = decisions[0]
            event_id = calendar.create_event(task.task_name, decision.start, decision.end)
            _store_contiguous(task.id, decision, event_id)
            return AttemptResult(task.id, True, decision.start, decision.end, 1)

    if task.allow_split:
        intervals = _split_plan(
            all_slots,
            task.estimated_minutes,
            task.min_segment_minutes or MIN_SEGMENT_MINUTES,
        )
        if intervals:
            event_ids: list[str] = []
            try:
                count = len(intervals)
                for index, (start, end) in enumerate(intervals, start=1):
                    event_ids.append(
                        calendar.create_event(
                            f"{task.task_name}（{index}/{count}）", start, end
                        )
                    )
            except Exception:
                for event_id in event_ids:
                    try:
                        calendar.delete_event(event_id)
                    except Exception:
                        pass
                raise
            _store_segments(task.id, intervals, event_ids)
            return AttemptResult(
                task.id, True, intervals[0][0], intervals[-1][1], len(intervals)
            )

    reason, details = _failure(all_slots, task.estimated_minutes, task.deadline)
    _record_failure(task.id, reason, details)
    return AttemptResult(
        task.id, False, failure_reason=reason, failure_details=details
    )


def clear_outside_horizon_failures(now: datetime | None = None) -> int:
    """Clear stale failure flags for tasks that are not eligible for the 7-day planner yet."""
    now = now or datetime.now(TIMEZONE)
    planning_end = now.date() + timedelta(days=PLANNING_HORIZON_DAYS)
    cleared = 0
    with SessionLocal() as session:
        tasks = list(session.scalars(
            select(Task).where(
                Task.status == "pending",
                Task.schedule_failure_reason.is_not(None),
                Task.available_from.is_not(None),
            )
        ))
        for task in tasks:
            if _local(task.available_from).date() <= planning_end:
                continue
            _clear_failure(task)
            task.schedule_failure_notified_at = None
            task.schedule_failure_notified_reason = None
            cleared += 1
        session.commit()
    return cleared



def schedule_all_pending() -> list[AttemptResult]:
    predictor = train_and_predict()
    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
    with SessionLocal() as session:
        task_ids = list(
            session.scalars(
                select(Task.id)
                .where(Task.status == "pending")
                .order_by(Task.priority.desc(), Task.deadline, Task.id)
            )
        )
    return [
        attempt_schedule_task(
            task_id, calendar=calendar, predictor=predictor
        )
        for task_id in task_ids
    ]


def _task_intervals(task: Task) -> tuple[tuple[datetime, datetime], ...]:
    if task.segments:
        return tuple(
            (_local(segment.scheduled_start), _local(segment.scheduled_end))
            for segment in task.segments
        )
    if task.scheduled_start and task.scheduled_end:
        return ((_local(task.scheduled_start), _local(task.scheduled_end)),)
    return ()


def _iso_intervals(
    intervals: tuple[tuple[datetime, datetime], ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((start.isoformat(), end.isoformat()) for start, end in intervals)


def _proposal_tasks(
    user_id: int,
    now: datetime,
) -> tuple[list[Task], dict[int, tuple[tuple[datetime, datetime], ...]]]:
    horizon_end = now + timedelta(days=PLANNING_HORIZON_DAYS + 1)
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .where(
                    Task.discord_user_id == str(user_id),
                    or_(
                        (
                            (Task.status == "pending")
                            & or_(Task.deadline.is_(None), Task.deadline >= _db_time(now))
                            & or_(
                                Task.available_from.is_(None),
                                Task.available_from < _db_time(horizon_end),
                            )
                        ),
                        (
                            (Task.status == "scheduled")
                            & (Task.is_fixed.is_(False))
                            & (Task.is_locked.is_(False))
                            & (Task.scheduled_start >= _db_time(now))
                            & (Task.scheduled_start < _db_time(horizon_end))
                        ),
                    ),
                )
                .options(selectinload(Task.segments))
                .order_by(Task.priority.desc(), Task.deadline, Task.id)
            )
        )
        current = {
            task.id: _task_intervals(task)
            for task in tasks
            if task.status == "scheduled"
        }
        for task in tasks:
            session.expunge(task)
    return tasks, current


def propose_rolling_replan(
    new_task_id: int | None = None,
    *,
    user_id: int | None = None,
    calendar: GoogleCalendarService | None = None,
    predictor: TaskScorePredictor | None = None,
    now: datetime | None = None,
) -> RollingReplanProposal | None:
    """Optimize pending and movable flexible tasks over the next seven days."""
    now = now or datetime.now(TIMEZONE)
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    predictor = predictor or train_and_predict()
    if user_id is None:
        if new_task_id is None:
            raise ValueError("需要 user_id 或 new_task_id。")
        with SessionLocal() as session:
            task = session.get(Task, new_task_id)
            if not task or not task.discord_user_id:
                return None
            user_id = int(task.discord_user_id)

    tasks, current = _proposal_tasks(user_id, now)
    if not tasks:
        return None
    slots: list[dict[str, datetime]] = []
    for offset in range(PLANNING_HORIZON_DAYS + 1):
        slots.extend(working_slots(calendar, now.date() + timedelta(days=offset), now=now))
    for intervals in current.values():
        slots.extend({"start": start, "end": end} for start, end in intervals)
    slots = _merge_slots(slots)
    selected = optimize_rolling_schedule(tasks, slots, predictor, current)
    if not selected:
        return None
    if new_task_id is not None and new_task_id not in selected:
        return None

    changes: list[RollingReplanChange] = []
    unchanged = 0
    for task in tasks:
        option: PlanOption | None = selected.get(task.id)
        if option is None:
            continue
        old = current.get(task.id, ())
        if old == option.intervals:
            unchanged += 1
            continue
        changes.append(
            RollingReplanChange(
                task_id=task.id,
                task_name=task.task_name,
                priority=int(task.priority or 0),
                old_intervals=_iso_intervals(old),
                new_intervals=_iso_intervals(option.intervals),
            )
        )
    if not changes:
        return None
    return RollingReplanProposal(new_task_id, tuple(changes), unchanged)


def _parse_intervals(values: list[list[str]] | tuple[tuple[str, str], ...]) -> tuple[tuple[datetime, datetime], ...]:
    return tuple(
        (datetime.fromisoformat(start), datetime.fromisoformat(end))
        for start, end in values
    )


def _event_ids(task: Task) -> list[str]:
    if task.segments:
        return [segment.event_id for segment in task.segments]
    return [task.event_id] if task.event_id else []


def apply_rolling_replan(
    proposal: dict,
    *,
    calendar: GoogleCalendarService | None = None,
) -> None:
    """Apply a previously previewed rolling proposal after stale-state checks."""
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    changes = list(proposal.get("changes") or [])
    ids = [int(change["task_id"]) for change in changes]
    with SessionLocal() as session:
        tasks = {
            task.id: task
            for task in session.scalars(
                select(Task)
                .where(Task.id.in_(ids))
                .options(selectinload(Task.segments))
            )
        }
        snapshots: dict[int, dict] = {}
        for change in changes:
            task = tasks.get(int(change["task_id"]))
            if not task:
                raise ValueError("重排內容已失效：找不到任務。")
            if task.is_fixed or task.is_locked:
                raise ValueError(f"「{task.task_name}」已固定或鎖定，請重新計算。")
            expected = _parse_intervals(change.get("old_intervals") or [])
            actual = _task_intervals(task)
            if actual != expected:
                raise ValueError("Calendar 或資料庫已有新變更，請重新計算排程。")
            snapshots[task.id] = {
                "task": task,
                "old_ids": _event_ids(task),
                "new": _parse_intervals(change["new_intervals"]),
            }

        final_ids: dict[int, list[str]] = {}
        created_ids: list[str] = []
        try:
            for task_id, snapshot in snapshots.items():
                task: Task = snapshot["task"]
                old_ids: list[str] = snapshot["old_ids"]
                intervals = snapshot["new"]
                count = len(intervals)
                if len(old_ids) == count and old_ids:
                    for index, (event_id, (start, end)) in enumerate(
                        zip(old_ids, intervals, strict=True), start=1
                    ):
                        title = task.task_name if count == 1 else f"{task.task_name}（{index}/{count}）"
                        calendar.update_event_time(event_id, start, end, title)
                    final_ids[task_id] = old_ids
                    continue
                new_ids: list[str] = []
                for index, (start, end) in enumerate(intervals, start=1):
                    title = task.task_name if count == 1 else f"{task.task_name}（{index}/{count}）"
                    event_id = calendar.create_event(title, start, end)
                    new_ids.append(event_id)
                    created_ids.append(event_id)
                for event_id in old_ids:
                    calendar.delete_event(event_id)
                final_ids[task_id] = new_ids
        except Exception:
            for event_id in created_ids:
                try:
                    calendar.delete_event(event_id)
                except Exception:
                    pass
            raise

        for task_id, snapshot in snapshots.items():
            task: Task = snapshot["task"]
            intervals = snapshot["new"]
            ids_for_task = final_ids[task_id]
            for segment in list(task.segments):
                session.delete(segment)
            task.event_id = ids_for_task[0] if len(intervals) == 1 else None
            if len(intervals) > 1:
                for index, ((start, end), event_id) in enumerate(
                    zip(intervals, ids_for_task, strict=True), start=1
                ):
                    session.add(
                        TaskSegment(
                            task_id=task.id,
                            segment_index=index,
                            scheduled_start=_db_time(start),
                            scheduled_end=_db_time(end),
                            event_id=event_id,
                        )
                    )
            task.status = "scheduled"
            task.scheduled_start = _db_time(intervals[0][0])
            task.scheduled_end = _db_time(intervals[-1][1])
            _clear_failure(task)
        session.commit()


def propose_same_day_replan(
    new_task_id: int,
    *,
    calendar: GoogleCalendarService | None = None,
    predictor: TaskScorePredictor | None = None,
    now: datetime | None = None,
) -> ReplanProposal | None:
    now = now or datetime.now(TIMEZONE)
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    predictor = predictor or train_and_predict()
    with SessionLocal() as session:
        new_task = session.get(Task, new_task_id)
        if not new_task or new_task.status != "pending":
            return None
        if new_task.deadline and _local(new_task.deadline).date() != now.date():
            return None
        movable = list(
            session.scalars(
                select(Task)
                .where(
                    Task.status == "scheduled",
                    Task.is_fixed.is_(False),
                    Task.is_locked.is_(False),
                    Task.scheduled_start.is_not(None),
                    Task.scheduled_start >= _db_time(now),
                    Task.scheduled_start < _db_time(now + timedelta(days=1)),
                )
                .order_by(Task.deadline, Task.id)
                .limit(MAX_REPLAN_MOVES)
            )
        )
        tasks = [new_task, *movable]
        for task in tasks:
            session.expunge(task)

    slots = working_slots(calendar, now.date(), now=now)
    slots.extend(
        {
            "start": _local(task.scheduled_start),
            "end": _local(task.scheduled_end),
        }
        for task in movable
        if task.scheduled_start and task.scheduled_end
    )
    decisions = optimize_daily_schedule(
        tasks, _merge_slots(slots), predictor, require_all=True
    )
    if len(decisions) != len(tasks):
        return None
    by_id = {decision.task_id: decision for decision in decisions}
    changes: list[ReplanChange] = []
    moved_count = 0
    for task in tasks:
        decision = by_id[task.id]
        old_start = _local(task.scheduled_start).isoformat() if task.scheduled_start else None
        old_end = _local(task.scheduled_end).isoformat() if task.scheduled_end else None
        if old_start and old_start != decision.start.isoformat():
            moved_count += 1
        changes.append(
            ReplanChange(
                task.id,
                task.task_name,
                old_start,
                old_end,
                decision.start.isoformat(),
                decision.end.isoformat(),
            )
        )
    if moved_count > MAX_REPLAN_MOVES:
        return None
    return ReplanProposal(new_task_id, tuple(changes))


def apply_replan(proposal: dict, *, calendar: GoogleCalendarService | None = None) -> None:
    if int(proposal.get("version") or 1) == 2:
        apply_rolling_replan(proposal, calendar=calendar)
        return
    calendar = calendar or GoogleCalendarService(timezone=str(TIMEZONE))
    changes = proposal.get("changes", [])
    with SessionLocal() as session:
        tasks = {task.id: task for task in session.scalars(select(Task).where(Task.id.in_([int(item["task_id"]) for item in changes])))}
        for item in changes:
            task = tasks.get(int(item["task_id"]))
            if not task:
                raise ValueError("重排內容已失效，請重新提出要求。")
            expected = item.get("old_start")
            actual = _local(task.scheduled_start).isoformat() if task.scheduled_start else None
            if actual != expected:
                raise ValueError("Calendar 已有新變更，請重新計算排程。")

        for item in changes:
            task = tasks[int(item["task_id"])]
            start = datetime.fromisoformat(item["new_start"])
            end = datetime.fromisoformat(item["new_end"])
            if task.event_id:
                calendar.update_event_time(task.event_id, start, end, task.task_name)
            else:
                task.event_id = calendar.create_event(task.task_name, start, end)
            task.status = "scheduled"
            task.scheduled_start = _db_time(start)
            task.scheduled_end = _db_time(end)
            _clear_failure(task)
        session.commit()


def failure_message(result: AttemptResult, task_name: str, deadline: datetime | None) -> str:
    details = json.loads(result.failure_details or "{}")
    deadline_text = deadline.strftime("%m/%d %H:%M") if deadline else "未設定"
    reason = {
        "no_free_slot": "沒有足夠的連續空檔",
        "deadline_too_close": "截止時間已到或過於接近",
        "not_pending": "任務目前不是待排程狀態",
    }.get(result.failure_reason or "", result.failure_reason or "未知原因")
    return (
        f"⚠️ 任務「{task_name}」目前無法排入\n"
        f"截止時間：{deadline_text}\n"
        f"需要：{details.get('required_minutes', '?')} 分鐘\n"
        f"最長連續空檔：{details.get('longest_free_minutes', 0)} 分鐘\n"
        f"原因：{reason}"
    )
