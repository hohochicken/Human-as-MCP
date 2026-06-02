"""
Shared task-creation pipeline used by all ``human_*`` MCP tools.

Centralises the flow that was previously duplicated across ``app.py`` and
the ``tools/`` modules:

1. Rate-limit gate
2. Input sanitisation
3. Task creation (via TaskManager)
4. Desktop toast notification
5. WebSocket broadcast
6. 180-second synchronous blocking wait (with async fallback)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable, Optional

from server.constants import (
    MAX_TITLE_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MIN_DEADLINE_MINUTES,
    MAX_DEADLINE_MINUTES,
    BLOCK_TIMEOUT,
    POLL_INTERVAL,
    DEFAULT_PRIORITY,
    DEFAULT_DEADLINE_MINUTES,
)
from server.notification import send_toast_notification, Task as NotificationTask
from server import boundary_gate

logger = logging.getLogger("task_pipeline")

# Type alias for the broadcast callback (injected by app.py).
BroadcastFunc = Callable[[dict], Awaitable[None]]


async def run_pipeline(
    *,
    title: str,
    description: str,
    tool_name: str,
    priority: str = DEFAULT_PRIORITY,
    deadline_minutes: int = DEFAULT_DEADLINE_MINUTES,
    agent_id: str = "unknown",
    task_manager: Any,
    storage: Any,
    broadcast: Optional[BroadcastFunc] = None,
    **extra_params: Any,
) -> dict:
    """Execute the full task-creation pipeline and return a result dict.

    Parameters
    ----------
    title : str
        Short human-readable title (truncated to MAX_TITLE_LENGTH).
    description : str
        Full task description (truncated to MAX_DESCRIPTION_LENGTH).
    tool_name : str
        Name of the originating MCP tool (e.g. ``"human_action"``).
    priority : str
        One of ``low`` / ``normal`` / ``high`` / ``critical``.
    deadline_minutes : int
        SLA window in minutes (clamped 1–10080).
    agent_id : str
        Identifier for the calling AI agent.
    task_manager : TaskManager
        The task manager instance.
    storage : Storage
        The storage backend (for rate-limit queries).
    broadcast : callable, optional
        Async function ``broadcast(message: dict)`` for WebSocket push.
    **extra_params
        Tool-specific key-value pairs stored on the task (steps, action_type,
        command, options, etc.).

    Returns
    -------
    dict
        - On synchronous completion/rejection within BLOCK_TIMEOUT seconds:
          ``{"task_id": ..., "status": "completed"|"rejected", "sync": True, ...}``
        - On timeout:
          ``{"task_id": ..., "status": "pending", "sync": False, "message": "..."}``
    """
    # ------------------------------------------------------------------
    # 1. Rate-limit gate
    # ------------------------------------------------------------------
    allowed, reason = await boundary_gate.check_rate_limit(agent_id, storage)
    if not allowed:
        return {"status": "rejected", "reason": reason}

    # ------------------------------------------------------------------
    # 2. Sanitise inputs
    # ------------------------------------------------------------------
    title = title.strip()[:MAX_TITLE_LENGTH]
    description = description.strip()[:MAX_DESCRIPTION_LENGTH]
    priority = priority.strip().lower() or DEFAULT_PRIORITY
    deadline_minutes = max(MIN_DEADLINE_MINUTES, min(deadline_minutes, MAX_DEADLINE_MINUTES))

    if not title:
        return {"status": "error", "message": "title must be a non-empty string."}

    # ------------------------------------------------------------------
    # 3. Create task
    # ------------------------------------------------------------------
    task: dict = await task_manager.create_task(
        title=title,
        description=description,
        tool_name=tool_name,
        priority=priority,
        deadline_minutes=deadline_minutes,
        agent_id=agent_id,
        **extra_params,
    )

    task_id: str = task["task_id"]

    # ------------------------------------------------------------------
    # 4. Toast notification (fire-and-forget — don't block on failure)
    # ------------------------------------------------------------------
    try:
        notif = NotificationTask(title=title, priority=priority, agent_id=agent_id)
        await send_toast_notification(notif)
    except Exception:
        logger.debug("Toast notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # 5. WebSocket broadcast
    # ------------------------------------------------------------------
    if broadcast is not None:
        try:
            await broadcast({"type": "new_task", "task": task})
        except Exception:
            logger.debug("WebSocket broadcast failed", exc_info=True)

    # ------------------------------------------------------------------
    # 6. Block-wait up to BLOCK_TIMEOUT seconds for quick human response
    # ------------------------------------------------------------------
    elapsed = 0

    while elapsed < BLOCK_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            current = await task_manager.get_task(task_id)
        except Exception:
            continue

        if current is None:
            continue

        status = current.get("status")
        if status == "completed":
            logger.info(
                "Task %s completed synchronously (human responded in ~%ds)",
                task_id, elapsed,
            )
            return {
                "task_id": task_id,
                "status": "completed",
                "sync": True,
                "result": current.get("result"),
                "evidence": current.get("evidence"),
                "completed_at": current.get("completed_at"),
                "message": "Human responded within the blocking window.",
            }
        elif status == "rejected":
            logger.info(
                "Task %s rejected synchronously (human responded in ~%ds)",
                task_id, elapsed,
            )
            return {
                "task_id": task_id,
                "status": "rejected",
                "sync": True,
                "rejection_reason": current.get("rejection_reason"),
                "rejection_note": current.get("rejection_note"),
                "completed_at": current.get("completed_at"),
                "message": f"Human rejected the task: {current.get('rejection_reason', 'unknown')}",
            }

    # ------------------------------------------------------------------
    # Timeout — return task_id for async polling
    # ------------------------------------------------------------------
    logger.info(
        "Task %s did not get human response within %ds, falling back to async",
        task_id, BLOCK_TIMEOUT,
    )
    return {
        "task_id": task_id,
        "status": "pending",
        "sync": False,
        "message": (
            f"Task queued (no response within {BLOCK_TIMEOUT}s). "
            f"Poll with human_poll('{task_id}') or human_poll_batch(['{task_id}']) for the result."
        ),
    }
