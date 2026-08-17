"""Persist task and per-segment feedback using the latest Calendar times."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from database import (
    Feedback,
    FeedbackDraft,
    SegmentFeedbackDraft,
    SessionLocal,
    Task,
    TaskSegment,
)
from gcal_service import GoogleCalendarService


logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class FeedbackResult:
    task_name: str
    efficiency: int
    event_ids: tuple[str, ...]
    task_status: str


def _local(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=TIMEZONE)
        if value.tzinfo is None
        else value.astimezone(TIMEZONE)
    )


def _db_time(value: datetime) -> datetime:
    return _local(value).replace(tzinfo=None)


def _event_times(
    calendar: GoogleCalendarService,
    event_id: str | None,
    fallback_start: datetime,
    fallback_end: datetime,
) -> tuple[datetime, datetime]:
    if not event_id:
        return fallback_start, fallback_end
    try:
        event = calendar.get_event(event_id)
        start_text = event.get("start", {}).get("dateTime")
        end_text = event.get("end", {}).get("dateTime")
        if start_text and end_text:
            return (
                _db_time(datetime.fromisoformat(start_text.replace("Z", "+00:00"))),
                _db_time(datetime.fromisoformat(end_text.replace("Z", "+00:00"))),
            )
    except Exception:
        logger.warning(
            "評分前無法讀取 Calendar 最新時間，使用已同步時間：event=%s",
            event_id,
            exc_info=True,
        )
    return fallback_start, fallback_end


def _existing_feedback(session, task_id: int, segment_id: int | None) -> Feedback | None:
    query = select(Feedback).where(Feedback.task_id == task_id)
    query = (
        query.where(Feedback.segment_id == segment_id)
        if segment_id is not None
        else query.where(Feedback.segment_id.is_(None))
    )
    return session.scalar(query)


def save_efficiency_draft(
    task_id: int,
    user_id: int,
    score: int,
    segment_id: int | None = None,
) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError("找不到任務。")
        if segment_id is None:
            if task.status != "feedback_requested":
                raise ValueError("此任務目前不能評分。")
            if _existing_feedback(session, task_id, None):
                raise ValueError("此任務已經完成評分。")
            draft = session.get(FeedbackDraft, task_id)
            if draft is None:
                draft = FeedbackDraft(
                    task_id=task_id,
                    discord_user_id=str(user_id),
                    efficiency_score=score,
                )
                session.add(draft)
            else:
                draft.efficiency_score = score
        else:
            segment = session.get(TaskSegment, segment_id)
            if (
                not segment
                or segment.task_id != task_id
                or segment.status != "feedback_requested"
            ):
                raise ValueError("此分段目前不能評分。")
            if _existing_feedback(session, task_id, segment_id):
                raise ValueError("此分段已經完成評分。")
            draft = session.get(SegmentFeedbackDraft, segment_id)
            if draft is None:
                draft = SegmentFeedbackDraft(
                    segment_id=segment_id,
                    discord_user_id=str(user_id),
                    efficiency_score=score,
                )
                session.add(draft)
            else:
                draft.efficiency_score = score
        session.commit()


def _parent_status(task: Task) -> str:
    statuses = {segment.status for segment in task.segments}
    if statuses & {"scheduled", "feedback_requested"}:
        return "scheduled"
    if statuses == {"completed"}:
        return "completed"
    if statuses == {"incomplete"}:
        return "incomplete"
    return "partially_completed"


def finalize_completed(
    task_id: int,
    mental: int,
    segment_id: int | None = None,
    calendar: GoogleCalendarService | None = None,
) -> FeedbackResult:
    calendar = calendar or GoogleCalendarService()
    now = _db_time(datetime.now(TIMEZONE))
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        segment = session.get(TaskSegment, segment_id) if segment_id else None
        if not task or (segment_id and (not segment or segment.task_id != task_id)):
            raise ValueError("找不到回饋目標。")
        if segment and segment.status != "feedback_requested":
            raise ValueError("此分段目前不能評分。")
        if segment is None and task.status != "feedback_requested":
            raise ValueError("此任務目前不能評分。")
        if segment is None:
            draft = session.get(FeedbackDraft, task_id)
            start = task.scheduled_start or now
            end = task.scheduled_end or now
            event_ids = tuple(
                value
                for value in [task.event_id, *(item.event_id for item in task.segments)]
                if value
            )
            event_id = task.event_id
        else:
            draft = session.get(SegmentFeedbackDraft, segment_id)
            start, end = segment.scheduled_start, segment.scheduled_end
            event_ids = (segment.event_id,)
            event_id = segment.event_id
        if not draft:
            raise ValueError("找不到尚未完成的效率評分。")
        if _existing_feedback(session, task_id, segment_id):
            raise ValueError("此項目已經完成評分。")
        efficiency = draft.efficiency_score

    start, end = _event_times(calendar, event_id, start, end)
    actual_minutes = max(1, int((end - start).total_seconds() // 60))
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        segment = session.get(TaskSegment, segment_id) if segment_id else None
        if not task or (segment_id and not segment):
            raise ValueError("回饋目標已不存在。")
        draft = (
            session.get(SegmentFeedbackDraft, segment_id)
            if segment_id
            else session.get(FeedbackDraft, task_id)
        )
        if not draft or _existing_feedback(session, task_id, segment_id):
            raise ValueError("此項目已經完成評分。")
        session.add(
            Feedback(
                task_id=task.id,
                segment_id=segment_id,
                actual_minutes=actual_minutes,
                scheduled_start=start,
                scheduled_end=end,
                time_of_day=_local(start).strftime("%H:%M"),
                efficiency_score=draft.efficiency_score,
                mental_score=mental,
                completion_status="completed",
                rating_method="segment_two_stage" if segment else "two_stage",
            )
        )
        if segment:
            segment.scheduled_start, segment.scheduled_end = start, end
            segment.status = "completed"
            task.scheduled_start = min(item.scheduled_start for item in task.segments)
            task.scheduled_end = max(item.scheduled_end for item in task.segments)
            task.status = _parent_status(task)
        else:
            task.scheduled_start, task.scheduled_end = start, end
            task.status = "completed"
        task_status = task.status
        name = task.task_name
        session.delete(draft)
        session.commit()

    for event in event_ids:
        calendar.update_event_feedback(event, efficiency, mental)
    return FeedbackResult(name, efficiency, event_ids, task_status)


def finalize_incomplete(
    task_id: int,
    reason: str | None,
    segment_id: int | None = None,
    calendar: GoogleCalendarService | None = None,
) -> FeedbackResult:
    calendar = calendar or GoogleCalendarService()
    now = _db_time(datetime.now(TIMEZONE))
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        segment = session.get(TaskSegment, segment_id) if segment_id else None
        if not task or (segment_id and (not segment or segment.task_id != task_id)):
            raise ValueError("找不到回饋目標。")
        if segment and segment.status != "feedback_requested":
            raise ValueError("此分段目前不能評分。")
        if segment is None and task.status != "feedback_requested":
            raise ValueError("此任務目前不能評分。")
        if _existing_feedback(session, task_id, segment_id):
            raise ValueError("此項目已經完成回饋。")
        start = segment.scheduled_start if segment else (task.scheduled_start or now)
        end = segment.scheduled_end if segment else (task.scheduled_end or now)
        event_ids = (
            (segment.event_id,)
            if segment
            else tuple(
                value
                for value in [task.event_id, *(item.event_id for item in task.segments)]
                if value
            )
        )
        event_id = segment.event_id if segment else task.event_id

    start, end = _event_times(calendar, event_id, start, end)
    actual_minutes = max(1, int((end - start).total_seconds() // 60))
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        segment = session.get(TaskSegment, segment_id) if segment_id else None
        if not task or (segment_id and not segment):
            raise ValueError("回饋目標已不存在。")
        if _existing_feedback(session, task_id, segment_id):
            raise ValueError("此項目已經完成回饋。")
        session.add(
            Feedback(
                task_id=task.id,
                segment_id=segment_id,
                actual_minutes=actual_minutes,
                scheduled_start=start,
                scheduled_end=end,
                time_of_day=_local(start).strftime("%H:%M"),
                efficiency_score=0,
                mental_score=1,
                completion_status="incomplete",
                incomplete_reason=reason,
                rating_method="segment_incomplete" if segment else "incomplete",
            )
        )
        if segment:
            segment.scheduled_start, segment.scheduled_end = start, end
            segment.status = "incomplete"
            draft = session.get(SegmentFeedbackDraft, segment_id)
            task.scheduled_start = min(item.scheduled_start for item in task.segments)
            task.scheduled_end = max(item.scheduled_end for item in task.segments)
            task.status = _parent_status(task)
        else:
            task.scheduled_start, task.scheduled_end = start, end
            task.status = "incomplete"
            draft = session.get(FeedbackDraft, task.id)
        if draft:
            session.delete(draft)
        task_status = task.status
        name = task.task_name
        session.commit()

    for event in event_ids:
        calendar.update_event_incomplete(event, reason)
    return FeedbackResult(name, 0, event_ids, task_status)
