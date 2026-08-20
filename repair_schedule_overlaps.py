"""Preview or repair future overlaps between Bot-owned contiguous tasks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import PLANNING_HORIZON_DAYS
from database import SessionLocal, Task
from gcal_service import GoogleCalendarService
from ml_engine import optimize_daily_schedule, train_and_predict
from planning_service import _local, _subtract_busy_intervals, working_slots


TIMEZONE = ZoneInfo("Asia/Taipei")
ACTIVE = {"scheduled", "feedback_requested"}


@dataclass(frozen=True)
class Move:
    task_id: int
    task_name: str
    old_start: datetime
    old_end: datetime
    new_start: datetime
    new_end: datetime


def _db_time(value: datetime) -> datetime:
    return _local(value).replace(tzinfo=None)


def _future_tasks(now: datetime) -> list[Task]:
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .where(
                    Task.status.in_(ACTIVE),
                    Task.event_id.is_not(None),
                    Task.scheduled_end > _db_time(now),
                )
                .options(selectinload(Task.segments))
                .order_by(Task.scheduled_start, Task.id)
            )
        )
        for task in tasks:
            session.expunge(task)
        return tasks


def _repair_candidates(tasks: list[Task]) -> tuple[list[Task], list[tuple[int, int]]]:
    selected: dict[int, Task] = {}
    blocked: list[tuple[int, int]] = []
    for index, first in enumerate(tasks):
        first_start, first_end = _local(first.scheduled_start), _local(first.scheduled_end)
        for second in tasks[index + 1 :]:
            second_start, second_end = _local(second.scheduled_start), _local(second.scheduled_end)
            if second_start >= first_end:
                break
            if first_start >= second_end:
                continue
            movable = [
                task
                for task in (first, second)
                if not task.is_fixed and not task.is_locked and not task.segments
            ]
            if not movable:
                blocked.append((first.id, second.id))
                continue
            candidate = max(movable, key=lambda task: task.id)
            selected[candidate.id] = candidate
    return sorted(selected.values(), key=lambda task: task.id), blocked


def propose_moves(
    *, calendar: GoogleCalendarService, now: datetime
) -> tuple[list[Move], list[int], list[tuple[int, int]]]:
    tasks = _future_tasks(now)
    candidates, blocked = _repair_candidates(tasks)
    predictor = train_and_predict()
    reserved: list[tuple[datetime, datetime]] = []
    moves: list[Move] = []
    unresolved: list[int] = []
    for task in candidates:
        first_day = max(
            now.date(),
            _local(task.available_from).date() if task.available_from else now.date(),
        )
        last_day = now.date() + timedelta(days=PLANNING_HORIZON_DAYS)
        if task.deadline:
            last_day = min(last_day, _local(task.deadline).date())
        decision = None
        for offset in range(max(0, (last_day - first_day).days) + 1):
            day = first_day + timedelta(days=offset)
            slots = _subtract_busy_intervals(
                working_slots(calendar, day, now=now), reserved
            )
            choices = optimize_daily_schedule(
                [task], slots, predictor, require_all=True
            )
            if choices:
                decision = choices[0]
                break
        if not decision:
            unresolved.append(task.id)
            continue
        move = Move(
            task.id,
            task.task_name,
            _local(task.scheduled_start),
            _local(task.scheduled_end),
            decision.start,
            decision.end,
        )
        moves.append(move)
        reserved.append((decision.start, decision.end))
    return moves, unresolved, blocked


def apply_moves(moves: list[Move], calendar: GoogleCalendarService) -> None:
    for move in moves:
        with SessionLocal() as session:
            task = session.get(Task, move.task_id)
            if (
                not task
                or task.status not in ACTIVE
                or not task.event_id
                or _local(task.scheduled_start) != move.old_start
                or _local(task.scheduled_end) != move.old_end
            ):
                raise RuntimeError(f"任務 {move.task_id} 已變更，已停止修復。")
            calendar.update_event_time(
                task.event_id, move.new_start, move.new_end, task.task_name
            )
            task.scheduled_start = _db_time(move.new_start)
            task.scheduled_end = _db_time(move.new_end)
            session.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="套用預覽結果；省略時只讀預覽"
    )
    args = parser.parse_args()
    now = datetime.now(TIMEZONE)
    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
    moves, unresolved, blocked = propose_moves(calendar=calendar, now=now)
    print("重疊修復預覽：")
    for move in moves:
        print(
            f"- ID {move.task_id} {move.task_name}: "
            f"{move.old_start:%m/%d %H:%M}–{move.old_end:%H:%M} -> "
            f"{move.new_start:%m/%d %H:%M}–{move.new_end:%H:%M}"
        )
    if unresolved:
        print("無可用新時段：", ", ".join(map(str, unresolved)))
    if blocked:
        print("固定或鎖定衝突：", ", ".join(f"{a}/{b}" for a, b in blocked))
    if not moves:
        print("沒有可修復的未來重疊。")
    if args.apply and moves:
        apply_moves(moves, calendar)
        print(f"已完成 {len(moves)} 筆改期。")
    else:
        print("預覽完成：未修改資料庫或 Google Calendar。")


if __name__ == "__main__":
    main()
