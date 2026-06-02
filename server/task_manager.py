"""
Task manager — persistent task storage and lifecycle.

Provides the ``TaskManager`` class that wraps SQLite-backed CRUD operations
and is the single entry-point for every ``human_*`` MCP tool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from server.models import Task, TaskCreateRequest, TaskStatus, Priority, RejectionReason

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskManager
# ---------------------------------------------------------------------------


class TaskManager:
    """Manages the full task lifecycle backed by a ``Storage`` instance.

    Parameters
    ----------
    storage:
        The persistent storage backend (SQLite via ``Storage``).
    config:
        The merged server configuration dictionary.
    """

    def __init__(self, storage: Any, config: dict[str, Any]) -> None:
        self._storage = storage
        self._config = config

        defaults = config.get("task_defaults", {})
        self._default_priority = defaults.get("default_priority", "normal")
        self._default_deadline = defaults.get("default_deadline_minutes", 120)
        self._max_title = defaults.get("max_title_length", 200)
        self._max_description = defaults.get("max_description_length", 10000)

    # -- Create --------------------------------------------------------------

    async def create_task(
        self,
        title: str,
        description: str,
        tool_name: str = "human_task",
        priority: str = "normal",
        deadline_minutes: int = 120,
        agent_id: str = "unknown",
        **params: Any,
    ) -> dict:
        """Persist a new task and return its dictionary representation.

        Parameters
        ----------
        title : str
            Short human-readable title.
        description : str
            Full task description / question body.
        tool_name : str
            Name of the originating MCP tool (e.g. ``"human_information"``).
        priority : str
            One of ``low`` / ``normal`` / ``high`` / ``critical``.
        deadline_minutes : int
            SLA window in minutes.
        agent_id : str
            Identifier for the calling AI agent.
        **params
            Additional tool-specific key-value pairs stored on the task.

        Returns
        -------
        dict
            The full task dictionary (see ``Task.to_dict()``).
        """
        request = TaskCreateRequest(
            title=title,
            description=description,
            tool_name=tool_name,
            priority=priority,
            deadline_minutes=deadline_minutes,
            agent_id=agent_id,
            params=params,
        )

        task = Task(
            title=request.title,
            description=request.description,
            tool_name=request.tool_name,
            priority=Priority(request.priority),
            deadline_minutes=request.deadline_minutes,
            agent_id=request.agent_id,
            params=params if params else None,
        )

        # Persist via storage backend.
        self._storage.save_task(task)

        logger.info(
            "Task created: id=%s title=%r tool=%s priority=%s agent=%s",
            task.task_id,
            task.title,
            task.tool_name,
            task.priority.value,
            task.agent_id,
        )

        return task.to_dict()

    # -- Read ----------------------------------------------------------------

    async def get_task(self, task_id: str) -> Optional[dict]:
        """Return the task dictionary for *task_id*, or ``None`` if not found."""
        task = self._storage.get_task(task_id)
        if task is None:
            return None
        return task.to_dict()

    async def get_pending_tasks(self) -> list[dict]:
        """Return all tasks currently in ``pending`` status, sorted newest-first."""
        tasks = self._storage.get_tasks_by_status(TaskStatus.PENDING)
        return [t.to_dict() for t in tasks]

    async def get_history_tasks(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Return completed/rejected tasks with pagination, newest-first."""
        tasks = self._storage.get_history_tasks(limit=limit, offset=offset)
        return [t.to_dict() for t in tasks]

    # -- Update / lifecycle --------------------------------------------------

    async def complete_task(
        self, task_id: str, result: str, evidence: list[str]
    ) -> dict:
        """Mark a pending task as completed.

        Raises ``ValueError`` if the task does not exist or is not pending.
        """
        task = self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.status != TaskStatus.PENDING:
            raise ValueError(
                f"Task {task_id} is '{task.status.value}', not pending."
            )

        task.status = TaskStatus.COMPLETED
        task.result = result or ""
        task.evidence = evidence or []
        task.completed_at = datetime.now(timezone.utc).isoformat()

        self._storage.update_task(task)

        logger.info("Task completed: id=%s", task_id)
        return task.to_dict()

    async def reject_task(
        self, task_id: str, reason: str, note: str
    ) -> dict:
        """Reject a pending task with a reason and optional note.

        Raises ``ValueError`` if the task does not exist or is not pending.
        """
        task = self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.status != TaskStatus.PENDING:
            raise ValueError(
                f"Task {task_id} is '{task.status.value}', not pending."
            )

        task.status = TaskStatus.REJECTED
        task.completed_at = datetime.now(timezone.utc).isoformat()

        # Normalise rejection reason.
        mapped = _normalise_rejection_reason(reason)
        task.rejection_reason = mapped
        task.rejection_note = note or ""

        self._storage.update_task(task)

        logger.info("Task rejected: id=%s reason=%s", task_id, mapped.value if mapped else reason)
        return task.to_dict()

    async def cancel_task(self, task_id: str, reason: Optional[str] = None) -> dict:
        """Request cancellation for a pending task.

        Marks ``cancel_requested=True`` so the human can confirm via the
        dashboard.
        """
        task = self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        task.cancel_requested = True
        task.cancel_action = "cancel"
        if reason:
            task.cancel_new_data = {"reason": reason}

        self._storage.update_task(task)

        logger.info("Task cancel requested: id=%s", task_id)
        return task.to_dict()

    async def confirm_cancel(self, task_id: str) -> dict:
        """Confirm a previously requested cancellation.

        Transitions the task to ``rejected`` with reason ``ai_can_do``.
        """
        task = self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if not task.cancel_requested:
            raise ValueError(f"Task {task_id} has no pending cancel request.")

        task.status = TaskStatus.REJECTED
        task.rejection_reason = RejectionReason.AI_CAN_DO
        task.rejection_note = "Cancelled by human (AI-requested cancel confirmed)."
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.cancel_requested = False

        self._storage.update_task(task)

        logger.info("Task cancel confirmed: id=%s", task_id)
        return task.to_dict()

    async def update_task(self, task_id: str, new_data: dict) -> dict:
        """Update mutable fields on a pending task.

        Allowed keys: ``title``, ``description``, ``priority``.
        """
        task = self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        allowed = {"title", "description", "priority"}
        for key, value in new_data.items():
            if key not in allowed:
                continue
            if key == "priority":
                try:
                    setattr(task, key, Priority(value))
                except ValueError:
                    pass  # Silently ignore invalid priorities.
            else:
                setattr(task, key, str(value)[:self._max_description if key == "description" else self._max_title])

        self._storage.update_task(task)
        logger.info("Task updated: id=%s keys=%s", task_id, list(new_data.keys()))
        return task.to_dict()

    # -- Stats ---------------------------------------------------------------

    async def get_stats(self) -> dict[str, int]:
        """Return aggregate task counts."""
        return self._storage.get_task_counts()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_rejection_reason(raw: str) -> Optional[RejectionReason]:
    """Map a raw reason string to a ``RejectionReason`` enum member."""
    if not raw:
        return None
    value = raw.strip().lower()
    # Accept both the enum values and human-friendly aliases.
    mapping: dict[str, RejectionReason] = {
        "ai_can_do": RejectionReason.AI_CAN_DO,
        "ai can do": RejectionReason.AI_CAN_DO,
        "unclear": RejectionReason.UNCLEAR,
        "unclear_instruction": RejectionReason.UNCLEAR,
        "out_of_scope": RejectionReason.OUT_OF_SCOPE,
        "out of scope": RejectionReason.OUT_OF_SCOPE,
        "invalid_task": RejectionReason.INVALID_TASK,
        "invalid": RejectionReason.INVALID_TASK,
    }
    return mapping.get(value)
