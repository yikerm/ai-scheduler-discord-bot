"""Progressive personal feedback prediction and CP-SAT scheduling."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol

import numpy as np
from ortools.sat.python import cp_model
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from config import ML_FULL_FEEDBACK, ML_PROGRESSIVE_MIN_FEEDBACK
from database import Feedback, SessionLocal, Task
from task_features import CATEGORY_KEYS, infer_task_category, normalize_task_name


DEFAULT_SCORE = 3.0
DEFAULT_COMPLETION = 0.8
COMPLETION_VALUE_WEIGHT = 2.0
MIN_NAME_FEEDBACK = 3
SLOT_GRANULARITY_MINUTES = 30


class TaskLike(Protocol):
    id: int
    estimated_minutes: int
    deadline: datetime | None
    available_from: datetime | None
    priority: int


@dataclass(frozen=True)
class ScorePrediction:
    efficiency: float
    mental: float
    completion_probability: float = DEFAULT_COMPLETION

    @property
    def total(self) -> float:
        return (
            self.efficiency
            + self.mental
            + COMPLETION_VALUE_WEIGHT * self.completion_probability
        )


@dataclass(frozen=True)
class ScheduleDecision:
    task_id: int
    start: datetime
    end: datetime
    prediction: ScorePrediction


@dataclass
class TaskScorePredictor:
    efficiency_model: RandomForestRegressor | None
    mental_model: RandomForestRegressor | None
    completion_model: RandomForestClassifier | None = None
    default_efficiency: float = DEFAULT_SCORE
    default_mental: float = DEFAULT_SCORE
    default_completion: float = DEFAULT_COMPLETION
    blend_factor: float = 0.0
    valid_feedback_count: int = 0
    known_task_names: tuple[str, ...] = ()

    def predict(
        self,
        estimated_minutes: int,
        candidate_start: datetime,
        *,
        task_name: str | None = None,
        task_category: str | None = None,
        parent_total_minutes: int | None = None,
        segment_index: int = 1,
        segment_count: int = 1,
        break_before_minutes: int = 0,
        prior_task_minutes: int = 0,
    ) -> ScorePrediction:
        features = np.array(
            [
                _features(
                    estimated_minutes,
                    candidate_start,
                    task_name=task_name,
                    task_category=task_category,
                    parent_total_minutes=parent_total_minutes,
                    segment_index=segment_index,
                    segment_count=segment_count,
                    break_before_minutes=break_before_minutes,
                    prior_task_minutes=prior_task_minutes,
                    known_task_names=self.known_task_names,
                )
            ]
        )
        model_efficiency = (
            float(self.efficiency_model.predict(features)[0])
            if self.efficiency_model is not None
            else self.default_efficiency
        )
        model_mental = (
            float(self.mental_model.predict(features)[0])
            if self.mental_model is not None
            else self.default_mental
        )
        if self.completion_model is None:
            model_completion = self.default_completion
        else:
            probabilities = self.completion_model.predict_proba(features)[0]
            class_probabilities = dict(
                zip(self.completion_model.classes_, probabilities, strict=True)
            )
            model_completion = float(class_probabilities.get(1, 0.0))
        efficiency = (
            self.default_efficiency * (1 - self.blend_factor)
            + model_efficiency * self.blend_factor
        )
        mental = (
            self.default_mental * (1 - self.blend_factor)
            + model_mental * self.blend_factor
        )
        completion = (
            self.default_completion * (1 - self.blend_factor)
            + model_completion * self.blend_factor
        )
        return ScorePrediction(
            efficiency=float(np.clip(efficiency, 0.0, 5.0)),
            mental=float(np.clip(mental, 1.0, 5.0)),
            completion_probability=float(np.clip(completion, 0.0, 1.0)),
        )


def _features(
    estimated_minutes: int,
    candidate_start: datetime,
    *,
    task_name: str | None = None,
    task_category: str | None = None,
    parent_total_minutes: int | None = None,
    segment_index: int = 1,
    segment_count: int = 1,
    break_before_minutes: int = 0,
    prior_task_minutes: int = 0,
    known_task_names: tuple[str, ...] = (),
) -> list[float]:
    minutes_of_day = candidate_start.hour * 60 + candidate_start.minute
    day_angle = 2 * np.pi * minutes_of_day / (24 * 60)
    week_angle = 2 * np.pi * candidate_start.weekday() / 7
    category = task_category or infer_task_category(task_name)
    name_key = normalize_task_name(task_name)
    return [
        float(estimated_minutes),
        float(parent_total_minutes or estimated_minutes),
        float(np.sin(day_angle)),
        float(np.cos(day_angle)),
        float(np.sin(week_angle)),
        float(np.cos(week_angle)),
        float(max(1, segment_index)),
        float(max(1, segment_count)),
        float(max(0, min(24 * 60, break_before_minutes))),
        float(max(0, prior_task_minutes)),
        *(1.0 if category == value else 0.0 for value in CATEGORY_KEYS),
        *(1.0 if name_key == value else 0.0 for value in known_task_names),
    ]


def _row_weight(row: Feedback) -> float:
    if row.rating_method == "legacy_overall":
        return 0.25
    if row.completion_status != "incomplete":
        return 1.0
    if row.incomplete_reason in {"臨時事件中斷", "優先度改變"}:
        return 0.2
    if row.incomplete_reason == "時間估計不足":
        return 0.6
    if not row.incomplete_reason:
        return 0.5
    return 1.0


def _row_context(row: Feedback) -> dict[str, int | str]:
    task = row.task
    segment = next(
        (item for item in task.segments if item.id == row.segment_id), None
    )
    ordered = sorted(task.segments, key=lambda item: item.segment_index)
    if segment:
        prior = [item for item in ordered if item.segment_index < segment.segment_index]
        previous = prior[-1] if prior else None
        derived_break = (
            max(0, int((row.scheduled_start - previous.scheduled_end).total_seconds() // 60))
            if previous
            else 0
        )
        derived_prior = sum(
            max(1, int((item.scheduled_end - item.scheduled_start).total_seconds() // 60))
            for item in prior
        )
    else:
        derived_break = 0
        derived_prior = 0
    return {
        "task_name": row.task_name_key or task.task_name,
        "task_category": row.task_category or infer_task_category(task.task_name),
        "parent_total_minutes": row.parent_total_minutes or task.estimated_minutes,
        "segment_index": row.segment_index or (segment.segment_index if segment else 1),
        "segment_count": row.segment_count or (len(ordered) if segment else 1),
        "break_before_minutes": (
            row.break_before_minutes
            if row.break_before_minutes is not None
            else derived_break
        ),
        "prior_task_minutes": (
            row.prior_task_minutes
            if row.prior_task_minutes is not None
            else derived_prior
        ),
    }


def train_and_predict() -> TaskScorePredictor:
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(Feedback)
                .options(joinedload(Feedback.task).joinedload(Task.segments))
                .where(Feedback.task_id.is_not(None))
            ).unique()
        )
    rows = [row for row in rows if row.task is not None]
    if not rows:
        return TaskScorePredictor(None, None)

    valid_count = sum(row.rating_method != "legacy_overall" for row in rows)
    default_efficiency = float(
        np.clip(np.average([row.efficiency_score for row in rows]), 0.0, 5.0)
    )
    default_mental = float(
        np.clip(np.average([row.mental_score for row in rows]), 1.0, 5.0)
    )
    default_completion = float(
        np.mean([row.completion_status != "incomplete" for row in rows])
    )
    name_counts = Counter(
        row.task_name_key or normalize_task_name(row.task.task_name) for row in rows
    )
    known_names = tuple(
        sorted(name for name, count in name_counts.items() if count >= MIN_NAME_FEEDBACK)
    )
    defaults = dict(
        default_efficiency=default_efficiency,
        default_mental=default_mental,
        default_completion=default_completion,
        valid_feedback_count=valid_count,
        known_task_names=known_names,
    )
    if valid_count < ML_PROGRESSIVE_MIN_FEEDBACK:
        return TaskScorePredictor(None, None, **defaults)

    features = np.array(
        [
            _features(
                row.actual_minutes or row.task.estimated_minutes,
                row.scheduled_start,
                known_task_names=known_names,
                **_row_context(row),
            )
            for row in rows
        ]
    )
    efficiency = np.array([row.efficiency_score for row in rows], dtype=float)
    mental = np.array([row.mental_score for row in rows], dtype=float)
    completion = np.array(
        [row.completion_status != "incomplete" for row in rows], dtype=int
    )
    weights = np.array([_row_weight(row) for row in rows], dtype=float)
    efficiency_model = RandomForestRegressor(n_estimators=200, random_state=42)
    mental_model = RandomForestRegressor(n_estimators=200, random_state=42)
    efficiency_model.fit(features, efficiency, sample_weight=weights)
    mental_model.fit(features, mental, sample_weight=weights)
    completion_model: RandomForestClassifier | None = None
    if len(set(completion)) > 1:
        completion_model = RandomForestClassifier(
            n_estimators=200, random_state=42, class_weight="balanced"
        )
        completion_model.fit(features, completion, sample_weight=weights)
    denominator = max(1, ML_FULL_FEEDBACK - ML_PROGRESSIVE_MIN_FEEDBACK)
    blend = min(1.0, (valid_count - ML_PROGRESSIVE_MIN_FEEDBACK) / denominator)
    return TaskScorePredictor(
        efficiency_model,
        mental_model,
        completion_model,
        blend_factor=blend,
        **defaults,
    )


def predict_task_score(
    estimated_minutes: int,
    candidate_start: datetime,
    predictor: TaskScorePredictor | None = None,
    **context,
) -> ScorePrediction:
    return (predictor or train_and_predict()).predict(
        estimated_minutes, candidate_start, **context
    )


def _task_importance(task: TaskLike, candidate_start: datetime) -> int:
    priority = max(0, int(getattr(task, "priority", 0)))
    if task.deadline is None:
        urgency = 0
    else:
        deadline = task.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=candidate_start.tzinfo)
        hours = max(0, int((deadline - candidate_start).total_seconds() // 3600))
        urgency = max(0, 7 * 24 - min(7 * 24, hours))
    return 100_000 + priority * 1_000_000 + urgency * 20_000


def optimize_daily_schedule(
    tasks: Iterable[TaskLike],
    free_slots: list[dict[str, datetime]],
    predictor: TaskScorePredictor,
    *,
    require_all: bool = False,
) -> list[ScheduleDecision]:
    model = cp_model.CpModel()
    task_list = list(tasks)
    candidates: list[
        tuple[
            TaskLike,
            datetime,
            datetime,
            ScorePrediction,
            cp_model.IntVar,
            cp_model.IntervalVar,
        ]
    ] = []
    task_choices: dict[int, list[cp_model.IntVar]] = {}
    day_origin = min((slot["start"] for slot in free_slots), default=None)
    if day_origin is None:
        return []

    for task in task_list:
        duration = timedelta(minutes=task.estimated_minutes)
        for slot in free_slots:
            start = slot["start"]
            while start + duration <= slot["end"]:
                end = start + duration
                deadline = task.deadline
                available_from = task.available_from
                if deadline is not None and deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=start.tzinfo)
                if available_from is not None and available_from.tzinfo is None:
                    available_from = available_from.replace(tzinfo=start.tzinfo)
                if (available_from is None or start >= available_from) and (
                    deadline is None or end <= deadline
                ):
                    selected = model.NewBoolVar(f"task_{task.id}_{start:%H%M}")
                    offset = int((start - day_origin).total_seconds() // 60)
                    interval = model.NewOptionalIntervalVar(
                        offset,
                        task.estimated_minutes,
                        offset + task.estimated_minutes,
                        selected,
                        f"interval_{task.id}_{offset}",
                    )
                    prediction = predictor.predict(
                        task.estimated_minutes,
                        start,
                        task_name=getattr(task, "task_name", None),
                        parent_total_minutes=task.estimated_minutes,
                    )
                    candidates.append(
                        (task, start, end, prediction, selected, interval)
                    )
                    task_choices.setdefault(task.id, []).append(selected)
                start += timedelta(minutes=SLOT_GRANULARITY_MINUTES)

    if not candidates:
        return []
    if require_all and any(task.id not in task_choices for task in task_list):
        return []
    for task in task_list:
        choices = task_choices.get(task.id, [])
        if not choices:
            continue
        if require_all:
            model.AddExactlyOne(choices)
        else:
            model.AddAtMostOne(choices)
    model.AddNoOverlap([value[5] for value in candidates])
    model.Maximize(
        sum(
            (
                _task_importance(task, start)
                + int(round(prediction.total * 100))
            )
            * selected
            for task, start, _end, prediction, selected, _interval in candidates
        )
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return []
    return [
        ScheduleDecision(task.id, start, end, prediction)
        for task, start, end, prediction, selected, _interval in candidates
        if solver.Value(selected)
    ]
