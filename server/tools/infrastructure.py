"""
Infrastructure MCP tools: human_poll, human_cancel, human_list_tasks.

These are immediate-response tools — they don't create tasks or block.
"""

from __future__ import annotations

import logging
from typing import Optional, Any, Union

logger = logging.getLogger(__name__)


async def human_poll(task_id: Union[str, list[str]], task_manager: Any = None) -> dict:
    """Check status of one or more tasks.

    Parameters:
    - task_id: A single task ID string, or a list of task ID strings.
    - task_manager: Injected by the server.

    Returns:
        Single task (str input):
          The task dict on success, or {"status": "error", "message": "..."}
        Batch (list[str] input):
          {"tasks": [...], "summary": {...}}
          On error: {"status": "error", "message": "..."}
    """
    if task_manager is None:
        return {"status": "error", "message": "Server not initialized."}

    # ── Single task ──────────────────────────────────────────────
    if isinstance(task_id, str):
        task = await task_manager.get_task(task_id)
        if task is None:
            return {"status": "error", "message": f"Task not found: {task_id}"}
        return task

    # ── Batch ────────────────────────────────────────────────────
    tasks: list[dict] = []
    summary: dict[str, int] = {
        "pending": 0,
        "completed": 0,
        "rejected": 0,
        "not_found": 0,
    }

    for tid in task_id:
        task = await task_manager.get_task(tid)
        if task is None:
            summary["not_found"] += 1
            tasks.append({"task_id": tid, "status": "not_found"})
        else:
            st = task.get("status", "unknown")
            summary[st] = summary.get(st, 0) + 1
            tasks.append(task)

    return {"tasks": tasks, "summary": summary}


async def human_cancel(
    task_id: str,
    reason: Optional[str] = None,
    action: str = "cancel",
    new_data: Optional[dict] = None,
    task_manager: Any = None,
    broadcast: Any = None,
) -> dict:
    """Cancel or modify a pending task."""
    if task_manager is None:
        return {"status": "error", "message": "Server not initialized."}

    task = await task_manager.get_task(task_id)
    if task is None:
        return {"status": "error", "message": f"Task not found: {task_id}"}
    if task.get("status") != "pending":
        return {
            "status": "error",
            "message": f"Task {task_id} is already '{task.get('status')}'. Cannot cancel.",
        }

    if action == "modify" and new_data:
        try:
            updated = await task_manager.update_task(task_id, new_data)
        except Exception as e:
            return {"status": "error", "message": f"Failed to modify task: {e}"}
        if broadcast:
            try:
                await broadcast({
                    "type": "task_updated",
                    "task_id": task_id,
                    "status": "modified",
                })
            except Exception:
                pass
        return {
            "status": "modified",
            "task_id": task_id,
            "task": updated,
            "message": "Task modified successfully.",
        }

    # Cancel
    try:
        await task_manager.cancel_task(task_id, reason)
    except Exception as e:
        return {"status": "error", "message": f"Failed to cancel task: {e}"}

    if broadcast:
        try:
            await broadcast({
                "type": "task_updated",
                "task_id": task_id,
                "status": "cancelled",
            })
        except Exception:
            pass

    return {
        "status": "cancelled",
        "task_id": task_id,
        "message": "Task cancellation requested. Human must confirm in dashboard.",
    }


async def human_wait(
    task_id: str,
    timeout: int = 300,
    *,
    task_manager: Any = None,
    register_session: Any = None,
    unregister_session: Any = None,
) -> dict:
    """Wait for a human to complete or reject a pending task.

    Registers an MCP notification listener and blocks until:
    - The task is resolved (completed/rejected) → returns result immediately
    - The timeout expires → returns current status with wait_status="timeout"

    Unlike human_poll which returns instantly, this waits for real-time push
    from the server when a human resolves the task via Dashboard or API.

    Parameters:
    - task_id: The task ID to wait for (required)
    - timeout: Max seconds to wait (default 300, clamped to 1..600)

    Returns:
        Task dict with wait_status field:
        - "notified" — human responded, result below
        - "timeout" — no response within timeout, current status below
        - "already_resolved" — task was already done when we checked
    """
    import asyncio

    if task_manager is None:
        return {"status": "error", "message": "Server not initialized."}
    if register_session is None or unregister_session is None:
        return {"status": "error", "message": "Session management not available."}

    # Clamp timeout
    timeout = max(1, min(timeout, 600))

    # Quick check: is the task even pending?
    task = await task_manager.get_task(task_id)
    if task is None:
        return {"status": "error", "message": f"Task not found: {task_id}"}
    status = task.get("status")
    if status != "pending":
        return {**task, "wait_status": "already_resolved",
                "message": f"Task already resolved (status={status})."}

    # Determine agent_id from task
    agent_id = task.get("agent_id", "unknown")

    # Register session queue
    q = register_session(agent_id)

    try:
        while True:
            try:
                notification = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # Timeout — poll current status
                task = await task_manager.get_task(task_id)
                return {
                    **(task or {}),
                    "wait_status": "timeout",
                    "message": (
                        f"No human response within {timeout}s. "
                        f"Current status: {task.get('status', 'unknown') if task else 'not_found'}. "
                        f"If still pending, the human may be away. Consider: "
                        f"(1) call human_poll('{task_id}') to check final status, "
                        f"(2) call human_cancel('{task_id}') if no longer needed."
                    ),
                }

            # Check if this notification is for our task
            if notification.get("task_id") == task_id:
                task = await task_manager.get_task(task_id)
                return {
                    **(task or {}),
                    "wait_status": "notified",
                    "message": "Human responded — result below.",
                }
            # Otherwise keep waiting (notification was for another task)
    finally:
        unregister_session(agent_id, q)


async def human_list_tasks(
    status: str = "pending",
    agent_id: Optional[str] = None,
    limit: int = 50,
    since: Optional[str] = None,
    task_manager: Any = None,
) -> dict:
    """List tasks by status, optionally filtered by agent and time.

    Parameters:
    - status: One of "pending", "completed", "rejected", "all". Default "pending".
    - agent_id: Optional agent ID to filter by. None = all agents.
    - limit: Maximum number of results. Default 50.
    - since: ISO-8601 timestamp. Only return tasks created or completed after this time.
    - task_manager: Injected by the server.

    Returns:
        {"tasks": [...], "total": N, "status_filter": "pending"}
        {"status": "error", "message": "..."}
    """
    if task_manager is None:
        return {"status": "error", "message": "Server not initialized."}

    valid_statuses = {"pending", "completed", "rejected", "all"}
    if status not in valid_statuses:
        return {
            "status": "error",
            "message": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}",
        }

    try:
        tasks = await task_manager.list_tasks(
            status=status, agent_id=agent_id, limit=limit, since=since,
        )
    except Exception as e:
        return {"status": "error", "message": f"Failed to list tasks: {e}"}

    return {
        "tasks": tasks,
        "total": len(tasks),
        "status_filter": status,
    }
