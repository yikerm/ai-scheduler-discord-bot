"""Background Calendar sync, multi-day planning, failure alerts, and feedback."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from bot import bot as discord_bot, send_feedback_request, send_recurrence_extension_request, send_rolling_replan_request, send_schedule_failure_notification, send_segment_feedback_request
from config import (
    DISCORD_USER_ID,
    SCHEDULE_NOTIFICATION_COOLDOWN_HOURS,
)
from database import (
    CalendarSyncState,
    Feedback,
    RecurrenceRule,
    SessionLocal,
    Task,
    TaskSegment,
)
from gcal_service import GoogleCalendarService
from planning_service import AttemptResult, apply_replan, clear_outside_horizon_failures, failure_message, propose_rolling_replan, schedule_all_pending
from recurrence_service import maintain_rules, mark_awaiting_extension, rules_due_for_extension


logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Taipei")


def _local(value: datetime) -> datetime:
    return value.replace(tzinfo=TIMEZONE) if value.tzinfo is None else value.astimezone(TIMEZONE)


def _db_time(value: datetime) -> datetime:
    return _local(value).replace(tzinfo=None)


def _calendar_datetime(payload: dict[str, str]) -> datetime | None:
    value = payload.get("dateTime")
    return _db_time(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else None


def sync_calendar_tasks() -> dict[str, int]:
    """Apply incremental Calendar changes; manual moves become locked."""
    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
    moved = cancelled = renamed = locked = 0
    with SessionLocal() as session:
        state = session.get(CalendarSyncState, calendar.calendar_id)
        sync_token = state.sync_token if state else None
        try:
            events, next_sync_token = calendar.list_event_changes(sync_token)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status != 410:
                raise
            logger.info("Calendar sync token 已失效，重新執行完整同步。")
            events, next_sync_token = calendar.list_event_changes()

        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status.in_(("scheduled", "feedback_requested")),
                    Task.event_id.is_not(None),
                )
            )
        )
        segments = list(
            session.scalars(
                select(TaskSegment).join(Task).where(
                    Task.status.in_(("scheduled", "feedback_requested"))
                )
            )
        )
        task_by_event = {task.event_id: task for task in tasks}
        segment_by_event = {segment.event_id: segment for segment in segments}
        touched_parent_ids: set[int] = set()

        for event in events:
            event_id = event.get("id")
            task = task_by_event.get(event_id)
            segment = segment_by_event.get(event_id)
            if task is None and segment is None:
                continue
            if event.get("status") == "cancelled":
                target_task = task or session.get(Task, segment.task_id)
                if target_task:
                    target_task.status = "cancelled"
                    target_task.event_id = None
                    cancelled += 1
                continue

            start = _calendar_datetime(event.get("start", {}))
            end = _calendar_datetime(event.get("end", {}))
            if task:
                if start and end and (
                    task.scheduled_start != start or task.scheduled_end != end
                ):
                    task.scheduled_start = start
                    task.scheduled_end = end
                    if not task.is_locked:
                        task.is_locked = True
                        locked += 1
                    moved += 1
                summary = str(event.get("summary") or "").strip()
                if summary and summary != task.task_name:
                    task.task_name = summary
                    renamed += 1
            elif segment:
                if start and end and (
                    segment.scheduled_start != start or segment.scheduled_end != end
                ):
                    segment.scheduled_start = start
                    segment.scheduled_end = end
                    segment.is_locked = True
                    parent = session.get(Task, segment.task_id)
                    if parent and not parent.is_locked:
                        parent.is_locked = True
                        locked += 1
                    touched_parent_ids.add(segment.task_id)
                    moved += 1

        for parent_id in touched_parent_ids:
            parent = session.get(Task, parent_id)
            if parent and parent.segments:
                parent.scheduled_start = min(item.scheduled_start for item in parent.segments)
                parent.scheduled_end = max(item.scheduled_end for item in parent.segments)

        now = _db_time(datetime.now(TIMEZONE))
        if state is None:
            session.add(
                CalendarSyncState(
                    calendar_id=calendar.calendar_id,
                    sync_token=next_sync_token,
                    updated_at=now,
                )
            )
        else:
            state.sync_token = next_sync_token
            state.updated_at = now
        session.commit()
    return {
        "moved": moved,
        "cancelled": cancelled,
        "renamed": renamed,
        "locked": locked,
        "changes": len(events),
    }


async def calendar_sync_job() -> None:
    try:
        result = await asyncio.to_thread(sync_calendar_tasks)
        logger.info(
            "Calendar 增量同步完成：變更=%s，移動=%s，鎖定=%s，改名=%s，取消=%s。",
            result["changes"], result["moved"], result["locked"],
            result["renamed"], result["cancelled"],
        )
    except Exception:
        logger.exception("Calendar 增量同步失敗")


def _failure_tasks(task_ids: list[int] | None = None) -> list[Task]:
    with SessionLocal() as session:
        query = select(Task).where(
            Task.status == "pending",
            Task.schedule_failure_reason.is_not(None),
        )
        if task_ids is not None:
            query = query.where(Task.id.in_(task_ids))
        tasks = list(session.scalars(query.order_by(Task.deadline, Task.id)))
        for task in tasks:
            session.expunge(task)
        return tasks


def _notification_due(task: Task, now: datetime) -> tuple[bool, str]:
    urgent = bool(task.deadline and _local(task.deadline) <= now + timedelta(hours=24))
    marker = f"{task.schedule_failure_reason}:urgent" if urgent else str(task.schedule_failure_reason)
    if task.schedule_failure_notified_reason != marker:
        return True, marker
    if not task.schedule_failure_notified_at:
        return True, marker
    elapsed = now - _local(task.schedule_failure_notified_at)
    return elapsed >= timedelta(hours=SCHEDULE_NOTIFICATION_COOLDOWN_HOURS), marker


def _mark_failure_notified(task_id: int, marker: str) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            task.schedule_failure_notified_at = _db_time(datetime.now(TIMEZONE))
            task.schedule_failure_notified_reason = marker
            session.commit()


async def notify_schedule_failures(task_ids: list[int] | None = None) -> int:
    await asyncio.to_thread(clear_outside_horizon_failures)
    now = datetime.now(TIMEZONE)
    sent = 0
    for task in await asyncio.to_thread(_failure_tasks, task_ids):
        due, marker = _notification_due(task, now)
        if not due:
            continue
        recipient = task.discord_user_id or DISCORD_USER_ID
        if not recipient:
            continue
        result = AttemptResult(
            task.id,
            False,
            failure_reason=task.schedule_failure_reason,
            failure_details=task.schedule_failure_details,
        )
        await send_schedule_failure_notification(
            int(recipient),
            failure_message(result, task.task_name, task.deadline),
            int(task.source_channel_id) if task.source_channel_id else None,
        )
        await asyncio.to_thread(_mark_failure_notified, task.id, marker)
        sent += 1
    return sent


def _recurrence_notice_target(rule_id: int) -> tuple[int, str, int | None] | None:
    with SessionLocal() as session:
        rule = session.get(RecurrenceRule, rule_id)
        if not rule:
            return None
        return (
            int(rule.discord_user_id),
            rule.task_name,
            int(rule.source_channel_id) if rule.source_channel_id else None,
        )



async def recurrence_maintenance_job() -> None:
    await discord_bot.wait_until_ready()
    try:
        cleared_deferred = await asyncio.to_thread(clear_outside_horizon_failures)
        results = await asyncio.to_thread(maintain_rules)
        created = sum(result.created for result in results)
        scheduled = sum(result.scheduled for result in results)
        conflict_notified = 0
        for result in results:
            if not result.conflicts:
                continue
            target = await asyncio.to_thread(_recurrence_notice_target, result.rule_id)
            if not target:
                continue
            user_id, task_name, channel_id = target
            await send_schedule_failure_notification(
                user_id,
                f"⚠️ 重複系列「{task_name}」有 {result.conflicts} 個固定時段與 Google Calendar 衝突，因此未建立。",
                channel_id,
            )
            conflict_notified += 1
        prompted = 0
        for rule in await asyncio.to_thread(rules_due_for_extension):
            await send_recurrence_extension_request(
                rule.id,
                int(rule.discord_user_id),
                rule.task_name,
                rule.cycle_end,
                int(rule.source_channel_id) if rule.source_channel_id else None,
            )
            await asyncio.to_thread(mark_awaiting_extension, rule.id)
            prompted += 1
        logger.info(
            "重複規則維護完成：清除延後任務假失敗=%s，新增=%s，已排入=%s，衝突通知=%s，到期詢問=%s。",
            cleared_deferred, created, scheduled, conflict_notified, prompted,
        )
    except Exception:
        logger.exception("重複規則維護失敗")



async def daily_schedule_job() -> None:
    try:
        with SessionLocal() as session:
            pending = list(
                session.scalars(
                    select(Task).where(
                        Task.status == "pending",
                        Task.discord_user_id.is_not(None),
                    )
                )
            )
            user_targets: dict[int, int | None] = {}
            for task in pending:
                user_targets.setdefault(
                    int(task.discord_user_id),
                    int(task.source_channel_id) if task.source_channel_id else None,
                )
        previewed = 0
        for user_id, channel_id in user_targets.items():
            proposal = await asyncio.to_thread(
                propose_rolling_replan, user_id=user_id
            )
            if not proposal:
                continue
            payload = proposal.to_dict()
            if proposal.moves_existing:
                await send_rolling_replan_request(user_id, payload, channel_id)
                previewed += 1
            else:
                await asyncio.to_thread(apply_replan, payload)
        if previewed:
            logger.info("多日最佳化完成：已傳送 %s 份重排摘要，等待確認。", previewed)
            return
        results = await asyncio.to_thread(schedule_all_pending)
        scheduled = sum(result.scheduled for result in results)
        failed_ids = [
            result.task_id for result in results
            if not result.scheduled
            and result.failure_reason not in {"outside_horizon", "not_pending"}
        ]
        deferred = sum(result.failure_reason == "outside_horizon" for result in results)
        notified = await notify_schedule_failures(failed_ids)
        logger.info(
            "多日排程完成：已排入=%s，等待規劃=%s，未排入=%s，已通知=%s。",
            scheduled, deferred, len(failed_ids), notified,
        )
    except Exception:
        logger.exception("每日排程失敗")


async def schedule_failure_notification_job() -> None:
    try:
        sent = await notify_schedule_failures()
        if sent:
            logger.info("排程失敗提醒完成：已通知=%s。", sent)
    except Exception:
        logger.exception("排程失敗提醒工作失敗")


def _find_finished_scheduled_tasks() -> list[tuple[int, str, str | None]]:
    now = datetime.now(TIMEZONE)
    finished: list[tuple[int, str, str | None]] = []
    with SessionLocal() as session:
        tasks = list(session.scalars(select(Task).where(Task.status == "scheduled", ~Task.segments.any())))
        for task in tasks:
            if session.scalar(select(Feedback.id).where(Feedback.task_id == task.id)):
                continue
            end = _local(task.scheduled_end) if task.scheduled_end else None
            if end is None and task.event_id:
                try:
                    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
                    event = calendar.get_event(task.event_id)
                    value = event.get("end", {}).get("dateTime")
                    end = _local(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else None
                except Exception:
                    logger.exception("無法讀取任務 %s 的 Calendar event。", task.id)
            if end and end <= now:
                finished.append((task.id, task.task_name, task.discord_user_id))
    return finished


def _find_finished_segments() -> list[tuple[int, int, int, int, str, str | None]]:
    now = _db_time(datetime.now(TIMEZONE))
    finished: list[tuple[int, int, int, int, str, str | None]] = []
    with SessionLocal() as session:
        segments = list(
            session.scalars(
                select(TaskSegment)
                .join(Task)
                .where(
                    Task.status == "scheduled",
                    TaskSegment.status == "scheduled",
                    TaskSegment.scheduled_end <= now,
                )
                .order_by(TaskSegment.scheduled_end)
            )
        )
        task_ids = {segment.task_id for segment in segments}
        counts = {
            task_id: len(
                list(
                    session.scalars(
                        select(TaskSegment).where(TaskSegment.task_id == task_id)
                    )
                )
            )
            for task_id in task_ids
        }
        for segment in segments:
            if session.scalar(
                select(Feedback.id).where(Feedback.segment_id == segment.id)
            ):
                continue
            task = session.get(Task, segment.task_id)
            if task:
                finished.append(
                    (
                        task.id,
                        segment.id,
                        segment.segment_index,
                        counts[task.id],
                        task.task_name,
                        task.discord_user_id,
                    )
                )
    return finished


def _mark_feedback_requested(task_id: int) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task and task.status == "scheduled":
            task.status = "feedback_requested"
            session.commit()


def _mark_segment_feedback_requested(segment_id: int) -> None:
    with SessionLocal() as session:
        segment = session.get(TaskSegment, segment_id)
        if segment and segment.status == "scheduled":
            segment.status = "feedback_requested"
            session.commit()


async def check_completed_tasks() -> None:
    try:
        finished = await asyncio.to_thread(_find_finished_scheduled_tasks)
        for task_id, task_name, owner_id in finished:
            recipient = owner_id or DISCORD_USER_ID
            if not recipient:
                logger.error("任務 %s 沒有 Discord 使用者 ID。", task_id)
                continue
            await send_feedback_request(int(recipient), task_name, task_id)
            await asyncio.to_thread(_mark_feedback_requested, task_id)
            logger.info("已傳送任務 %s 的回饋邀請。", task_id)
        finished_segments = await asyncio.to_thread(_find_finished_segments)
        for task_id, segment_id, index, count, task_name, owner_id in finished_segments:
            recipient = owner_id or DISCORD_USER_ID
            if not recipient:
                logger.error(
                    "任務 %s 的分段 %s 沒有 Discord 使用者 ID。",
                    task_id,
                    segment_id,
                )
                continue
            await send_segment_feedback_request(
                int(recipient), task_name, task_id, segment_id, index, count
            )
            await asyncio.to_thread(_mark_segment_feedback_requested, segment_id)
            logger.info(
                "已傳送任務 %s 第 %s/%s 段的回饋邀請。",
                task_id,
                index,
                count,
            )
    except Exception:
        logger.exception("完工追蹤任務失敗")


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        calendar_sync_job,
        CronTrigger(minute="0,30", timezone=TIMEZONE),
        id="calendar_sync_job",
        replace_existing=True,
    )
    scheduler.add_job(
        schedule_failure_notification_job,
        CronTrigger(minute="5,35", timezone=TIMEZONE),
        id="schedule_failure_notification_job",
        replace_existing=True,
    )
    scheduler.add_job(
        recurrence_maintenance_job,
        CronTrigger(hour=0, minute=10, timezone=TIMEZONE),
        next_run_time=datetime.now(TIMEZONE) + timedelta(seconds=30),
        id="recurrence_maintenance_job",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_schedule_job,
        CronTrigger(hour=23, minute=59, timezone=TIMEZONE),
        id="daily_schedule_job",
        replace_existing=True,
    )
    scheduler.add_job(
        check_completed_tasks,
        IntervalTrigger(minutes=15, timezone=TIMEZONE),
        id="check_completed_tasks",
        replace_existing=True,
    )
    return scheduler
