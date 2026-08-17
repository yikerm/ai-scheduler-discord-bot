"""Progressive feedback prediction and deadline-aware CP-SAT scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol

import numpy as np
from ortools.sat.python import cp_model
from sklearn.ensemble import RandomForestRegressor
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from config import ML_FULL_FEEDBACK, ML_PROGRESSIVE_MIN_FEEDBACK
from database import Feedback, SessionLocal


DEFAULT_SCORE = 3.0
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

    @property
    def total(self) -> float:
        return self.efficiency + self.mental


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
    default_efficiency: float = DEFAULT_SCORE
    default_mental: float = DEFAULT_SCORE
    blend_factor: float = 0.0
    valid_feedback_count: int = 0

    def predict(self, estimated_minutes: int, candidate_start: datetime) -> ScorePrediction:
        features = np.array([_features(estimated_minutes, candidate_start)])
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
        efficiency = (
            self.default_efficiency * (1 - self.blend_factor)
            + model_efficiency * self.blend_factor
        )
        mental = (
            self.default_mental * (1 - self.blend_factor)
            + model_mental * self.blend_factor
        )
        return ScorePrediction(
            efficiency=float(np.clip(efficiency, 0.0, 5.0)),
            mental=float(np.clip(mental, 1.0, 5.0)),
        )


def _features(estimated_minutes: int, candidate_start: datetime) -> list[float]:
    minutes_of_day = candidate_start.hour * 60 + candidate_start.minute
    day_angle = 2 * np.pi * minutes_of_day / (24 * 60)
    week_angle = 2 * np.pi * candidate_start.weekday() / 7
    return [
        float(estimated_minutes),
        float(np.sin(day_angle)),
        float(np.cos(day_angle)),
        float(np.sin(week_angle)),
        float(np.cos(week_angle)),
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


def train_and_predict() -> TaskScorePredictor:
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(Feedback)
                .options(joinedload(Feedback.task))
                .where(Feedback.task_id.is_not(None))
            )
        )
    rows = [row for row in rows if row.task is not None]
    if not rows:
        return TaskScorePredictor(None, None)

    valid_count = sum(row.rating_method != "legacy_overall" for row in rows)
    generic_scores = np.array(
        [(row.efficiency_score + row.mental_score) / 2 for row in rows], dtype=float
    )
    default = float(np.clip(generic_scores.mean(), 1.0, 5.0))
    if valid_count < ML_PROGRESSIVE_MIN_FEEDBACK:
        return TaskScorePredictor(
            None,
            None,
            default_efficiency=default,
            default_mental=default,
            valid_feedback_count=valid_count,
        )

    features = np.array(
        [_features(row.actual_minutes or row.task.estimated_minutes, row.scheduled_start) for row in rows]
    )
    efficiency = np.array([row.efficiency_score for row in rows], dtype=float)
    mental = np.array([row.mental_score for row in rows], dtype=float)
    weights = np.array([_row_weight(row) for row in rows], dtype=float)
    efficiency_model = RandomForestRegressor(n_estimators=200, random_state=42)
    mental_model = RandomForestRegressor(n_estimators=200, random_state=42)
    efficiency_model.fit(features, efficiency, sample_weight=weights)
    mental_model.fit(features, mental, sample_weight=weights)
    denominator = max(1, ML_FULL_FEEDBACK - ML_PROGRESSIVE_MIN_FEEDBACK)
    blend = min(1.0, (valid_count - ML_PROGRESSIVE_MIN_FEEDBACK) / denominator)
    return TaskScorePredictor(
        efficiency_model,
        mental_model,
        default_efficiency=default,
        default_mental=default,
        blend_factor=blend,
        valid_feedback_count=valid_count,
    )


def predict_task_score(
    estimated_minutes: int,
    candidate_start: datetime,
    predictor: TaskScorePredictor | None = None,
) -> ScorePrediction:
    return (predictor or train_and_predict()).predict(
        estimated_minutes, candidate_start
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
                    selected = model.NewBoolVar(
                        f"task_{task.id}_{start:%H%M}"
                    )
                    offset = int((start - day_origin).total_seconds() // 60)
                    interval = model.NewOptionalIntervalVar(
                        offset,
                        task.estimated_minutes,
                        offset + task.estimated_minutes,
                        selected,
                        f"interval_{task.id}_{offset}",
                    )
                    prediction = predictor.predict(task.estimated_minutes, start)
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
