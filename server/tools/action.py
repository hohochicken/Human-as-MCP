"""
human_action MCP tool — unified delegation to a human operator.

This single tool absorbs the old ``human_coordinate`` and ``human_execute_command``:
- Coordination is now ``human_action`` with ``action_type="coordination"``
- Running commands is now ``human_action`` with ``action_type="command"`` or ``"build"``
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from server.task_pipeline import run_pipeline

logger = logging.getLogger(__name__)

_VALID_ACTION_TYPES = frozenset({
    "editor_operation",
    "version_control",
    "build",
    "config",
    "coordination",
    "command",
    "other",
})

_VALID_PRIORITIES = frozenset({"low", "normal", "high", "critical"})


async def human_action(
    title: str,
    description: str,
    steps: Optional[list[str]] = None,
    action_type: str = "other",
    target_person: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
    task_manager: Any = None,
    storage: Any = None,
    broadcast: Any = None,
    agent_id: str = "unknown",
) -> dict:
    """Delegate a physical-world or external-system action to a human operator.

    Covers editor operations, version control, builds, config changes,
    cross-team coordination, privileged commands, and generic actions.

    Parameters
    ----------
    title : str
        Short, descriptive title (max 200 chars).
    description : str
        Detailed description of what needs to be done and the expected outcome.
    steps : list[str], optional
        Step-by-step instructions for completing the task.
    action_type : str
        One of ``editor_operation``, ``version_control``, ``build``, ``config``,
        ``coordination``, ``command``, or ``other``.  Default ``"other"``.
    target_person : str, optional
        Who to coordinate with (only relevant for ``action_type="coordination"``).
    priority : str
        One of ``low``, ``normal``, ``high``, ``critical``.  Default ``"normal"``.
    deadline_minutes : int
        SLA window in minutes (default 120).
    task_manager : TaskManager
        Injected by app.py — the task manager instance.
    storage : Storage
        Injected by app.py — the storage backend.
    broadcast : callable, optional
        Injected by app.py — async WebSocket broadcast function.
    agent_id : str
        Injected by app.py — identifier for the calling AI agent.

    Returns
    -------
    dict
        On success: ``{"task_id": ..., "status": "completed"|"pending"|"rejected", ...}``
        On validation failure: ``{"status": "error", "message": "..."}``
    """
    # ------------------------------------------------------------------
    # Validation — return error dict on failure, never raise ValueError
    # ------------------------------------------------------------------
    if not title or not title.strip():
        return {"status": "error", "message": "title must be a non-empty string."}
    if not description or not description.strip():
        return {"status": "error", "message": "description must be a non-empty string."}
    if not steps or len(steps) == 0:
        return {"status": "error", "message": "steps is required for human_action. Provide a step-by-step list so the human can execute without thinking."}

    # Normalise action_type — unknown values fall back to "other".
    action_type = action_type.strip().lower() if action_type else "other"
    if action_type not in _VALID_ACTION_TYPES:
        action_type = "other"

    # Normalise priority — unknown values fall back to "normal".
    priority = priority.strip().lower() if priority else "normal"
    if priority not in _VALID_PRIORITIES:
        priority = "normal"

    # ------------------------------------------------------------------
    # Build enriched description
    # ------------------------------------------------------------------
    desc_parts = [description]

    # Append steps as a numbered list when provided.
    if steps:
        desc_parts.append(
            "Steps:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
        )

    # For coordination tasks, surface the target person prominently.
    if action_type == "coordination" and target_person:
        desc_parts.insert(0, f"Target Person: {target_person}")

    # For command/build tasks, add a caution prefix.
    if action_type == "command":
        desc_parts.insert(
            0,
            "⚠️ PRIVILEGED COMMAND: This task requires a human to execute a command "
            "the AI cannot run. Review carefully before executing.",
        )

    # ------------------------------------------------------------------
    # Delegate to the shared pipeline
    # ------------------------------------------------------------------
    extra_params: dict[str, Any] = {
        "action_type": action_type,
    }
    if steps:
        extra_params["steps"] = steps
    if target_person:
        extra_params["target_person"] = target_person

    return await run_pipeline(
        title=title,
        description="\n\n".join(desc_parts),
        tool_name="human_action",
        priority=priority,
        deadline_minutes=deadline_minutes,
        agent_id=agent_id,
        task_manager=task_manager,
        storage=storage,
        broadcast=broadcast,
        **extra_params,
    )
