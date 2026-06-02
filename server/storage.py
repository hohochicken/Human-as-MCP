"""
Storage backend — SQLite persistence for tasks.

Provides the ``Storage`` class that encapsulates all database operations.
All public I/O methods are async, delegating synchronous sqlite3 calls to
a thread-pool executor so the asyncio event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from server.models import Task, TaskStatus, Priority, RejectionReason
from server.constants import MAX_TITLE_LENGTH, MAX_DESCRIPTION_LENGTH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    tool_name        TEXT NOT NULL DEFAULT 'human_task',
    priority         TEXT NOT NULL DEFAULT 'normal',
    status           TEXT NOT NULL DEFAULT 'pending',
    agent_id         TEXT NOT NULL DEFAULT 'unknown',
    created_at       TEXT NOT NULL,
    deadline_minutes INTEGER NOT NULL DEFAULT 120,
    result           TEXT,
    evidence         TEXT,
    rejection_reason TEXT,
    rejection_note   TEXT,
    completed_at     TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_action    TEXT,
    cancel_new_data  TEXT,
    params_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_id ON tasks(agent_id);
"""

_MIGRATIONS: dict[int, str] = {
    # version -> SQL to upgrade FROM (version-1) TO version
    # 2: "ALTER TABLE tasks ADD COLUMN params_json TEXT;",
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class Storage:
    """SQLite-backed persistent task storage.

    All public methods are **async** — synchronous sqlite3 calls run inside
    ``loop.run_in_executor`` so they never block the asyncio event loop.

    Parameters
    ----------
    db_path : str
        Absolute or relative path to the SQLite database file.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._executor: Optional[asyncio.AbstractEventLoop] = None

        # Ensure the parent directory exists.
        parent = Path(self._db_path).parent
        os.makedirs(parent, exist_ok=True)

    # -- internal ------------------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Return the running event loop (cached after first call)."""
        if self._executor is None:
            self._executor = asyncio.get_running_loop()
        return self._executor

    def _connect(self) -> sqlite3.Connection:
        """Open a new SQLite connection for the current thread."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create tables, run migrations, and verify integrity.

        Must be called once before any other public method.
        """
        loop = self._get_loop()

        def _init() -> None:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _init)

        # Run any pending migrations.
        await self._migrate()

        # Verify database integrity.
        ok, detail = await self.check_integrity()
        if not ok:
            logger.error("Database integrity check FAILED: %s", detail)
            raise RuntimeError(f"Database integrity check failed: {detail}")
        logger.info("Database integrity check passed.")

    async def _migrate(self) -> None:
        """Apply any pending schema migrations."""
        loop = self._get_loop()

        def _get_version(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            return row["v"] if row and row["v"] is not None else 0

        def _run() -> int:
            conn = self._connect()
            try:
                current = _get_version(conn)
                for ver in range(current + 1, _SCHEMA_VERSION + 1):
                    sql = _MIGRATIONS.get(ver)
                    if sql:
                        conn.executescript(sql)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                        (ver,),
                    )
                    conn.commit()
                    logger.info("Applied schema migration v%d.", ver)
                return current
            finally:
                conn.close()

        before = await loop.run_in_executor(None, _run)
        if before < _SCHEMA_VERSION:
            logger.info(
                "Schema migrated from v%d to v%d.", before, _SCHEMA_VERSION
            )

    async def check_integrity(self) -> tuple[bool, str]:
        """Run ``PRAGMA integrity_check`` and return ``(ok, detail)``."""
        loop = self._get_loop()

        def _check() -> tuple[bool, str]:
            try:
                conn = self._connect()
                try:
                    row = conn.execute("PRAGMA integrity_check").fetchone()
                    result = row[0] if row else "no result"
                    return (result == "ok", str(result))
                finally:
                    conn.close()
            except Exception as exc:
                return (False, str(exc))

        return await loop.run_in_executor(None, _check)

    # -- CRUD ----------------------------------------------------------------

    async def save_task(self, task: Task) -> None:
        """Insert a new task row."""
        loop = self._get_loop()

        def _save() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, title, description, tool_name, priority, status,
                        agent_id, created_at, deadline_minutes, result, evidence,
                        rejection_reason, rejection_note, completed_at,
                        cancel_requested, cancel_action, cancel_new_data, params_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.description,
                        task.tool_name,
                        task.priority.value,
                        task.status.value,
                        task.agent_id,
                        task.created_at,
                        task.deadline_minutes,
                        task.result,
                        json.dumps(task.evidence) if task.evidence else None,
                        task.rejection_reason.value if task.rejection_reason else None,
                        task.rejection_note,
                        task.completed_at,
                        int(task.cancel_requested),
                        task.cancel_action,
                        json.dumps(task.cancel_new_data) if task.cancel_new_data else None,
                        json.dumps(task.params) if task.params else None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _save)

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Return the Task for *task_id*, or ``None``."""
        loop = self._get_loop()

        def _get() -> Optional[sqlite3.Row]:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            finally:
                conn.close()

        row = await loop.run_in_executor(None, _get)
        if row is None:
            return None
        return self._row_to_task(row)

    async def get_tasks_by_status(self, status: TaskStatus) -> list[Task]:
        """Return all tasks with the given status, newest first."""
        loop = self._get_loop()

        def _get() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                    (status.value,),
                ).fetchall()
            finally:
                conn.close()

        rows = await loop.run_in_executor(None, _get)
        return [self._row_to_task(r) for r in rows]

    async def get_history_tasks(
        self, limit: int = 100, offset: int = 0
    ) -> list[Task]:
        """Return completed/rejected tasks, newest first, with pagination."""
        loop = self._get_loop()

        def _get() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                return conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status IN ('completed', 'rejected')
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            finally:
                conn.close()

        rows = await loop.run_in_executor(None, _get)
        return [self._row_to_task(r) for r in rows]

    async def update_task(self, task: Task) -> None:
        """Update all fields of an existing task."""
        loop = self._get_loop()

        def _update() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE tasks SET
                        title = ?, description = ?, tool_name = ?, priority = ?,
                        status = ?, agent_id = ?, created_at = ?,
                        deadline_minutes = ?, result = ?, evidence = ?,
                        rejection_reason = ?, rejection_note = ?,
                        completed_at = ?, cancel_requested = ?,
                        cancel_action = ?, cancel_new_data = ?, params_json = ?
                    WHERE task_id = ?
                    """,
                    (
                        task.title,
                        task.description,
                        task.tool_name,
                        task.priority.value,
                        task.status.value,
                        task.agent_id,
                        task.created_at,
                        task.deadline_minutes,
                        task.result,
                        json.dumps(task.evidence) if task.evidence else None,
                        task.rejection_reason.value if task.rejection_reason else None,
                        task.rejection_note,
                        task.completed_at,
                        int(task.cancel_requested),
                        task.cancel_action,
                        json.dumps(task.cancel_new_data) if task.cancel_new_data else None,
                        json.dumps(task.params) if task.params else None,
                        task.task_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _update)

    # -- Rate-limit query ----------------------------------------------------

    async def get_recent_tasks(self, hours: float = 1.0) -> list[dict]:
        """Return tasks created within the last *hours* as lightweight dicts.

        Each dict has at least ``agent_id`` (str) and ``created_at`` (ISO str).
        Used by ``boundary_gate.check_rate_limit()``.
        """
        loop = self._get_loop()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        def _get() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT agent_id, created_at FROM tasks WHERE created_at >= ?",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()

        rows = await loop.run_in_executor(None, _get)
        return [
            {"agent_id": r["agent_id"], "created_at": r["created_at"]}
            for r in rows
        ]

    # -- Stats ---------------------------------------------------------------

    async def get_task_counts(self) -> dict[str, int]:
        """Return ``{"pending": N, "completed": N, "rejected": N, "total": N}``."""
        loop = self._get_loop()

        def _get() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
                ).fetchall()
            finally:
                conn.close()

        rows = await loop.run_in_executor(None, _get)

        counts: dict[str, int] = {
            "pending": 0, "completed": 0, "rejected": 0, "cancelled": 0, "total": 0
        }
        for row in rows:
            status = row["status"]
            cnt = row["cnt"]
            counts[status] = cnt
            counts["total"] += cnt
        return counts

    # -- Serialisation -------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """Deserialise a database row into a ``Task`` model.

        Enum parsing is wrapped in try-except so that unknown future values
        don't crash the server — they fall back to safe defaults.
        """

        def _parse_json(raw: Optional[str]) -> Optional[list | dict]:
            if raw is None:
                return None
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None

        # Robust enum parsing: missing / unknown values fall back to defaults.
        try:
            priority = Priority(row["priority"])
        except (ValueError, KeyError):
            priority = Priority.NORMAL
            logger.warning(
                "Unknown priority %r for task %s — falling back to normal.",
                row["priority"] if "priority" in row.keys() else "<missing>",
                row["task_id"],
            )

        try:
            status = TaskStatus(row["status"])
        except (ValueError, KeyError):
            status = TaskStatus.PENDING
            logger.warning(
                "Unknown status %r for task %s — falling back to pending.",
                row["status"] if "status" in row.keys() else "<missing>",
                row["task_id"],
            )

        rejection_reason = None
        raw_reason = row["rejection_reason"] if "rejection_reason" in row.keys() else None
        if raw_reason:
            try:
                rejection_reason = RejectionReason(raw_reason)
            except ValueError:
                logger.warning(
                    "Unknown rejection_reason %r for task %s.",
                    raw_reason,
                    row["task_id"],
                )

        # Safe column access — use .keys() check for optional columns
        # that may not exist in older databases.
        def _col(key: str, default=None):
            if key in row.keys():
                return row[key]
            return default

        return Task(
            task_id=row["task_id"],
            title=_col("title", "")[:MAX_TITLE_LENGTH],
            description=_col("description", "")[:MAX_DESCRIPTION_LENGTH],
            tool_name=_col("tool_name", "human_task"),
            priority=priority,
            status=status,
            agent_id=_col("agent_id", "unknown"),
            created_at=_col("created_at", ""),
            deadline_minutes=_col("deadline_minutes", 120),
            result=_col("result"),
            evidence=_parse_json(_col("evidence")),
            rejection_reason=rejection_reason,
            rejection_note=_col("rejection_note"),
            completed_at=_col("completed_at"),
            cancel_requested=bool(_col("cancel_requested", 0)),
            cancel_action=_col("cancel_action"),
            cancel_new_data=_parse_json(_col("cancel_new_data")),
            params=_parse_json(_col("params_json")),
        )

    def __repr__(self) -> str:
        return f"Storage(db_path={self._db_path!r})"
