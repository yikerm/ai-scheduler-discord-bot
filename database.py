"""SQLAlchemy models and idempotent SQLite schema upgrades."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
SCHEMA_VERSION = 6


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_name: Mapped[str] = mapped_column(String, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    discord_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scheduled_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scheduled_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    available_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurrence_group: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    recurrence_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_split: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_segment_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    source_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id"), nullable=True
    )
    last_schedule_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    schedule_failure_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    schedule_failure_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    schedule_failure_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    schedule_failure_notified_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    schedule_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    feedback_entries: Mapped[list["Feedback"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    segments: Mapped[list["TaskSegment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskSegment.segment_index",
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    segment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_segments.id", ondelete="CASCADE"), nullable=True
    )
    actual_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    time_of_day: Mapped[str] = mapped_column(String, nullable=False)
    efficiency_score: Mapped[int] = mapped_column(Integer, nullable=False)
    mental_score: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_status: Mapped[str] = mapped_column(
        String, nullable=False, default="completed"
    )
    incomplete_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rating_method: Mapped[str] = mapped_column(
        String, nullable=False, default="two_stage"
    )

    task: Mapped[Task] = relationship(back_populates="feedback_entries")


class FeedbackDraft(Base):
    __tablename__ = "feedback_drafts"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    discord_user_id: Mapped[str] = mapped_column(String, nullable=False)
    efficiency_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class TaskSegment(Base):
    __tablename__ = "task_segments"
    __table_args__ = (UniqueConstraint("task_id", "segment_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="scheduled")

    task: Mapped[Task] = relationship(back_populates="segments")


class SegmentFeedbackDraft(Base):
    __tablename__ = "segment_feedback_drafts"

    segment_id: Mapped[int] = mapped_column(
        ForeignKey("task_segments.id", ondelete="CASCADE"), primary_key=True
    )
    discord_user_id: Mapped[str] = mapped_column(String, nullable=False)
    efficiency_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class PendingRequest(Base):
    __tablename__ = "pending_requests"
    __table_args__ = (UniqueConstraint("discord_user_id", "channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String, nullable=False)
    channel_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="collecting")
    action: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    missing_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class RecurrenceRule(Base):
    __tablename__ = "recurrence_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    discord_user_id: Mapped[str] = mapped_column(String, nullable=False)
    source_channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    task_name: Mapped[str] = mapped_column(String, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    fixed_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    allow_split: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_segment_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    renewal_mode: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    final_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    generated_through: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    extension_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)



class WorkSchedule(Base):
    __tablename__ = "work_schedules"
    __table_args__ = (UniqueConstraint("mode", "day_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    day_type: Mapped[str] = mapped_column(String, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleModePeriod(Base):
    __tablename__ = "schedule_mode_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, unique=True)


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class CalendarSyncState(Base):
    __tablename__ = "calendar_sync_state"

    calendar_id: Mapped[str] = mapped_column(String, primary_key=True)
    sync_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if isinstance(dbapi_connection, __import__("sqlite3").Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


TASK_ADDITIONS = {
    "discord_user_id": "VARCHAR",
    "source_channel_id": "VARCHAR",
    "scheduled_start": "DATETIME",
    "scheduled_end": "DATETIME",
    "available_from": "DATETIME",
    "is_fixed": "BOOLEAN NOT NULL DEFAULT 0",
    "recurrence_group": "VARCHAR",
    "recurrence_date": "DATE",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "is_locked": "BOOLEAN NOT NULL DEFAULT 0",
    "allow_split": "BOOLEAN NOT NULL DEFAULT 0",
    "min_segment_minutes": "INTEGER NOT NULL DEFAULT 30",
    "source_task_id": "INTEGER REFERENCES tasks(id)",
    "last_schedule_attempt_at": "DATETIME",
    "schedule_failure_reason": "VARCHAR",
    "schedule_failure_details": "TEXT",
    "schedule_failure_notified_at": "DATETIME",
    "schedule_failure_notified_reason": "VARCHAR",
    "schedule_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "DATETIME",
}

FEEDBACK_ADDITIONS = {
    "completion_status": "VARCHAR NOT NULL DEFAULT 'completed'",
    "incomplete_reason": "VARCHAR",
    "rating_method": "VARCHAR NOT NULL DEFAULT 'legacy_overall'",
    "segment_id": "INTEGER REFERENCES task_segments(id) ON DELETE CASCADE",
    "actual_minutes": "INTEGER",
}

TASK_SEGMENT_ADDITIONS = {
    "status": "VARCHAR NOT NULL DEFAULT 'scheduled'",
}


RECURRENCE_RULE_ADDITIONS = {
    "min_segment_minutes": "INTEGER NOT NULL DEFAULT 30",
}

def _add_missing_columns(
    connection, table_name: str, definitions: dict[str, str]
) -> None:
    existing = {
        column["name"] for column in inspect(connection).get_columns(table_name)
    }
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")
            )


def _seed_work_schedules() -> None:
    with SessionLocal() as session:
        for mode in ("school", "not_school"):
            for day_type in ("weekday", "weekend"):
                exists = session.scalar(
                    text(
                        "SELECT 1 FROM work_schedules "
                        "WHERE mode = :mode AND day_type = :day_type"
                    ),
                    {"mode": mode, "day_type": day_type},
                )
                if not exists:
                    session.add(
                        WorkSchedule(
                            mode=mode,
                            day_type=day_type,
                            start_minute=9 * 60,
                            end_minute=22 * 60,
                        )
                    )
        if session.scalar(text("SELECT COUNT(*) FROM schedule_mode_periods")) == 0:
            session.add(
                ScheduleModePeriod(mode="not_school", effective_from=date(1970, 1, 1))
            )
        session.commit()


def init_db() -> None:
    """Create new tables and upgrade existing SQLite data without resetting it."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "tasks" in tables:
            _add_missing_columns(connection, "tasks", TASK_ADDITIONS)
        if "feedback" in tables:
            _add_missing_columns(connection, "feedback", FEEDBACK_ADDITIONS)
        if "task_segments" in tables:
            _add_missing_columns(connection, "task_segments", TASK_SEGMENT_ADDITIONS)
        if "recurrence_rules" in tables:
            _add_missing_columns(
                connection, "recurrence_rules", RECURRENCE_RULE_ADDITIONS
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_recurrence_occurrence "
                "ON tasks(recurrence_group, recurrence_date) "
                "WHERE recurrence_group IS NOT NULL AND recurrence_date IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_segment "
                "ON feedback(segment_id) WHERE segment_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE tasks SET status = 'completed' "
                "WHERE status = 'feedback_requested' "
                "AND EXISTS (SELECT 1 FROM feedback WHERE feedback.task_id = tasks.id)"
            )
        )
        connection.execute(
            text("UPDATE tasks SET is_locked = 1 WHERE is_fixed = 1")
        )
        connection.execute(
            text(
                "UPDATE tasks SET created_at = COALESCE(created_at, "
                "scheduled_start, deadline, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (:version, CURRENT_TIMESTAMP)"
            ),
            {"version": SCHEMA_VERSION},
        )
    _seed_work_schedules()


init_db()
