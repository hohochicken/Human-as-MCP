"""
human_decision MCP tool — ask human to make a major directional decision.
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from server.task_pipeline import run_pipeline

logger = logging.getLogger(__name__)

_VALID_DECISION_TYPES = frozenset({
    "architecture", "resource", "risk", "direction", "other",
})


async def human_decision(
    title: str,
    context: str,
    options: list[str],
    recommendation: Optional[str] = None,
    decision_type: str = "other",
    impact: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
    task_manager: Any = None,
    storage: Any = None,
    broadcast: Any = None,
    agent_id: str = "unknown",
) -> dict:
    """Ask a human to make a subjective decision or value judgment.

    Parameters
    ----------
    title : str
        Short title for the decision.
    context : str
        Background, constraints, and why this decision matters.
    options : list[str]
        2–5 concrete options.
    recommendation : str, optional
        AI's recommended choice and rationale.
    decision_type : str
        One of architecture, resource, risk, direction, other.
    impact : str, optional
        Description of the decision's impact.
    priority : str
        One of low, normal, high, critical.
    deadline_minutes : int
        SLA window in minutes.
    """
    if not title or not title.strip():
        return {"status": "error", "message": "title must be a non-empty string."}
    if not context or not context.strip():
        return {"status": "error", "message": "context must be a non-empty string."}
    if not options or len(options) < 2:
        return {"status": "error", "message": "options must contain at least 2 items."}

    # Filter empty/whitespace-only options
    clean_options = [o.strip() for o in options if o and o.strip()]
    if len(clean_options) < 2:
        return {"status": "error", "message": "options must contain at least 2 non-empty items."}
    options = clean_options

    decision_type = decision_type.strip().lower() if decision_type else "other"
    if decision_type not in _VALID_DECISION_TYPES:
        decision_type = "other"

    desc_parts = [f"Context: {context}"]
    desc_parts.append("Options:\n" + "\n".join(f"- {opt}" for opt in options))
    if recommendation:
        desc_parts.append(f"AI Recommendation: {recommendation}")
    if impact:
        desc_parts.append(f"Impact: {impact}")

    return await run_pipeline(
        title=title[:200],
        description="\n\n".join(desc_parts),
        tool_name="human_decision",
        priority=priority,
        deadline_minutes=deadline_minutes,
        agent_id=agent_id,
        task_manager=task_manager,
        storage=storage,
        broadcast=broadcast,
        options=options,
        recommendation=recommendation,
        decision_type=decision_type,
        impact=impact,
    )
