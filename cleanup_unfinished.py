"""Safely preview or remove unfinished tasks and their Calendar events.

Dry-run is the default. Pass ``--execute`` only after reviewing the exact list.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from gcal_service import GoogleCalendarService


UNFINISHED_STATUSES = ("pending", "scheduled", "feedback_requested")


def _targets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in UNFINISHED_STATUSES)
    return list(
        connection.execute(
            f"""
            SELECT id, task_name, status, scheduled_start, deadline, event_id
            FROM tasks
            WHERE status IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM feedback WHERE feedback.task_id = tasks.id
              )
            ORDER BY id
            """,
            UNFINISHED_STATUSES,
        )
    )


def _backup_database(source: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = source.with_name(f"{source.stem}.before-cleanup-{stamp}{source.suffix}")
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as backup_connection:
            source_connection.backup(backup_connection)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data.db")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    database = Path(args.database).resolve()
    if not database.is_file():
        raise SystemExit(f"找不到資料庫：{database}")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = _targets(connection)

    print(f"資料庫：{database}")
    print(f"符合條件的未完成任務：{len(rows)} 筆")
    for row in rows:
        when = row["scheduled_start"] or row["deadline"] or "未指定"
        print(f"- ID {row['id']} [{row['status']}] {row['task_name']}（{when}）")

    if not args.execute:
        print("預覽完成：未修改資料庫或 Google Calendar。")
        return 0
    if not rows:
        print("沒有需要清理的任務。")
        return 0

    backup = _backup_database(database)
    print(f"已建立清理前備份：{backup}")

    calendar = GoogleCalendarService()
    deleted_ids: list[int] = []
    for row in rows:
        event_id = row["event_id"]
        if event_id:
            try:
                calendar.delete_event(event_id)
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status not in (404, 410):
                    raise RuntimeError(
                        f"Calendar 行程刪除失敗，停止清理：Task ID {row['id']}"
                    ) from exc
        deleted_ids.append(int(row["id"]))

    placeholders = ", ".join("?" for _ in deleted_ids)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"DELETE FROM tasks WHERE id IN ({placeholders})",
            deleted_ids,
        )
        connection.commit()

    print(f"清理完成：已刪除 {len(deleted_ids)} 筆任務及其 Calendar 行程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
