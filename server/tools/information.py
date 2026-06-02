"""
human_information MCP tool.

Allows AI agents to ask human operators questions — whether the answer lives in
the human's memory, in a restricted system the AI cannot access, in a document,
a database, or via a colleague.  The key insight: "look something up in a
system" and "tell me what you know" are the same workflow — the human receives a
question and returns an answer.

This tool absorbs the old ``human_access`` tool.  Use ``source="system"`` with
the ``system`` parameter to replicate the old access behaviour.

Every question creates a tracked task and triggers a desktop toast notification.
Uses the shared ``task_pipeline`` for rate-limiting, 180-second synchronous
blocking, and WebSocket broadcast.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional, Any

from server.task_pipeline import run_pipeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain enum
# ---------------------------------------------------------------------------


class InformationDomain(str, Enum):
    """Category of information being requested from the human operator."""

    ARCHITECTURE = "architecture"
    DESIGN = "design"
    HISTORY = "history"
    CONVENTION = "convention"
    WORKFLOW = "workflow"
    CONTACT = "contact"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Valid sets for normalisation
# ---------------------------------------------------------------------------

_VALID_DOMAINS = frozenset(d.value for d in InformationDomain)

_VALID_SOURCES = frozenset({
    "memory",
    "system",
    "document",
    "database",
    "colleague",
    "other",
})

_VALID_SYSTEMS = frozenset({
    "internal_platform",
    "document_system",
    "admin_tool",
    "database",
    "dashboard",
    "other",
})

_VALID_PRIORITIES = frozenset({"low", "normal", "high", "critical"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def human_information(
    question: str,
    context: Optional[str] = None,
    domain: Optional[str] = None,
    source: str = "memory",
    system: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
    task_manager: Any = None,
    storage: Any = None,
    broadcast: Any = None,
    agent_id: str = "unknown",
) -> dict:
    """Ask a human operator a question and track it as a task.

    Covers both "what do you know?" (source="memory") and "look this up in a
    system I cannot access" (source="system" / "database" / "document" etc.).

    Parameters
    ----------
    question : str
        The question to ask.  Be specific and self-contained.
    context : str, optional
        Why the AI needs this answer and how it will be used.
    domain : str, optional
        Category: architecture, design, history, convention, workflow,
        contact, other.  Invalid values are silently mapped to ``"other"``.
    source : str
        Where the human should look for the answer:
        - ``"memory"``     — the human's own knowledge / experience (default)
        - ``"system"``     — a restricted system; use ``system`` to name it
        - ``"document"``   — a spec, design doc, or other document
        - ``"database"``   — a database the AI cannot query
        - ``"colleague"``  — another person the human should ask
        - ``"other"``      — any other information source
    system : str, optional
        When ``source="system"``, which system to query:
        ``internal_platform``, ``document_system``, ``admin_tool``,
        ``database``, ``dashboard``, or ``other``.
        Ignored when *source* is not ``"system"``.
    priority : str, optional
        Task importance: low, normal, high, critical (default ``"normal"``).
    deadline_minutes : int, optional
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
    # Validation — return error dict on failure
    # ------------------------------------------------------------------
    if not question or not question.strip():
        return {"status": "error", "message": "question must be a non-empty string."}

    # ------------------------------------------------------------------
    # Normalise inputs
    # ------------------------------------------------------------------
    question = question.strip()

    if context is not None:
        context = context.strip() or None

    domain = _normalise_domain(domain)
    source = _normalise_source(source)
    system = _normalise_system(system, source)
    priority = _normalise_priority(priority)

    # ------------------------------------------------------------------
    # Build enriched description
    # ------------------------------------------------------------------
    description_parts = [f"Question: {question}"]

    if context:
        description_parts.append(f"Context: {context}")

    if domain:
        description_parts.append(f"Domain: {domain}")

    # Source / system details — surface when the human needs to look beyond
    # their own memory.  A system lookup prompt looks different from a
    # "what do you know?" prompt.
    if source != "memory":
        source_label = _source_label(source, system)
        description_parts.append(f"Source: {source_label}")
        if source == "system" and system:
            description_parts.insert(
                1,
                f"⚠️ SYSTEM ACCESS REQUIRED: The human must query '{system}' "
                f"to answer this question. The AI cannot access this system.",
            )

    title = _build_title(question)

    logger.info(
        "Creating human_information task: title=%r domain=%s source=%s system=%s priority=%s deadline=%dm",
        title, domain, source, system, priority, deadline_minutes,
    )

    # ------------------------------------------------------------------
    # Delegate to the shared pipeline
    # ------------------------------------------------------------------
    extra_params: dict[str, Any] = {
        "source": source,
    }
    if domain:
        extra_params["domain"] = domain
    if system:
        extra_params["system"] = system

    return await run_pipeline(
        title=title,
        description="\n\n".join(description_parts),
        tool_name="human_information",
        priority=priority,
        deadline_minutes=deadline_minutes,
        agent_id=agent_id,
        task_manager=task_manager,
        storage=storage,
        broadcast=broadcast,
        **extra_params,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_title(question: str, max_length: int = 200) -> str:
    """Derive a concise task title from the question text."""
    title = question.strip()
    if len(title) > max_length:
        title = title[: max_length - 1] + "…"
    return title


def _normalise_domain(raw: Optional[str]) -> Optional[str]:
    """Validate and normalise the *domain* parameter.

    Returns ``None`` for empty/None input.  Unknown values silently fall back
    to ``"other"``.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value not in _VALID_DOMAINS:
        logger.debug("Unknown domain %r — falling back to 'other'", raw)
        return InformationDomain.OTHER.value
    return value


def _normalise_source(raw: Optional[str]) -> str:
    """Validate and normalise the *source* parameter.

    Returns ``"memory"`` for empty/None input.  Unknown values silently fall
    back to ``"other"``.
    """
    if raw is None:
        return "memory"
    value = raw.strip().lower()
    if not value:
        return "memory"
    if value not in _VALID_SOURCES:
        logger.debug("Unknown source %r — falling back to 'other'", raw)
        return "other"
    return value


def _normalise_system(raw: Optional[str], source: str) -> Optional[str]:
    """Validate and normalise the *system* parameter.

    Only meaningful when *source* is ``"system"`` — returns ``None`` for all
    other source types.  Unknown values silently fall back to ``"other"``.
    """
    if source != "system":
        return None
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value not in _VALID_SYSTEMS:
        logger.debug("Unknown system %r — falling back to 'other'", raw)
        return "other"
    return value


def _normalise_priority(raw: Optional[str]) -> str:
    """Validate and normalise the *priority* parameter.

    Returns ``"normal"`` for empty/None input.  Unknown values silently fall
    back to ``"normal"``.
    """
    if raw is None:
        return "normal"
    value = raw.strip().lower()
    if not value:
        return "normal"
    if value not in _VALID_PRIORITIES:
        logger.debug("Unknown priority %r — falling back to 'normal'", raw)
        return "normal"
    return value


def _source_label(source: str, system: Optional[str]) -> str:
    """Build a human-readable label for the information source."""
    labels: dict[str, str] = {
        "memory": "Human operator's knowledge / experience",
        "system": f"Restricted system: {system}" if system else "Restricted system (unspecified)",
        "document": "Document / specification",
        "database": "Database (AI cannot query directly)",
        "colleague": "Another person / colleague",
        "other": "Other information source",
    }
    return labels.get(source, f"Unknown source: {source}")
