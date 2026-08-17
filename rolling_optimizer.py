"""Candidate-plan CP-SAT optimizer for a rolling multi-day schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol

from ortools.sat.python import cp_model

from config import MAX_REPLAN_MOVES
from ml_engine import TaskScorePredictor


SLOT_GRANULARITY_MINUTES = 30
MAX_CONTIGUOUS_OPTIONS = 36
MAX_SPLIT_OPTIONS = 16
MAX_SEGMENTS = 4
STABILITY_BONUS = 50_000


class RollingTask(Protocol):
    id: int
    estimated_minutes: int
    deadline: datetime | None
    available_from: datetime | None
    priority: int
    allow_split: bool
    min_segment_minutes: int
    status: str


@dataclass(frozen=True)
class PlanOption:
    task_id: int
    intervals: tuple[tuple[datetime, datetime], ...]
    objective: int
    is_current: bool = False


def _aware(value: datetime | None, reference: datetime) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=reference.tzinfo) if value.tzinfo is None else value


def _clip_slots(
    task: RollingTask,
    slots: list[dict[str, datetime]],
) -> list[dict[str, datetime]]:
    clipped: list[dict[str, datetime]] = []
    for slot in slots:
        start, end = slot["start"], slot["end"]
        available = _aware(task.available_from, start)
        deadline = _aware(task.deadline, start)
        if available:
            start = max(start, available)
        if deadline:
            end = min(end, deadline)
        if start < end:
            clipped.append({"start": start, "end": end})
    return clipped


def _prediction_value(
    task: RollingTask,
    intervals: tuple[tuple[datetime, datetime], ...],
    predictor: TaskScorePredictor,
) -> int:
    weighted = 0.0
    total = 0
    for start, end in intervals:
        minutes = int((end - start).total_seconds() // 60)
        weighted += predictor.predict(minutes, start).total * minutes
        total += minutes
    return int(round((weighted / max(1, total)) * 1_000))


def _urgency_value(task: RollingTask, planning_start: datetime) -> int:
    deadline = _aware(task.deadline, planning_start)
    if not deadline:
        return 0
    hours = max(0, int((deadline - planning_start).total_seconds() // 3600))
    return max(0, 7 * 24 - min(7 * 24, hours)) * 20_000


def _objective(
    task: RollingTask,
    intervals: tuple[tuple[datetime, datetime], ...],
    predictor: TaskScorePredictor,
    *,
    is_current: bool,
    planning_start: datetime,
) -> int:
    return (
        10_000_000
        + max(0, int(task.priority)) * 2_000_000
        + _urgency_value(task, planning_start)
        + _prediction_value(task, intervals, predictor)
        + (STABILITY_BONUS if is_current else 0)
        - max(0, len(intervals) - 1) * 300
    )


def _contiguous_options(
    task: RollingTask,
    slots: list[dict[str, datetime]],
    predictor: TaskScorePredictor,
) -> list[PlanOption]:
    duration = timedelta(minutes=task.estimated_minutes)
    planning_start = min(slot["start"] for slot in slots)
    values: list[PlanOption] = []
    for slot in _clip_slots(task, slots):
        start = slot["start"]
        while start + duration <= slot["end"]:
            intervals = ((start, start + duration),)
            values.append(
                PlanOption(
                    task.id,
                    intervals,
                    _objective(task, intervals, predictor, is_current=False, planning_start=planning_start),
                )
            )
            start += timedelta(minutes=SLOT_GRANULARITY_MINUTES)
    return sorted(values, key=lambda item: item.objective, reverse=True)[
        :MAX_CONTIGUOUS_OPTIONS
    ]


def _allocate_split(
    ordered_slots: list[dict[str, datetime]],
    total_minutes: int,
    minimum: int,
) -> tuple[tuple[datetime, datetime], ...] | None:
    if total_minutes < 2 * minimum:
        return None
    remaining = total_minutes
    intervals: list[tuple[datetime, datetime]] = []
    for slot in ordered_slots:
        capacity = int((slot["end"] - slot["start"]).total_seconds() // 60)
        if capacity < minimum:
            continue
        if not intervals:
            take = min(capacity, remaining - minimum)
        else:
            take = remaining if remaining <= capacity else min(capacity, remaining - minimum)
        if take < minimum:
            continue
        intervals.append((slot["start"], slot["start"] + timedelta(minutes=take)))
        remaining -= take
        if remaining == 0:
            result = tuple(sorted(intervals))
            return result if 2 <= len(result) <= MAX_SEGMENTS else None
        if len(intervals) >= MAX_SEGMENTS:
            return None
    return None


def _split_options(
    task: RollingTask,
    slots: list[dict[str, datetime]],
    predictor: TaskScorePredictor,
) -> list[PlanOption]:
    if not task.allow_split:
        return []
    minimum = max(1, int(task.min_segment_minutes or 30))
    planning_start = min(slot["start"] for slot in slots)
    eligible = [
        slot
        for slot in _clip_slots(task, slots)
        if int((slot["end"] - slot["start"]).total_seconds() // 60) >= minimum
    ]
    if len(eligible) < 2:
        return []
    orders: list[list[dict[str, datetime]]] = [eligible]
    orders.append(
        sorted(
            eligible,
            key=lambda slot: predictor.predict(
                min(
                    task.estimated_minutes,
                    int((slot["end"] - slot["start"]).total_seconds() // 60),
                ),
                slot["start"],
            ).total,
            reverse=True,
        )
    )
    orders.extend(eligible[index:] + eligible[:index] for index in range(1, min(len(eligible), 8)))
    unique: dict[tuple[tuple[datetime, datetime], ...], PlanOption] = {}
    for order in orders:
        intervals = _allocate_split(
            order, task.estimated_minutes, minimum
        )
        if not intervals:
            continue
        unique[intervals] = PlanOption(
            task.id,
            intervals,
            _objective(task, intervals, predictor, is_current=False, planning_start=planning_start),
        )
    return sorted(unique.values(), key=lambda item: item.objective, reverse=True)[
        :MAX_SPLIT_OPTIONS
    ]


def build_task_options(
    task: RollingTask,
    slots: list[dict[str, datetime]],
    predictor: TaskScorePredictor,
    current_intervals: tuple[tuple[datetime, datetime], ...] = (),
) -> list[PlanOption]:
    options = _contiguous_options(task, slots, predictor)
    options.extend(_split_options(task, slots, predictor))
    if current_intervals:
        planning_start = min(slot["start"] for slot in slots)
        current = PlanOption(
            task.id,
            current_intervals,
            _objective(task, current_intervals, predictor, is_current=True, planning_start=planning_start),
            True,
        )
        options.append(current)
    unique: dict[tuple[tuple[datetime, datetime], ...], PlanOption] = {}
    for option in options:
        previous = unique.get(option.intervals)
        if previous is None or option.objective > previous.objective:
            unique[option.intervals] = option
    return list(unique.values())


def optimize_rolling_schedule(
    tasks: Iterable[RollingTask],
    slots: list[dict[str, datetime]],
    predictor: TaskScorePredictor,
    current_by_task: dict[int, tuple[tuple[datetime, datetime], ...]],
) -> dict[int, PlanOption]:
    task_list = list(tasks)
    if not task_list or not slots:
        return {}
    origin = min(slot["start"] for slot in slots)
    model = cp_model.CpModel()
    option_rows: list[tuple[PlanOption, cp_model.IntVar]] = []
    intervals: list[cp_model.IntervalVar] = []
    moved_existing: list[cp_model.IntVar] = []

    for task in task_list:
        task_options = build_task_options(
            task, slots, predictor, current_by_task.get(task.id, ())
        )
        choices: list[cp_model.IntVar] = []
        current_choice: cp_model.IntVar | None = None
        for index, option in enumerate(task_options):
            selected = model.NewBoolVar(f"task_{task.id}_plan_{index}")
            choices.append(selected)
            option_rows.append((option, selected))
            if option.is_current:
                current_choice = selected
            for segment_index, (start, end) in enumerate(option.intervals):
                offset = int((start - origin).total_seconds() // 60)
                duration = int((end - start).total_seconds() // 60)
                intervals.append(
                    model.NewOptionalIntervalVar(
                        offset,
                        duration,
                        offset + duration,
                        selected,
                        f"task_{task.id}_plan_{index}_segment_{segment_index}",
                    )
                )
        if not choices:
            continue
        if task.status == "scheduled":
            model.AddExactlyOne(choices)
            if current_choice is not None:
                moved = model.NewBoolVar(f"task_{task.id}_moved")
                model.Add(moved + current_choice == 1)
                moved_existing.append(moved)
        else:
            model.AddAtMostOne(choices)

    if not option_rows:
        return {}
    if moved_existing:
        model.Add(sum(moved_existing) <= MAX_REPLAN_MOVES)
    model.AddNoOverlap(intervals)
    model.Maximize(sum(option.objective * selected for option, selected in option_rows))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 15
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {}
    return {
        option.task_id: option
        for option, selected in option_rows
        if solver.Value(selected)
    }
