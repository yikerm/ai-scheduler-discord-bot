"""Discord UI and natural-language task orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from availability import (
    mode_for_day,
    schedule_summary,
    set_day_override,
    set_mode,
    set_work_schedule,
    work_window_for_day,
)
from config import (
    DISCORD_BOT_TOKEN,
    DISCORD_USER_ID,
    NLP_PENDING_MINUTES,
    PLANNING_HORIZON_DAYS,
    REPEAT_HORIZON_DAYS,
)
from database import (
    Feedback,
    FeedbackDraft,
    PendingRequest,
    RecurrenceRule,
    SegmentFeedbackDraft,
    SessionLocal,
    Task,
    TaskSegment,
)
from gcal_service import GoogleCalendarService
from feedback_service import finalize_completed, finalize_incomplete, save_efficiency_draft
from nlp_router import merge_supplement, parse_bot_entry, parse_bot_intent
from planning_service import (
    apply_replan,
    attempt_schedule_task,
    failure_message,
    propose_rolling_replan,
    propose_same_day_replan,
)
from recurrence_service import (
    create_rule,
    delete_series,
    end_rule,
    extend_rule,
    generate_occurrences,
)
from settings_parser import parse_schedule_settings
from segment_feedback import SegmentEfficiencyFeedbackView, SegmentMentalFeedbackView
from structured_add import StructuredAddView
from structured_delete import DeleteEntry, StructuredDeleteView
from structured_fixed import StructuredFixedView
from structured_plan import PlanEntry, StructuredPlanView
from structured_repeat import StructuredRepeatView
from temporal_parser import parse_date_text, parse_duration


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Taipei")
UNFINISHED_STATUSES = ("pending", "scheduled", "feedback_requested")
CONFIRM_ACTIONS = {"fixed", "repeat", "delete", "reset", "reschedule", "schedule_settings"}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


@dataclass
class ExecutionOutcome:
    content: str
    view: discord.ui.View | None = None


def _local(value: datetime) -> datetime:
    return value.replace(tzinfo=TIMEZONE) if value.tzinfo is None else value.astimezone(TIMEZONE)


def _db_time(value: datetime) -> datetime:
    return _local(value).replace(tzinfo=None)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return _db_time(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None))


def _parse_fixed_time(day_text: str, time_text: str) -> datetime:
    try:
        return datetime.combine(
            date.fromisoformat(day_text), time.fromisoformat(time_text), tzinfo=TIMEZONE
        )
    except ValueError as exc:
        raise ValueError("日期或時間格式不正確。") from exc


def _user_filter(user_id: int):
    return or_(Task.discord_user_id == str(user_id), Task.discord_user_id.is_(None))


def _create_task(parsed: dict, user_id: int, channel_id: int | None = None) -> Task:
    task_date = date.fromisoformat(parsed["date"]) if parsed.get("date") else None
    deadline = _parse_iso_datetime(parsed.get("deadline"))
    available_from = None
    if task_date:
        start, end, _source = work_window_for_day(task_date)
        available_from = start
        if deadline is None:
            deadline = end
    with SessionLocal() as session:
        task = Task(
            task_name=str(parsed["task_name"]).strip(),
            estimated_minutes=int(parsed["duration_minutes"]),
            status="pending",
            deadline=deadline,
            available_from=available_from,
            discord_user_id=str(user_id),
            source_channel_id=str(channel_id) if channel_id else None,
            priority=int(parsed.get("priority") or 0),
            allow_split=bool(parsed.get("allow_split")),
            min_segment_minutes=int(parsed.get("min_segment_minutes") or 30),
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        session.expunge(task)
        return task


def _slot_is_free(calendar: GoogleCalendarService, start: datetime, end: datetime) -> bool:
    return any(
        slot["start"] <= start and end <= slot["end"]
        for slot in calendar.get_free_slots(start.date())
    )


def _create_fixed_task(
    user_id: int,
    title: str,
    start: datetime,
    minutes: int,
    recurrence_group: str | None = None,
    channel_id: int | None = None,
) -> Task:
    if minutes <= 0:
        raise ValueError("分鐘數必須大於 0。")
    end = start + timedelta(minutes=minutes)
    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
    if not _slot_is_free(calendar, start, end):
        raise ValueError("指定時段已有行程，請選擇其他時間。")
    event_id = calendar.create_event(title, start, end)
    with SessionLocal() as session:
        task = Task(
            task_name=title,
            estimated_minutes=minutes,
            status="scheduled",
            deadline=_db_time(end),
            event_id=event_id,
            discord_user_id=str(user_id),
            source_channel_id=str(channel_id) if channel_id else None,
            scheduled_start=_db_time(start),
            scheduled_end=_db_time(end),
            is_fixed=True,
            is_locked=True,
            recurrence_group=recurrence_group,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        session.expunge(task)
        return task


WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "一": 0,
    "tue": 1, "tuesday": 1, "二": 1,
    "wed": 2, "wednesday": 2, "三": 2,
    "thu": 3, "thursday": 3, "四": 3,
    "fri": 4, "friday": 4, "五": 4,
    "sat": 5, "saturday": 5, "六": 5,
    "sun": 6, "sunday": 6, "日": 6, "天": 6,
}


def _selected_weekdays(frequency: str, current_weekday: int) -> set[int]:
    normalized = (
        frequency.lower().replace("星期", "").replace("週", "")
        .replace("week", "").replace("到", "-").replace("至", "-").replace(" ", "")
    )
    if normalized == "daily":
        return set(range(7))
    if normalized in {"weekly", "每週"}:
        return {current_weekday}
    if normalized in {"weekday", "weekdays", "mon-fri"}:
        return set(range(5))
    if "," in normalized:
        try:
            return {WEEKDAY_NAMES[value] for value in normalized.split(",")}
        except KeyError as exc:
            raise ValueError("星期格式錯誤。") from exc
    if "-" in normalized:
        start_name, end_name = normalized.split("-", 1)
        try:
            start, end = WEEKDAY_NAMES[start_name], WEEKDAY_NAMES[end_name]
        except KeyError as exc:
            raise ValueError("星期區間格式錯誤。") from exc
        return set(range(start, end + 1)) if start <= end else set(range(start, 7)) | set(range(end + 1))
    try:
        return {WEEKDAY_NAMES[normalized]}
    except KeyError as exc:
        raise ValueError("星期格式錯誤。") from exc


def _recurrence_days(frequency: str, days: int, *, include_today: bool) -> list[date]:
    if not 1 <= days <= 90:
        raise ValueError("建立天數需介於 1 到 90。")
    today = datetime.now(TIMEZONE).date()
    selected = _selected_weekdays(frequency, today.weekday())
    first = 0 if include_today else 1
    return [
        target
        for offset in range(first, days + first)
        if (target := today + timedelta(days=offset)).weekday() in selected
    ]


def _create_recurrences(user_id: int, parsed: dict, channel_id: int | None = None) -> tuple[list[Task], int]:
    clock = time.fromisoformat(str(parsed["time"]))
    minutes = int(parsed["duration_minutes"])
    days = int(parsed.get("days") or REPEAT_HORIZON_DAYS)
    title = str(parsed["task_name"])
    frequency = str(parsed["frequency"])
    now = datetime.now(TIMEZONE)
    group = str(uuid.uuid4())
    created: list[Task] = []
    conflicts = 0
    for target_day in _recurrence_days(frequency, days, include_today=True):
        start = datetime.combine(target_day, clock, tzinfo=TIMEZONE)
        if start <= now:
            continue
        try:
            created.append(_create_fixed_task(user_id, title, start, minutes, group, channel_id))
        except ValueError:
            conflicts += 1
    return created, conflicts


def _create_flexible_recurrences(user_id: int, parsed: dict, channel_id: int | None = None) -> list[Task]:
    minutes = int(parsed["duration_minutes"])
    days = int(parsed.get("days") or REPEAT_HORIZON_DAYS)
    frequency = str(parsed["frequency"])
    title = str(parsed["task_name"])
    group = str(uuid.uuid4())
    created: list[Task] = []
    with SessionLocal() as session:
        for target_day in _recurrence_days(frequency, days, include_today=False):
            start, end, _source = work_window_for_day(target_day)
            task = Task(
                task_name=title,
                estimated_minutes=minutes,
                status="pending",
                available_from=start,
                deadline=end,
                discord_user_id=str(user_id),
                source_channel_id=str(channel_id) if channel_id else None,
                recurrence_group=group,
                priority=int(parsed.get("priority") or 0),
                allow_split=bool(parsed.get("allow_split")),
            )
            session.add(task)
            created.append(task)
        session.commit()
        for task in created:
            session.refresh(task)
            session.expunge(task)
    return created


def _create_managed_recurrence(
    user_id: int, parsed: dict, channel_id: int | None = None
):
    final_end = (
        date.fromisoformat(str(parsed["recurrence_end_date"]))
        if parsed.get("recurrence_end_date")
        else None
    )
    rule = create_rule(
        group_id=str(uuid.uuid4()),
        user_id=user_id,
        channel_id=channel_id,
        task_name=str(parsed["task_name"]),
        minutes=int(parsed["duration_minutes"]),
        frequency=str(parsed["frequency"]),
        fixed_time=str(parsed["time"]) if parsed.get("time") else None,
        min_segment_minutes=int(parsed.get("min_segment_minutes") or 30),
        allow_split=bool(parsed.get("allow_split")),
        priority=int(parsed.get("priority") or 0),
        final_end_date=final_end,
        requested_start_date=(
            date.fromisoformat(str(parsed["date"])) if parsed.get("date") else None
        ),
        duration_days=int(parsed["days"]) if parsed.get("days") else None,
    )
    result = generate_occurrences(rule.id)
    return rule, result



def _unfinished_tasks_for_user(user_id: int) -> list[Task]:
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    _user_filter(user_id), Task.status.in_(UNFINISHED_STATUSES)
                )
            )
        )
        for task in tasks:
            session.expunge(task)
    return sorted(
        tasks,
        key=lambda task: (
            task.scheduled_start is None and task.deadline is None,
            task.scheduled_start or task.deadline or datetime.max,
            task.id,
        ),
    )


def _delete_entries(user_id: int) -> list[DeleteEntry]:
    active = _unfinished_tasks_for_user(user_id)
    task_numbers = {task.id: index for index, task in enumerate(active, start=1)}
    candidate_ids = [
        task.id for task in active if task.status in ("pending", "scheduled")
    ]
    if not candidate_ids:
        return []
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .where(
                    Task.id.in_(candidate_ids),
                    ~Task.feedback_entries.any(),
                )
                .options(selectinload(Task.segments))
            )
        )
        entries = [
            DeleteEntry(
                task_id=task.id,
                task_number=task_numbers[task.id],
                title=task.task_name,
                status=task.status,
                duration_minutes=task.estimated_minutes,
                scheduled_start=task.scheduled_start,
                deadline=task.deadline,
                recurrence_group=task.recurrence_group,
                is_fixed=task.is_fixed,
                segment_count=len(task.segments),
            )
            for task in tasks
        ]
    return sorted(entries, key=lambda entry: entry.task_number)


def _find_tasks_by_text(user_id: int, query: str) -> list[Task]:
    tasks = _unfinished_tasks_for_user(user_id)
    date_value, _original, _ambiguities = parse_date_text(query)
    keyword = re.sub(r"\d{1,2}[月/]\d{1,2}[日號]?", "", query).strip().casefold()
    if date_value:
        tasks = [
            task for task in tasks
            if (task.scheduled_start or task.deadline)
            and (task.scheduled_start or task.deadline).date() == date_value
        ]
    stopwords = ("請", "幫我", "查詢", "找", "任務", "刪除", "取消", "改期", "移到", "重新安排")
    for token in stopwords:
        keyword = keyword.replace(token, "")
    keyword = keyword.strip(" ，,。")
    return [task for task in tasks if keyword in task.task_name.casefold()] if keyword else tasks


def _active_task(user_id: int, number: int) -> Task:
    tasks = _unfinished_tasks_for_user(user_id)
    if number < 1 or number > len(tasks):
        raise ValueError("找不到此未完成編號。")
    return tasks[number - 1]


def _task_target(user_id: int, parsed: dict) -> Task:
    if parsed.get("task_number"):
        return _active_task(user_id, int(parsed["task_number"]))
    candidates = _find_tasks_by_text(
        user_id, str(parsed.get("query") or parsed.get("task_name") or "")
    )
    if len(candidates) != 1:
        raise ValueError("需要唯一的任務目標，請提供名稱或未完成編號。")
    return candidates[0]


def _task_event_ids(task_id: int) -> list[str]:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            return []
        return [value for value in [task.event_id, *(segment.event_id for segment in task.segments)] if value]


def _delete_task(task_id: int, user_id: int, preserve_feedback: bool = True) -> str:
    with SessionLocal() as session:
        task = session.scalar(
            select(Task)
            .where(Task.id == task_id)
            .options(selectinload(Task.feedback_entries), selectinload(Task.segments))
        )
        if not task:
            raise ValueError("找不到指定任務。")
        if task.discord_user_id and task.discord_user_id != str(user_id):
            raise PermissionError("你只能刪除自己的任務。")
        if preserve_feedback and task.feedback_entries:
            raise ValueError("此任務已有歷史評分，為避免遺失訓練資料，不能整筆刪除。")
        name = task.task_name
        event_ids = [value for value in [task.event_id, *(item.event_id for item in task.segments)] if value]
    calendar = GoogleCalendarService()
    for event_id in event_ids:
        try:
            calendar.delete_event(event_id)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in (404, 410):
                raise
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            session.delete(task)
            session.commit()
    return name


def _reset_user_tasks(user_id: int) -> int:
    with SessionLocal() as session:
        tasks = list(session.scalars(select(Task).where(_user_filter(user_id))))
        ids = [task.id for task in tasks]
    for task_id in ids:
        _delete_task(task_id, user_id, preserve_feedback=False)
    with SessionLocal() as session:
        for rule in session.scalars(
            select(RecurrenceRule).where(RecurrenceRule.discord_user_id == str(user_id))
        ):
            session.delete(rule)
        session.commit()
    return len(ids)


def _format_tasks(tasks: list[Task], user_id: int) -> str:
    active = _unfinished_tasks_for_user(user_id)
    numbers = {task.id: index for index, task in enumerate(active, start=1)}
    if not tasks:
        return "🎉 目前沒有符合的未完成任務。"
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    horizon = now + timedelta(days=PLANNING_HORIZON_DAYS)
    lines = []
    for task in tasks[:30]:
        when = task.scheduled_start or task.deadline
        time_text = when.strftime("%m/%d %H:%M") if when else "未指定期限"
        if task.schedule_failure_reason:
            status = "無法排入"
        elif task.status == "pending" and task.deadline and task.deadline > horizon:
            status = "等待規劃"
        else:
            status = task.status
        lock = " 🔒" if task.is_locked else ""
        lines.append(
            f"編號 {numbers.get(task.id, '?')} [{status}] {task.task_name}{lock}"
            f"（{task.estimated_minutes} 分鐘，{time_text}）"
        )
    return "📋 任務：\n" + "\n".join(lines)


def _format_plan(user_id: int, days: int) -> str:
    start = datetime.now(TIMEZONE).replace(tzinfo=None)
    end = start + timedelta(days=days)
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    _user_filter(user_id),
                    Task.status == "scheduled",
                    Task.scheduled_start.is_not(None),
                    Task.scheduled_start >= start,
                    Task.scheduled_start < end,
                ).order_by(Task.scheduled_start)
            )
        )
    if not tasks:
        return f"未來 {days} 天沒有已排定行程。"
    return f"🗓️ 未來 {days} 天規劃：\n" + "\n".join(
        f"{task.scheduled_start:%m/%d %H:%M}–{task.scheduled_end:%H:%M}｜{task.task_name}"
        for task in tasks[:30]
    )


def _plan_entries(user_id: int, days: int) -> list[PlanEntry]:
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    end = datetime.combine(now.date() + timedelta(days=days), time.min)
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .options(selectinload(Task.segments))
                .where(
                    _user_filter(user_id),
                    Task.status == "scheduled",
                    Task.scheduled_end.is_not(None),
                    Task.scheduled_end > now,
                    Task.scheduled_start < end,
                )
                .order_by(Task.scheduled_start)
            )
        )
        entries: list[PlanEntry] = []
        for task in tasks:
            if task.segments:
                count = len(task.segments)
                for segment in task.segments:
                    if now <= segment.scheduled_start < end:
                        entries.append(
                            PlanEntry(
                                start=segment.scheduled_start,
                                end=segment.scheduled_end,
                                title=task.task_name,
                                locked=task.is_locked,
                                segment_index=segment.segment_index,
                                segment_count=count,
                            )
                        )
            elif task.scheduled_start and now <= task.scheduled_start < end:
                entries.append(
                    PlanEntry(
                        start=task.scheduled_start,
                        end=task.scheduled_end,
                        title=task.task_name,
                        locked=task.is_locked,
                    )
                )
    return sorted(entries, key=lambda item: (item.start, item.end, item.title))


def _save_pending(
    user_id: int,
    channel_id: int,
    action: str,
    payload: dict,
    original_text: str,
    state: str,
) -> PendingRequest:
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    with SessionLocal() as session:
        old = session.scalar(
            select(PendingRequest).where(
                PendingRequest.discord_user_id == str(user_id),
                PendingRequest.channel_id == str(channel_id),
            )
        )
        if old:
            session.delete(old)
            session.flush()
        row = PendingRequest(
            discord_user_id=str(user_id),
            channel_id=str(channel_id),
            state=state,
            action=action,
            payload_json=json.dumps(payload, ensure_ascii=False),
            missing_fields_json=json.dumps(payload.get("missing_fields", []), ensure_ascii=False),
            original_text=original_text,
            expires_at=now + timedelta(minutes=NLP_PENDING_MINUTES),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _load_pending(request_id: int | None = None, user_id: int | None = None, channel_id: int | None = None) -> PendingRequest | None:
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    with SessionLocal() as session:
        if request_id is not None:
            row = session.get(PendingRequest, request_id)
        else:
            row = session.scalar(
                select(PendingRequest).where(
                    PendingRequest.discord_user_id == str(user_id),
                    PendingRequest.channel_id == str(channel_id),
                )
            )
        if row and row.expires_at <= now:
            session.delete(row)
            session.commit()
            return None
        if row:
            session.expunge(row)
        return row


def _delete_pending(request_id: int) -> None:
    with SessionLocal() as session:
        row = session.get(PendingRequest, request_id)
        if row:
            session.delete(row)
            session.commit()


FIELD_LABELS = {
    "task_name": "任務名稱",
    "duration_minutes": "預計持續時間",
    "date": "日期",
    "time": "開始時間",
    "frequency": "重複星期",
    "query": "任務名稱或編號",
}


def _missing_prompt(payload: dict) -> str:
    understood = []
    if payload.get("task_name"):
        understood.append(f"事項：{payload['task_name']}")
    if payload.get("date"):
        understood.append(f"日期：{payload['date']}")
    if payload.get("time"):
        understood.append(f"時間：{payload['time']}")
    if payload.get("duration_minutes"):
        understood.append(f"時長：{payload['duration_minutes']} 分鐘")
    missing = "、".join(FIELD_LABELS.get(value, value) for value in payload.get("missing_fields", []))
    prefix = "我已理解：\n" + "\n".join(understood) + "\n\n" if understood else ""
    return prefix + f"還缺少：{missing}。請直接回覆這則訊息補充，不必再次 @Bob。"


def _confirmation_summary(payload: dict) -> str:
    action_labels = {
        "add": "一般待辦",
        "fixed": "固定行程",
        "repeat": "重複任務",
        "delete": "刪除任務",
        "reset": "清除全部任務",
        "reschedule": "任務改期",
        "schedule_settings": "作息調整",
    }
    if payload.get("action") == "schedule_settings":
        settings = dict(payload.get("settings") or {})
        mode_labels = {"school": "開學", "not_school": "未開學"}
        day_labels = {"weekday": "星期一到星期五", "weekend": "星期六日"}
        lines = ["請確認操作：作息調整"]
        if settings.get("mode"):
            mode = settings["mode"]
            lines.append(f"使用模式：{mode_labels.get(mode, mode)}")
        if settings.get("effective_date"):
            lines.append(f"生效日期：{settings['effective_date']}")
        for update in settings.get("updates", []):
            mode = update.get("mode") or settings.get("mode")
            day_type = update.get("day_type")
            lines.append(
                f"{mode_labels.get(mode, mode)}・{day_labels.get(day_type, day_type)}："
                f"{update['start_time']}–{update['end_time']}"
            )
        if settings.get("target_date"):
            lines.append(f"單日例外：{settings['target_date']}")
            if settings.get("start_time") or settings.get("end_time"):
                lines.append(
                    f"可排程時間：{settings.get('start_time', '沿用原設定')}–"
                    f"{settings.get('end_time', '沿用原設定')}"
                )
        if len(lines) == 1:
            lines.append("未辨識到可套用的作息內容")
        return "\n".join(lines)
    lines = [f"請確認操作：{action_labels.get(payload.get('action'), payload.get('action'))}"]
    for key, label in (("task_name", "事項"), ("date", "日期"), ("time", "時間"), ("duration_minutes", "時長"), ("frequency", "頻率"), ("query", "目標")):
        value = payload.get(key)
        if value not in (None, ""):
            suffix = " 分鐘" if key == "duration_minutes" else ""
            lines.append(f"{label}：{value}{suffix}")
    if payload.get("action") == "repeat":
        if payload.get("recurrence_end_date"):
            lines.append("系列截止：" + str(payload.get("recurrence_end_date")))
        elif payload.get("days"):
            lines.append("系列期間：" + str(payload.get("days")) + " 天，到期直接結束")
        else:
            lines.append(f"到期方式：{REPEAT_HORIZON_DAYS} 天後詢問是否延長")
    if payload.get("allow_split"):
        lines.append("允許分段：是（每段至少 30 分鐘）")
    if payload.get("ambiguities"):
        lines.append("需要確認：" + "；".join(payload["ambiguities"]))
    return "\n".join(lines)


def _format_interval_rows(values: list[list[str]] | tuple[tuple[str, str], ...]) -> str:
    parts = []
    for start_text, end_text in values:
        start = datetime.fromisoformat(start_text)
        end = datetime.fromisoformat(end_text)
        if start.date() == end.date():
            parts.append(f"{start:%m/%d %H:%M}–{end:%H:%M}")
        else:
            parts.append(f"{start:%m/%d %H:%M}–{end:%m/%d %H:%M}")
    return "、".join(parts)


def _rolling_summary(proposal: dict) -> str:
    priority_labels = ("一般", "重要", "緊急")
    added: list[str] = []
    moved: list[str] = []
    for change in proposal.get("changes", []):
        priority = max(0, min(int(change.get("priority") or 0), 2))
        label = priority_labels[priority]
        new_text = _format_interval_rows(change["new_intervals"])
        if change.get("old_intervals"):
            old_text = _format_interval_rows(change["old_intervals"])
            moved.append(f"• {change['task_name']}（{label}）：{old_text} → {new_text}")
        else:
            added.append(f"• {change['task_name']}（{label}）：{new_text}")
    lines = ["🧠 **未來 7 天重排建議**"]
    visible_limit = 12
    if added:
        lines.extend(["", "**新排入**", *added[:visible_limit]])
        if len(added) > visible_limit:
            lines.append(f"…另有 {len(added) - visible_limit} 筆新排入任務")
    if moved:
        remaining = max(0, visible_limit - min(len(added), visible_limit))
        visible_moved = moved[:remaining]
        lines.extend(["", "**將調整既有彈性任務**", *visible_moved])
        if len(moved) > len(visible_moved):
            lines.append(f"…另有 {len(moved) - len(visible_moved)} 筆既有任務會調整")
    unchanged = int(proposal.get("unchanged_count") or 0)
    if unchanged:
        lines.extend(["", f"維持原位：{unchanged} 筆"])
    lines.extend([
        "",
        "固定、鎖定、已開始及外部 Calendar 行程不會移動。",
        "只有按下「確認重排」後才會更新 Calendar。",
    ])
    content = "\n".join(lines)
    return content if len(content) <= 1900 else content[:1870] + "\n…其餘項目省略"


async def _prepare_rolling_outcome(
    proposal,
    *,
    user_id: int,
    channel_id: int,
    original_text: str,
) -> ExecutionOutcome | None:
    if not proposal:
        return None
    payload = proposal.to_dict()
    if not proposal.moves_existing:
        await asyncio.to_thread(apply_replan, payload)
        return ExecutionOutcome("✅ 已依未來 7 天最佳方案完成排程。")
    request = await asyncio.to_thread(
        _save_pending,
        user_id,
        channel_id,
        "apply_replan",
        {"action": "apply_replan", "proposal": payload},
        original_text,
        "awaiting_replan",
    )
    return ExecutionOutcome(
        _rolling_summary(payload), ReplanConfirmationView(request.id, user_id)
    )


def _apply_schedule_settings(payload: dict) -> str:
    settings = dict(payload.get("settings") or {})
    local_settings = parse_schedule_settings(str(payload.get("original_text") or ""))
    for key, value in local_settings.items():
        if value not in (None, [], ""):
            settings[key] = value
    mode = settings.get("mode")
    effective = date.fromisoformat(settings.get("effective_date")) if settings.get("effective_date") else datetime.now(TIMEZONE).date()
    if mode:
        set_mode(str(mode), effective)
    for update in settings.get("updates", []):
        start = time.fromisoformat(update["start_time"])
        end = time.fromisoformat(update["end_time"])
        set_work_schedule(
            update.get("mode") or mode,
            update["day_type"],
            start.hour * 60 + start.minute,
            end.hour * 60 + end.minute,
        )
    if settings.get("target_date"):
        target = date.fromisoformat(settings["target_date"])
        current_start, current_end, _source = work_window_for_day(target)
        start_clock = time.fromisoformat(settings["start_time"]) if settings.get("start_time") else current_start.time()
        end_clock = time.fromisoformat(settings["end_time"]) if settings.get("end_time") else current_end.time()
        set_day_override(
            target,
            start_clock.hour * 60 + start_clock.minute,
            end_clock.hour * 60 + end_clock.minute,
        )
    return "✅ 作息設定已更新。\n" + schedule_summary(effective)


def _reschedule_task(user_id: int, payload: dict) -> Task:
    target = _task_target(user_id, payload)
    start = _parse_fixed_time(str(payload["date"]), str(payload["time"]))
    minutes = int(payload.get("duration_minutes") or target.estimated_minutes)
    end = start + timedelta(minutes=minutes)
    calendar = GoogleCalendarService(timezone=str(TIMEZONE))
    free = list(calendar.get_free_slots(start.date()))
    if target.scheduled_start and target.scheduled_end:
        free.append({"start": _local(target.scheduled_start), "end": _local(target.scheduled_end)})
    if not any(item["start"] <= start and end <= item["end"] for item in free):
        raise ValueError("新時段與其他不可移動行程衝突。")
    if not target.event_id:
        raise ValueError("分段任務目前請先刪除後重新建立。")
    calendar.update_event_time(target.event_id, start, end, target.task_name)
    with SessionLocal() as session:
        task = session.get(Task, target.id)
        task.scheduled_start = _db_time(start)
        task.scheduled_end = _db_time(end)
        task.deadline = _db_time(end) if task.is_fixed else task.deadline
        task.estimated_minutes = minutes
        task.is_locked = True
        task.schedule_failure_reason = None
        session.commit()
        session.refresh(task)
        session.expunge(task)
        return task


async def _execute_intent(payload: dict, user_id: int, channel_id: int) -> ExecutionOutcome:
    action = payload.get("action")
    if action == "tasks":
        return ExecutionOutcome(_format_tasks(_find_tasks_by_text(user_id, str(payload.get("query") or "")), user_id))
    if action == "plan":
        return ExecutionOutcome(_format_plan(user_id, max(1, min(int(payload.get("plan_days") or 7), 30))))
    if action == "add":
        task = await asyncio.to_thread(_create_task, payload, user_id, channel_id)
        proposal = await asyncio.to_thread(propose_rolling_replan, task.id)
        rolling = await _prepare_rolling_outcome(
            proposal,
            user_id=user_id,
            channel_id=channel_id,
            original_text=task.task_name,
        )
        if rolling:
            return rolling
        result = await asyncio.to_thread(attempt_schedule_task, task.id)
        if result.scheduled:
            segment_note = f"，分成 {result.segment_count} 段" if result.segment_count > 1 else ""
            return ExecutionOutcome(
                f"✅ 已收錄並排定「{task.task_name}」{segment_note}："
                f"{result.start:%m/%d %H:%M}–{result.end:%H:%M}。"
            )
        if task.deadline and _local(task.deadline).date() == datetime.now(TIMEZONE).date():
            proposal = await asyncio.to_thread(propose_same_day_replan, task.id)
            if proposal:
                request = await asyncio.to_thread(
                    _save_pending,
                    user_id,
                    channel_id,
                    "apply_replan",
                    {"action": "apply_replan", "proposal": proposal.to_dict()},
                    task.task_name,
                    "awaiting_replan",
                )
                lines = ["可以排入，但需要調整以下彈性任務："]
                for change in proposal.changes:
                    old = datetime.fromisoformat(change.old_start).strftime("%H:%M") if change.old_start else "新增"
                    new = datetime.fromisoformat(change.new_start).strftime("%H:%M")
                    lines.append(f"{change.task_name}：{old} → {new}")
                return ExecutionOutcome("\n".join(lines), ReplanConfirmationView(request.id, user_id))
        return ExecutionOutcome(failure_message(result, task.task_name, task.deadline))
    if action == "fixed":
        task = await asyncio.to_thread(
            _create_fixed_task,
            user_id,
            str(payload["task_name"]),
            _parse_fixed_time(str(payload["date"]), str(payload["time"])),
            int(payload["duration_minutes"]),
            None,
            channel_id,
        )
        return ExecutionOutcome(f"📌 已建立固定行程「{task.task_name}」。")
    if action == "repeat":
        rule, result = await asyncio.to_thread(
            _create_managed_recurrence, user_id, payload, channel_id
        )
        expiry = (
            f"持續到 {rule.final_end_date:%Y-%m-%d}"
            if rule.final_end_date
            else f"本期到 {rule.cycle_end:%Y-%m-%d}，到期會詢問是否延長"
        )
        conflict_note = f"；略過 {result.conflicts} 個衝突時段" if result.conflicts else ""
        base = (
            f"🔁 已建立重複系列「{rule.task_name}」，{expiry}。"
            f"已產生 {result.created} 筆，立即排入 {result.scheduled} 筆{conflict_note}。"
        )
        proposal = await asyncio.to_thread(
            propose_rolling_replan, user_id=user_id
        )
        rolling = await _prepare_rolling_outcome(
            proposal,
            user_id=user_id,
            channel_id=channel_id,
            original_text=rule.task_name,
        )
        if rolling:
            rolling.content = base + "\n\n" + rolling.content
            return rolling
        return ExecutionOutcome(base)
    if action == "delete":
        target = await asyncio.to_thread(_task_target, user_id, payload)
        name = await asyncio.to_thread(_delete_task, target.id, user_id)
        return ExecutionOutcome(f"🗑️ 已刪除「{name}」及其 Calendar 行程。")
    if action == "reset":
        count = await asyncio.to_thread(_reset_user_tasks, user_id)
        return ExecutionOutcome(f"🧹 已清除 {count} 個任務、相關評分與 Calendar 行程。")
    if action == "reschedule":
        task = await asyncio.to_thread(_reschedule_task, user_id, payload)
        return ExecutionOutcome(f"📌 已將「{task.task_name}」改到 {task.scheduled_start:%m/%d %H:%M}。")
    if action == "schedule_settings":
        return ExecutionOutcome(await asyncio.to_thread(_apply_schedule_settings, payload))
    return ExecutionOutcome("我還無法安全判斷這個要求，請換個方式描述。")


async def _route_payload(payload: dict, user_id: int, channel_id: int, original_text: str) -> ExecutionOutcome:
    payload["original_text"] = original_text
    if payload.get("action") == "schedule_settings":
        local_settings = parse_schedule_settings(original_text)
        payload["settings"] = {**(payload.get("settings") or {}), **{k: v for k, v in local_settings.items() if v not in (None, [], "")}}
        settings = payload["settings"]
        if any(token in original_text for token in ("查看", "顯示", "目前", "現在的", "什麼")) and not settings.get("mode") and not settings.get("updates") and not settings.get("target_date"):
            return ExecutionOutcome(schedule_summary())
    if payload.get("missing_fields"):
        await asyncio.to_thread(_save_pending, user_id, channel_id, str(payload["action"]), payload, original_text, "collecting")
        return ExecutionOutcome(_missing_prompt(payload))
    if payload.get("action") == "delete" and not payload.get("ambiguities"):
        target = await asyncio.to_thread(_task_target, user_id, payload)
        if target.recurrence_group:
            request = await asyncio.to_thread(
                _save_pending,
                user_id,
                channel_id,
                "delete_recurrence",
                {"action": "delete_recurrence", "task_id": target.id, "recurrence_group": target.recurrence_group},
                original_text,
                "awaiting_recurrence_delete",
            )
            return ExecutionOutcome(
                f"「{target.task_name}」屬於重複系列，要刪除哪個範圍？",
                RecurrenceDeleteView(request.id, user_id),
            )
    if payload.get("action") in CONFIRM_ACTIONS or payload.get("ambiguities"):
        request = await asyncio.to_thread(_save_pending, user_id, channel_id, str(payload["action"]), payload, original_text, "awaiting_confirmation")
        return ExecutionOutcome(_confirmation_summary(payload), ActionConfirmationView(request.id, user_id))
    return await _execute_intent(payload, user_id, channel_id)


async def _handle_pending_reply(row: PendingRequest, text: str) -> ExecutionOutcome:
    payload = json.loads(row.payload_json)
    if row.state == "awaiting_confirmation":
        return ExecutionOutcome("請使用上一則訊息的確認、修改或取消按鈕。")
    if row.state == "awaiting_revision":
        combined = f"原始要求：{row.original_text}\n使用者修改：{text}"
        payload = await asyncio.to_thread(parse_bot_intent, combined)
        await asyncio.to_thread(_delete_pending, row.id)
        return await _route_payload(payload, int(row.discord_user_id), int(row.channel_id), combined)
    previous_missing = set(payload.get("missing_fields", []))
    merged = merge_supplement(payload, text)
    if "task_name" in merged.get("missing_fields", []) and len(previous_missing) == 1:
        merged["task_name"] = text.strip()
        merged["missing_fields"] = []
    if set(merged.get("missing_fields", [])) == previous_missing:
        combined = f"原始要求：{row.original_text}\n已知資料：{json.dumps(payload, ensure_ascii=False)}\n使用者補充：{text}"
        merged = await asyncio.to_thread(parse_bot_intent, combined)
    await asyncio.to_thread(_delete_pending, row.id)
    return await _route_payload(merged, int(row.discord_user_id), int(row.channel_id), row.original_text + "；" + text)


class ActionConfirmationView(discord.ui.View):
    def __init__(self, request_id: int, user_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id = request_id
        self.user_id = user_id
        self.add_item(_ActionButton(request_id, "confirm", "確認", discord.ButtonStyle.success))
        self.add_item(_ActionButton(request_id, "modify", "修改", discord.ButtonStyle.secondary))
        self.add_item(_ActionButton(request_id, "cancel", "取消", discord.ButtonStyle.danger))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("只有提出要求的使用者可以操作。", ephemeral=True)
        return False

    async def handle(self, interaction: discord.Interaction, operation: str) -> None:
        row = await asyncio.to_thread(_load_pending, self.request_id)
        if not row:
            await interaction.response.send_message("這個確認已逾時，請重新提出要求。", ephemeral=True)
            return
        if operation == "cancel":
            await asyncio.to_thread(_delete_pending, row.id)
            await interaction.response.edit_message(content="已取消。", view=None)
            return
        if operation == "modify":
            with SessionLocal() as session:
                current = session.get(PendingRequest, row.id)
                current.state = "awaiting_revision"
                current.expires_at = datetime.now(TIMEZONE).replace(tzinfo=None) + timedelta(minutes=NLP_PENDING_MINUTES)
                session.commit()
            await interaction.response.edit_message(content="請直接回覆這則訊息，告訴我要修改的內容。", view=None)
            return
        await interaction.response.defer()
        payload = json.loads(row.payload_json)
        try:
            await interaction.edit_original_response(
                content="⏳ 正在執行，請稍候……", view=None
            )
        except discord.HTTPException:
            logger.warning("無法先移除確認按鈕", exc_info=True)
        try:
            outcome = await _execute_intent(payload, self.user_id, int(row.channel_id))
        except Exception as exc:
            logger.exception("確認操作失敗")
            try:
                await interaction.edit_original_response(
                    content=f"操作失敗：{exc}\n你可以再次按確認重試，或取消。",
                    view=ActionConfirmationView(row.id, self.user_id),
                )
            except discord.HTTPException:
                await interaction.followup.send(f"操作失敗：{exc}", ephemeral=True)
            return
        await asyncio.to_thread(_delete_pending, row.id)
        try:
            await interaction.edit_original_response(
                content=outcome.content, view=outcome.view
            )
        except discord.HTTPException:
            logger.exception("操作完成，但無法更新原確認訊息")
            try:
                await interaction.message.edit(
                    content=outcome.content, view=outcome.view
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    "操作已完成：" + outcome.content, ephemeral=True
                )


class _ActionButton(discord.ui.Button[ActionConfirmationView]):
    def __init__(self, request_id: int, operation: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"pending_{operation}_{request_id}")
        self.operation = operation

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, ActionConfirmationView):
            await self.view.handle(interaction, self.operation)


class RecurrenceDeleteView(discord.ui.View):
    def __init__(self, request_id: int, user_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id, self.user_id = request_id, user_id
        self.add_item(RecurrenceDeleteButton(request_id, "occurrence", "只刪除此行程", discord.ButtonStyle.secondary))
        self.add_item(RecurrenceDeleteButton(request_id, "series", "刪除整個系列", discord.ButtonStyle.danger))
        self.add_item(RecurrenceDeleteButton(request_id, "cancel", "取消", discord.ButtonStyle.primary))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的刪除確認。", ephemeral=True)
        return False

    async def handle(self, interaction: discord.Interaction, operation: str) -> None:
        row = await asyncio.to_thread(_load_pending, self.request_id)
        if not row:
            await interaction.response.send_message("這個刪除選項已逾時。", ephemeral=True)
            return
        payload = json.loads(row.payload_json)
        if operation == "cancel":
            await asyncio.to_thread(_delete_pending, row.id)
            await interaction.response.edit_message(content="已取消刪除。", view=None)
            return
        await interaction.response.defer()
        try:
            if operation == "series":
                name, count = await asyncio.to_thread(
                    delete_series, str(payload.get("recurrence_group")), self.user_id
                )
                content = f"🗑️ 已結束「{name}」系列並刪除 {count} 筆尚未完成行程；歷史與評分已保留。"
            else:
                name = await asyncio.to_thread(
                    _delete_task, int(payload.get("task_id")), self.user_id
                )
                content = f"🗑️ 已刪除這一次的「{name}」及其 Calendar 行程。"
            await asyncio.to_thread(_delete_pending, row.id)
            await interaction.edit_original_response(content=content, view=None)
        except Exception as exc:
            logger.exception("刪除重複任務失敗")
            await interaction.followup.send(f"刪除失敗：{exc}", ephemeral=True)


class RecurrenceDeleteButton(discord.ui.Button[RecurrenceDeleteView]):
    def __init__(self, request_id: int, operation: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"recurrence_delete_{operation}_{request_id}")
        self.operation = operation

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, RecurrenceDeleteView):
            await self.view.handle(interaction, self.operation)



class RecurrenceExtensionView(discord.ui.View):
    def __init__(self, rule_id: int, user_id: int) -> None:
        super().__init__(timeout=None)
        self.rule_id, self.user_id = rule_id, user_id
        self.add_item(RecurrenceExtensionButton(rule_id, "extend", f"延長 {REPEAT_HORIZON_DAYS} 天", discord.ButtonStyle.success))
        self.add_item(RecurrenceExtensionButton(rule_id, "end", "結束系列", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的重複系列。", ephemeral=True)
        return False

    async def handle(self, interaction: discord.Interaction, operation: str) -> None:
        with SessionLocal() as session:
            rule = session.get(RecurrenceRule, self.rule_id)
            if not rule or rule.discord_user_id != str(self.user_id):
                await interaction.response.send_message("找不到可處理的重複系列。", ephemeral=True)
                return
            name = rule.task_name
        await interaction.response.defer()
        try:
            if operation == "extend":
                result = await asyncio.to_thread(extend_rule, self.rule_id)
                content = (
                    f"🔁 已將「{name}」延長 {REPEAT_HORIZON_DAYS} 天，"
                    f"新增 {result.created} 筆並立即排入 {result.scheduled} 筆。"
                )
                if result.conflicts:
                    content += f"另有 {result.conflicts} 個固定時段衝突。"
            else:
                await asyncio.to_thread(end_rule, self.rule_id)
                content = f"已結束重複系列「{name}」；歷史任務與評分已保留。"
            await interaction.edit_original_response(content=content, view=None)
        except Exception as exc:
            logger.exception("處理重複系列到期選項失敗")
            await interaction.followup.send(f"操作失敗：{exc}", ephemeral=True)


class RecurrenceExtensionButton(discord.ui.Button[RecurrenceExtensionView]):
    def __init__(self, rule_id: int, operation: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"recurrence_extension_{operation}_{rule_id}")
        self.operation = operation

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, RecurrenceExtensionView):
            await self.view.handle(interaction, self.operation)



class ReplanConfirmationView(discord.ui.View):
    def __init__(self, request_id: int, user_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id, self.user_id = request_id, user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的重排提案。", ephemeral=True)
        return False

    @discord.ui.button(label="確認重排", style=discord.ButtonStyle.success, custom_id="confirm_replan")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await asyncio.to_thread(_load_pending, self.request_id)
        if not row:
            await interaction.response.send_message("重排提案已逾時。", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(apply_replan, json.loads(row.payload_json)["proposal"])
            await asyncio.to_thread(_delete_pending, row.id)
            await interaction.edit_original_response(content="✅ 已依照提案完成重排。", view=None)
        except Exception as exc:
            await interaction.followup.send(f"重排失敗：{exc}", ephemeral=True)

    @discord.ui.button(label="保持原排程", style=discord.ButtonStyle.secondary, custom_id="cancel_replan")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await asyncio.to_thread(_delete_pending, self.request_id)
        await interaction.response.edit_message(content="已保留原排程；尚未排入的任務維持 pending。", view=None)


def _save_efficiency_draft(task_id: int, user_id: int, score: int, segment_id: int | None = None) -> None:
    return save_efficiency_draft(task_id, user_id, score, segment_id)


def _legacy_save_efficiency_draft(task_id: int, user_id: int, score: int) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task or task.status != "feedback_requested":
            raise ValueError("此任務目前不能評分。")
        if session.scalar(select(Feedback.id).where(Feedback.task_id == task_id)):
            raise ValueError("此任務已經完成評分。")
        draft = session.get(FeedbackDraft, task_id)
        if draft is None:
            draft = FeedbackDraft(task_id=task_id, discord_user_id=str(user_id), efficiency_score=score)
            session.add(draft)
        else:
            draft.efficiency_score = score
        session.commit()


def _finalize_feedback(task_id: int, mental: int, segment_id: int | None = None) -> tuple[str, int, list[str]]:
    result = finalize_completed(task_id, mental, segment_id, calendar=GoogleCalendarService())
    return result.task_name, result.efficiency, list(result.event_ids)


def _legacy_finalize_feedback(task_id: int, mental: int) -> tuple[str, int, list[str]]:
    now = datetime.now(TIMEZONE)
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        draft = session.get(FeedbackDraft, task_id)
        if not task or not draft:
            raise ValueError("找不到尚未完成的效率評分。")
        if session.scalar(select(Feedback.id).where(Feedback.task_id == task_id)):
            raise ValueError("此任務已經完成評分。")
        start = task.scheduled_start or _db_time(now)
        end = task.scheduled_end or _db_time(now)
        feedback = Feedback(
            task_id=task.id,
            scheduled_start=start,
            scheduled_end=end,
            time_of_day=_local(start).strftime("%H:%M"),
            efficiency_score=draft.efficiency_score,
            mental_score=mental,
            completion_status="completed",
            rating_method="two_stage",
        )
        name, efficiency = task.task_name, draft.efficiency_score
        event_ids = [value for value in [task.event_id, *(item.event_id for item in task.segments)] if value]
        task.status = "completed"
        session.add(feedback)
        session.delete(draft)
        session.commit()
    calendar = GoogleCalendarService()
    for event_id in event_ids:
        calendar.update_event_feedback(event_id, efficiency, mental)
    return name, efficiency, event_ids


def _finalize_incomplete(task_id: int, reason: str | None, segment_id: int | None = None) -> tuple[str, list[str]]:
    result = finalize_incomplete(task_id, reason, segment_id, calendar=GoogleCalendarService())
    return result.task_name, list(result.event_ids)


def _legacy_finalize_incomplete(task_id: int, reason: str | None) -> tuple[str, list[str]]:
    now = datetime.now(TIMEZONE)
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError("找不到任務。")
        if session.scalar(select(Feedback.id).where(Feedback.task_id == task_id)):
            raise ValueError("此任務已經完成回饋。")
        start = task.scheduled_start or _db_time(now)
        end = task.scheduled_end or _db_time(now)
        session.add(
            Feedback(
                task_id=task.id,
                scheduled_start=start,
                scheduled_end=end,
                time_of_day=_local(start).strftime("%H:%M"),
                efficiency_score=0,
                mental_score=1,
                completion_status="incomplete",
                incomplete_reason=reason,
                rating_method="incomplete",
            )
        )
        draft = session.get(FeedbackDraft, task.id)
        if draft:
            session.delete(draft)
        event_ids = [value for value in [task.event_id, *(item.event_id for item in task.segments)] if value]
        name = task.task_name
        task.status = "incomplete"
        session.commit()
    calendar = GoogleCalendarService()
    for event_id in event_ids:
        calendar.update_event_incomplete(event_id, reason)
    return name, event_ids


class ScoreButton(discord.ui.Button):
    def __init__(self, task_id: int, stage: str, score: int) -> None:
        super().__init__(label=str(score), style=discord.ButtonStyle.primary, custom_id=f"feedback_{stage}_{task_id}_{score}")
        self.score, self.stage = score, stage

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.stage == "efficiency" and isinstance(self.view, EfficiencyFeedbackView):
            await self.view.record(interaction, self.score)
        elif self.stage == "mental" and isinstance(self.view, MentalFeedbackView):
            await self.view.record(interaction, self.score)


class EfficiencyFeedbackView(discord.ui.View):
    def __init__(self, user_id: int, task_id: int, task_name: str) -> None:
        super().__init__(timeout=None)
        self.user_id, self.task_id, self.task_name = user_id, task_id, task_name
        self.add_item(IncompleteButton(task_id))
        for score in range(1, 6):
            self.add_item(ScoreButton(task_id, "efficiency", score))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的回饋邀請。", ephemeral=True)
        return False

    async def record(self, interaction: discord.Interaction, score: int) -> None:
        await interaction.response.defer()
        try:
            await asyncio.to_thread(_save_efficiency_draft, self.task_id, self.user_id, score)
            await interaction.edit_original_response(
                content=f"已記錄「{self.task_name}」效率 {score}/5。當時精神狀況如何？",
                view=MentalFeedbackView(self.user_id, self.task_id, self.task_name),
            )
        except Exception as exc:
            await interaction.followup.send(f"評分失敗：{exc}", ephemeral=True)


class MentalFeedbackView(discord.ui.View):
    def __init__(self, user_id: int, task_id: int, task_name: str) -> None:
        super().__init__(timeout=None)
        self.user_id, self.task_id, self.task_name = user_id, task_id, task_name
        for score in range(1, 6):
            self.add_item(ScoreButton(task_id, "mental", score))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("這不是你的回饋邀請。", ephemeral=True)
        return False

    async def record(self, interaction: discord.Interaction, score: int) -> None:
        await interaction.response.defer()
        try:
            name, efficiency, _events = await asyncio.to_thread(_finalize_feedback, self.task_id, score)
            await interaction.edit_original_response(content=f"已收到「{name}」的回饋：效率 {efficiency}/5、精神 {score}/5。", view=None)
        except Exception as exc:
            logger.exception("兩階段回饋失敗")
            await interaction.followup.send(f"評分失敗：{exc}", ephemeral=True)


class IncompleteButton(discord.ui.Button):
    def __init__(self, task_id: int) -> None:
        super().__init__(label="未完成", style=discord.ButtonStyle.danger, custom_id=f"feedback_incomplete_{task_id}")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, EfficiencyFeedbackView):
            await interaction.response.edit_message(
                content=f"確認將「{view.task_name}」記為未完成嗎？效率將記為 0、精神預設為 1。",
                view=IncompleteConfirmView(view.user_id, view.task_id, view.task_name),
            )


class IncompleteConfirmView(discord.ui.View):
    def __init__(self, user_id: int, task_id: int, task_name: str) -> None:
        super().__init__(timeout=None)
        self.user_id, self.task_id, self.task_name = user_id, task_id, task_name

    @discord.ui.button(label="確認未完成", style=discord.ButtonStyle.danger, custom_id="confirm_incomplete")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"請選擇「{self.task_name}」未完成的原因（可略過）：",
            view=IncompleteReasonView(self.user_id, self.task_id, self.task_name),
        )

    @discord.ui.button(label="返回評分", style=discord.ButtonStyle.secondary, custom_id="cancel_incomplete")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"請評估任務「{self.task_name}」的完成效率：",
            view=EfficiencyFeedbackView(self.user_id, self.task_id, self.task_name),
        )


class IncompleteReasonSelect(discord.ui.Select):
    def __init__(self, task_id: int) -> None:
        options = [
            discord.SelectOption(label=value, value=value)
            for value in ("精神或體力不足", "時間估計不足", "臨時事件中斷", "時段不適合", "任務太困難", "優先度改變", "其他", "略過")
        ]
        super().__init__(placeholder="選擇原因", options=options, custom_id=f"incomplete_reason_{task_id}")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, IncompleteReasonView):
            return
        reason = None if self.values[0] == "略過" else self.values[0]
        await interaction.response.defer()
        try:
            name, _events = await asyncio.to_thread(_finalize_incomplete, self.view.task_id, reason)
            await interaction.edit_original_response(
                content=f"已記錄「{name}」未完成。是否重新安排剩餘工作？",
                view=IncompleteRescheduleView(self.view.user_id, self.view.task_id, name),
            )
        except Exception as exc:
            await interaction.followup.send(f"儲存失敗：{exc}", ephemeral=True)


class IncompleteReasonView(discord.ui.View):
    def __init__(self, user_id: int, task_id: int, task_name: str) -> None:
        super().__init__(timeout=None)
        self.user_id, self.task_id, self.task_name = user_id, task_id, task_name
        self.add_item(IncompleteReasonSelect(task_id))


class RemainingDurationModal(discord.ui.Modal, title="重新安排剩餘工作"):
    duration = discord.ui.TextInput(label="剩餘工作需要多久？", placeholder="例如：一個半小時或 90 分鐘")

    def __init__(self, user_id: int, source_task_id: int, task_name: str) -> None:
        super().__init__()
        self.user_id, self.source_task_id, self.task_name = user_id, source_task_id, task_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        minutes, _original = parse_duration(str(self.duration))
        if not minutes:
            await interaction.response.send_message("無法辨識時長，請輸入例如「90 分鐘」。", ephemeral=True)
            return
        with SessionLocal() as session:
            task = Task(
                task_name=self.task_name,
                estimated_minutes=minutes,
                status="pending",
                discord_user_id=str(self.user_id),
                source_task_id=self.source_task_id,
            )
            session.add(task)
            session.commit()
            task_id = task.id
        result = await asyncio.to_thread(attempt_schedule_task, task_id)
        content = (
            f"✅ 已建立並排定剩餘工作：{result.start:%m/%d %H:%M}–{result.end:%H:%M}。"
            if result.scheduled
            else "✅ 已建立剩餘工作，但目前仍無法排入，已保留為 pending。"
        )
        await interaction.response.send_message(content, ephemeral=True)


class IncompleteRescheduleView(discord.ui.View):
    def __init__(self, user_id: int, task_id: int, task_name: str) -> None:
        super().__init__(timeout=None)
        self.user_id, self.task_id, self.task_name = user_id, task_id, task_name

    @discord.ui.button(label="重新排程", style=discord.ButtonStyle.primary, custom_id="reschedule_incomplete")
    async def reschedule(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RemainingDurationModal(self.user_id, self.task_id, self.task_name))

    @discord.ui.button(label="不再進行", style=discord.ButtonStyle.secondary, custom_id="close_incomplete")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=f"已保留「{self.task_name}」的未完成歷史，不再重新安排。", view=None)


async def send_feedback_request(user_id: int, task_name: str, task_id: int) -> discord.Message:
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    return await user.send(
        f"請評估任務「{task_name}」的完成效率：",
        view=EfficiencyFeedbackView(user_id, task_id, task_name),
    )


async def send_segment_feedback_request(
    user_id: int, task_name: str, task_id: int, segment_id: int,
    segment_index: int, segment_count: int,
) -> discord.Message:
    display_name = f"{task_name}（第 {segment_index}/{segment_count} 段）"
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    return await user.send(
        f"請評估「{display_name}」的完成效率：",
        view=SegmentEfficiencyFeedbackView(user_id, task_id, segment_id, display_name),
    )


async def send_schedule_failure_notification(
    user_id: int, content: str, channel_id: int | None = None
) -> discord.Message:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return await user.send(content)
    except discord.Forbidden:
        if not channel_id:
            raise
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        return await channel.send(f"<@{user_id}> {content}")


async def send_rolling_replan_request(
    user_id: int,
    proposal: dict,
    channel_id: int | None = None,
) -> discord.Message:
    request = await asyncio.to_thread(
        _save_pending,
        user_id,
        channel_id or 0,
        "apply_replan",
        {"action": "apply_replan", "proposal": proposal},
        "每日全局最佳化",
        "awaiting_replan",
    )
    content = _rolling_summary(proposal)
    view = ReplanConfirmationView(request.id, user_id)
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return await user.send(content, view=view)
    except discord.Forbidden:
        if not channel_id:
            raise
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        return await channel.send(f"<@{user_id}> {content}", view=view)


async def send_recurrence_extension_request(
    rule_id: int,
    user_id: int,
    task_name: str,
    cycle_end: date,
    channel_id: int | None = None,
) -> discord.Message:
    content = (
        f"重複系列「{task_name}」已於 {cycle_end:%Y-%m-%d} 到期，是否延長？"
    )
    view = RecurrenceExtensionView(rule_id, user_id)
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return await user.send(content, view=view)
    except discord.Forbidden:
        if not channel_id:
            raise
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        return await channel.send(f"<@{user_id}> {content}", view=view)


def _natural_read_action(text: str) -> str | None:
    """Recognise common read-only requests without calling Gemini."""
    compact = re.sub(r"\s+", "", text.casefold())
    query_words = ("查看", "看看", "顯示", "列出", "哪些", "有什麼", "查詢")
    if any(word in compact for word in query_words) and any(
        word in compact for word in ("規劃", "行程表", "未來安排")
    ):
        return "plan"
    if any(word in compact for word in query_words) and any(
        word in compact for word in ("任務", "待辦", "尚未完成")
    ):
        return "tasks"
    return None


def _natural_task_query(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if any(word in compact for word in ("所有", "全部", "有哪些", "尚未完成")):
        return ""
    return text


def _structured_title(payload: dict) -> str | None:
    title = " ".join(str(payload.get("task_name") or "").split())
    return title if 0 < len(title) <= 100 else None


async def _natural_plan_outcome(user_id: int, days: int = 7) -> ExecutionOutcome:
    async def load(selected_days: int) -> list[PlanEntry]:
        return await asyncio.to_thread(_plan_entries, user_id, selected_days)

    entries = await load(days)
    view = StructuredPlanView(
        user_id=user_id,
        load_callback=load,
        initial_entries=entries,
        days=days,
    )
    return ExecutionOutcome(view.content, view)


async def _natural_delete_outcome(user_id: int) -> ExecutionOutcome:
    entries = await asyncio.to_thread(_delete_entries, user_id)
    if not entries:
        return ExecutionOutcome("目前沒有可刪除的等待排程或已排程任務。")

    async def delete_task(task_id: int, scope: str) -> str:
        if scope == "series":
            entry = next(
                (item for item in entries if item.task_id == task_id), None
            )
            if not entry or not entry.recurrence_group:
                raise ValueError("找不到指定的重複系列。")
            name, count = await asyncio.to_thread(
                delete_series, entry.recurrence_group, user_id
            )
            return (
                f"🗑️ 已結束「{name}」系列並刪除 {count} 筆尚未完成行程；"
                "歷史與評分已保留。"
            )
        name = await asyncio.to_thread(_delete_task, task_id, user_id)
        return f"🗑️ 已刪除「{name}」及其 Calendar 行程。"

    view = StructuredDeleteView(
        user_id=user_id,
        entries=entries,
        delete_callback=delete_task,
    )
    return ExecutionOutcome(view.content, view)


async def _natural_entry_outcome(
    payload: dict,
    user_id: int,
    channel_id: int,
    original_text: str,
) -> ExecutionOutcome:
    """Turn natural-language intent into a safe UI instead of a direct write."""
    action = str(payload.get("action") or "unknown")
    if action == "tasks":
        query = str(payload.get("query") or original_text)
        query = _natural_task_query(query)
        return ExecutionOutcome(
            _format_tasks(_find_tasks_by_text(user_id, query), user_id)
        )
    if action == "plan":
        days = max(1, min(int(payload.get("plan_days") or 7), 30))
        return await _natural_plan_outcome(user_id, days)
    if action in {"add", "fixed", "repeat"}:
        title = _structured_title(payload)
        if not title:
            command = {"add": "/add", "fixed": "/fixed", "repeat": "/repeat"}[action]
            return ExecutionOutcome(
                f"我已判斷要使用 {command}，但無法可靠辨識名稱。"
                "請再說一次名稱，或直接使用該指令。"
            )

        async def submit(form_payload: dict) -> tuple[str, discord.ui.View | None]:
            outcome = await _execute_intent(form_payload, user_id, channel_id)
            return outcome.content, outcome.view

        if action == "add":
            view = StructuredAddView(
                user_id=user_id,
                channel_id=channel_id,
                task_name=title,
                submit_callback=submit,
            )
            return ExecutionOutcome(
                "我判斷這是一般待辦；請用下方選單確認截止時間、時長與分割方式。\n\n"
                + view.summary(),
                view,
            )
        if action == "fixed":
            view = StructuredFixedView(
                user_id=user_id,
                channel_id=channel_id,
                title=title,
                submit_callback=submit,
            )
            return ExecutionOutcome(
                "我判斷這是固定行程；請用下方選單重新確認日期、時間與時長。\n\n"
                + view.summary(),
                view,
            )
        view = StructuredRepeatView(
            user_id=user_id,
            channel_id=channel_id,
            title=title,
            submit_callback=submit,
        )
        return ExecutionOutcome(
            "我判斷這是重複任務；請用下方選單確認星期、時間、時長與期限。\n\n"
            + view.summary(),
            view,
        )
    if action == "delete":
        return await _natural_delete_outcome(user_id)
    if action == "reschedule":
        return ExecutionOutcome(
            "請直接在 Google Calendar 移動該行程；Bob 會在下一個整點或半點同步，"
            "最長約 30 分鐘後反映到 `/tasks` 與 `/plan`。"
        )
    if action in {"reset", "schedule_settings"}:
        return await _route_payload(
            payload, user_id, channel_id, original_text
        )
    return ExecutionOutcome(
        "我無法安全判斷要執行哪項功能。你可以使用 `/add`、`/fixed`、"
        "`/repeat`、`/tasks`、`/plan`、`/delete` 或 `/reset`。"
    )



async def _natural_reply(message: discord.Message, text: str) -> None:
    try:
        read_action = _natural_read_action(text)
        if read_action:
            payload = {
                "action": read_action,
                "query": text if read_action == "tasks" else None,
                "plan_days": 7,
            }
        else:
            payload = await asyncio.to_thread(parse_bot_entry, text)
        outcome = await _natural_entry_outcome(
            payload, message.author.id, message.channel.id, text
        )
        await message.reply(outcome.content, view=outcome.view)
    except Exception as exc:
        logger.exception("自然語言請求失敗")
        await message.reply(f"無法完成此請求：{exc}")


@bot.tree.command(name="add", description="用選單新增一般待辦")
@app_commands.describe(task_name="要完成的事情，例如：期末報告")
async def slash_add(interaction: discord.Interaction, task_name: str) -> None:
    task_name = " ".join(task_name.split())
    if not task_name:
        await interaction.response.send_message("任務名稱不可空白。", ephemeral=True)
        return
    if len(task_name) > 100:
        await interaction.response.send_message("任務名稱最多 100 個字。", ephemeral=True)
        return

    async def submit(payload: dict) -> tuple[str, discord.ui.View | None]:
        outcome = await _execute_intent(
            payload, interaction.user.id, interaction.channel_id or 0
        )
        return outcome.content, outcome.view

    view = StructuredAddView(
        user_id=interaction.user.id,
        channel_id=interaction.channel_id or 0,
        task_name=task_name,
        submit_callback=submit,
    )
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


@bot.tree.command(name="tasks", description="查詢尚未完成任務")
@app_commands.describe(query="可選，例如：8/5 語言練習")
async def slash_tasks(interaction: discord.Interaction, query: str | None = None) -> None:
    await interaction.response.send_message(
        _format_tasks(_find_tasks_by_text(interaction.user.id, query or ""), interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="repeat", description="用選單建立重複任務")
@app_commands.describe(title="重複任務名稱，例如：閱讀")
async def slash_repeat(interaction: discord.Interaction, title: str) -> None:
    title = " ".join(title.split())
    if not title:
        await interaction.response.send_message("系列名稱不可空白。", ephemeral=True)
        return
    if len(title) > 100:
        await interaction.response.send_message("系列名稱最多 100 個字。", ephemeral=True)
        return

    async def submit(payload: dict) -> tuple[str, discord.ui.View | None]:
        outcome = await _execute_intent(
            payload, interaction.user.id, interaction.channel_id or 0
        )
        return outcome.content, outcome.view

    view = StructuredRepeatView(
        user_id=interaction.user.id,
        channel_id=interaction.channel_id or 0,
        title=title,
        submit_callback=submit,
    )
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


@bot.tree.command(name="plan", description="用選單查看 Bob 已排定的行程")
async def slash_plan(interaction: discord.Interaction) -> None:
    async def load(days: int) -> list[PlanEntry]:
        return await asyncio.to_thread(_plan_entries, interaction.user.id, days)

    entries = await load(7)
    view = StructuredPlanView(
        user_id=interaction.user.id,
        load_callback=load,
        initial_entries=entries,
        days=7,
    )
    await interaction.response.send_message(view.content, view=view, ephemeral=True)


@bot.tree.command(name="fixed", description="用選單建立固定時間行程")
@app_commands.describe(title="固定行程名稱，例如：看醫生")
async def slash_fixed(interaction: discord.Interaction, title: str) -> None:
    title = " ".join(title.split())
    if not title:
        await interaction.response.send_message("行程名稱不可空白。", ephemeral=True)
        return
    if len(title) > 100:
        await interaction.response.send_message("行程名稱最多 100 個字。", ephemeral=True)
        return

    async def submit(payload: dict) -> tuple[str, discord.ui.View | None]:
        outcome = await _execute_intent(
            payload, interaction.user.id, interaction.channel_id or 0
        )
        return outcome.content, outcome.view

    view = StructuredFixedView(
        user_id=interaction.user.id,
        channel_id=interaction.channel_id or 0,
        title=title,
        submit_callback=submit,
    )
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


@bot.tree.command(name="delete", description="用選單刪除尚未完成任務")
async def slash_delete(interaction: discord.Interaction) -> None:
    entries = await asyncio.to_thread(_delete_entries, interaction.user.id)
    if not entries:
        await interaction.response.send_message(
            "目前沒有可刪除的等待排程或已排程任務。",
            ephemeral=True,
        )
        return

    async def delete_task(task_id: int, scope: str) -> str:
        if scope == "series":
            entry = next(
                (item for item in entries if item.task_id == task_id), None
            )
            if not entry or not entry.recurrence_group:
                raise ValueError("找不到指定的重複系列。")
            name, count = await asyncio.to_thread(
                delete_series,
                entry.recurrence_group,
                interaction.user.id,
            )
            return (
                f"🗑️ 已結束「{name}」系列並刪除 {count} 筆尚未完成行程；"
                "歷史與評分已保留。"
            )
        name = await asyncio.to_thread(
            _delete_task, task_id, interaction.user.id
        )
        return f"🗑️ 已刪除「{name}」及其 Calendar 行程。"

    view = StructuredDeleteView(
        user_id=interaction.user.id,
        entries=entries,
        delete_callback=delete_task,
    )
    await interaction.response.send_message(view.content, view=view, ephemeral=True)


@bot.tree.command(name="reset", description="清除自己的所有任務與歷史資料")
async def slash_reset(interaction: discord.Interaction) -> None:
    payload = {"action": "reset", "missing_fields": [], "ambiguities": []}
    request = await asyncio.to_thread(_save_pending, interaction.user.id, interaction.channel_id or 0, "reset", payload, "reset", "awaiting_confirmation")
    await interaction.response.send_message(_confirmation_summary(payload), view=ActionConfirmationView(request.id, interaction.user.id), ephemeral=True)


async def _is_reply_to_bot(message: discord.Message) -> bool:
    if not message.reference or not bot.user:
        return False
    resolved = message.reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot.user.id
    try:
        referenced = await message.channel.fetch_message(message.reference.message_id)
        return referenced.author.id == bot.user.id
    except Exception:
        return False


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not bot.user:
        return
    mentioned = bot.user in message.mentions
    if mentioned:
        text = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if text:
            await _natural_reply(message, text)
        return
    if await _is_reply_to_bot(message):
        row = await asyncio.to_thread(_load_pending, None, message.author.id, message.channel.id)
        if row:
            try:
                outcome = await _handle_pending_reply(row, message.content.strip())
                await message.reply(outcome.content, view=outcome.view)
            except Exception as exc:
                logger.exception("處理補充訊息失敗")
                await message.reply(f"無法處理補充內容：{exc}")


@bot.event
async def setup_hook() -> None:
    with SessionLocal() as session:
        pending_feedback = list(session.scalars(select(Task).where(Task.status == "feedback_requested")))
        drafts = {draft.task_id: draft for draft in session.scalars(select(FeedbackDraft))}
        pending_segment_rows = []
        segment_draft_ids = {
            draft.segment_id for draft in session.scalars(select(SegmentFeedbackDraft))
        }
        for segment in session.scalars(
            select(TaskSegment).where(TaskSegment.status == "feedback_requested")
        ):
            task = session.get(Task, segment.task_id)
            if not task:
                continue
            segment_count = len(
                list(
                    session.scalars(
                        select(TaskSegment).where(TaskSegment.task_id == task.id)
                    )
                )
            )
            pending_segment_rows.append(
                (
                    segment.id,
                    task.id,
                    segment.segment_index,
                    segment_count,
                    task.task_name,
                    task.discord_user_id,
                    segment.id in segment_draft_ids,
                )
            )
        pending_actions = list(session.scalars(select(PendingRequest)))
        pending_extensions = list(
            session.scalars(
                select(RecurrenceRule).where(RecurrenceRule.status == "awaiting_extension")
            )
        )
    for task in pending_feedback:
        owner = task.discord_user_id or DISCORD_USER_ID
        if not owner:
            continue
        if task.id in drafts:
            bot.add_view(MentalFeedbackView(int(owner), task.id, task.task_name))
        else:
            bot.add_view(EfficiencyFeedbackView(int(owner), task.id, task.task_name))
    for segment_id, task_id, index, count, task_name, owner_id, has_draft in pending_segment_rows:
        owner = owner_id or DISCORD_USER_ID
        if not owner:
            continue
        display_name = f"{task_name}（第 {index}/{count} 段）"
        if has_draft:
            bot.add_view(
                SegmentMentalFeedbackView(
                    int(owner), task_id, segment_id, display_name
                )
            )
        else:
            bot.add_view(
                SegmentEfficiencyFeedbackView(
                    int(owner), task_id, segment_id, display_name
                )
            )
    for row in pending_actions:
        if row.state == "awaiting_confirmation":
            bot.add_view(ActionConfirmationView(row.id, int(row.discord_user_id)))
        elif row.state == "awaiting_recurrence_delete":
            bot.add_view(RecurrenceDeleteView(row.id, int(row.discord_user_id)))
        elif row.state == "awaiting_replan":
            bot.add_view(ReplanConfirmationView(row.id, int(row.discord_user_id)))
    for rule in pending_extensions:
        bot.add_view(
            RecurrenceExtensionView(rule.id, int(rule.discord_user_id))
        )
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    logger.info("已登入 Discord：%s", bot.user)


def main() -> None:
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("缺少 DISCORD_BOT_TOKEN。")
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
