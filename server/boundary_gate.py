"""Boundary gate — rate limiting for the HumanMCP system.

Checks per-agent and global hourly task-creation limits before allowing a new
task through.  Loads limits from H:\\Human\\config\\server_config.yaml.

Storage contract
----------------
The *storage* argument passed to ``check_rate_limit`` must expose::

    storage.get_recent_tasks(hours: float = 1.0) -> list[dict]

Each dict must have at least ``agent_id`` (str) and ``created_at`` (ISO-8601
str) keys.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from server.constants import DEFAULT_PER_AGENT_PER_HOUR, DEFAULT_GLOBAL_PER_HOUR

logger = logging.getLogger("boundary_gate")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(r"H:\Human\config\server_config.yaml")

_RATE_LIMIT_CACHE: dict[str, int] | None = None


def _load_rate_limits() -> dict[str, int]:
    """Return ``{per_agent_per_hour, global_per_hour}`` from the YAML config.

    Cached values are returned after the first successful load; call
    ``reload_config()`` to force a refresh.
    """
    global _RATE_LIMIT_CACHE
    if _RATE_LIMIT_CACHE is not None:
        return _RATE_LIMIT_CACHE

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg: dict[str, Any] = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning("Config not found at %s, using defaults.", _CONFIG_PATH)
        cfg = {}
    except yaml.YAMLError:
        logger.exception("Failed to parse %s, using defaults.", _CONFIG_PATH)
        cfg = {}

    rate_limits: dict[str, Any] = cfg.get("rate_limits", {}) or {}
    per_agent = int(rate_limits.get("per_agent_per_hour", DEFAULT_PER_AGENT_PER_HOUR))
    global_limit = int(rate_limits.get("global_per_hour", DEFAULT_GLOBAL_PER_HOUR))

    _RATE_LIMIT_CACHE = {
        "per_agent_per_hour": per_agent,
        "global_per_hour": global_limit,
    }
    return _RATE_LIMIT_CACHE


def reload_config() -> None:
    """Force the next call to re-read the YAML config from disk."""
    global _RATE_LIMIT_CACHE
    _RATE_LIMIT_CACHE = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_rate_limit(agent_id: str, storage: Any) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` after checking hourly rate limits.

    Parameters
    ----------
    agent_id:
        The agent whose per-agent quota is being tested.
    storage:
        Object with an async ``get_recent_tasks(hours: float) -> list[dict]``
        method.  Each dict must carry ``agent_id`` and ``created_at`` keys.

    Returns
    -------
    (bool, str)
        ``(True, "")`` when the request is allowed; ``(False, "<reason>")``
        when a limit has been exceeded.
    """
    limits = _load_rate_limits()
    per_agent_limit: int = limits["per_agent_per_hour"]
    global_limit: int = limits["global_per_hour"]

    # --- fetch recent tasks ------------------------------------------------
    try:
        recent = await storage.get_recent_tasks(hours=1.0)
    except AttributeError:
        logger.error(
            "storage %r has no get_recent_tasks() method — cannot enforce rate limits",
            storage,
        )
        # Fail open: let the caller decide whether to proceed.
        return True, ""

    # --- count -------------------------------------------------------------
    agent_count = sum(1 for t in recent if t.get("agent_id") == agent_id)
    global_count = len(recent)

    # --- per-agent check ---------------------------------------------------
    if agent_count >= per_agent_limit:
        reason = (
            f"Per-agent hourly limit reached: agent '{agent_id}' has submitted "
            f"{agent_count} tasks in the last hour (limit: {per_agent_limit})"
        )
        logger.warning(
            "RATE LIMIT — agent '%s' blocked | agent_count=%d/%d | global_count=%d/%d",
            agent_id, agent_count, per_agent_limit,
            global_count, global_limit,
        )
        return False, reason

    # --- global check ------------------------------------------------------
    if global_count >= global_limit:
        reason = (
            f"Global hourly limit reached: {global_count} tasks submitted "
            f"across all agents in the last hour (limit: {global_limit})"
        )
        logger.warning(
            "RATE LIMIT — global cap hit | agent '%s' | agent_count=%d/%d | global_count=%d/%d",
            agent_id, agent_count, per_agent_limit,
            global_count, global_limit,
        )
        return False, reason

    return True, ""


async def get_stats(storage: Any) -> dict[str, int]:
    """Return current rate-limit statistics as a flat dict."""
    limits = _load_rate_limits()
    per_agent_limit: int = limits["per_agent_per_hour"]
    global_limit: int = limits["global_per_hour"]

    try:
        recent = await storage.get_recent_tasks(hours=1.0)
    except AttributeError:
        logger.error("storage %r has no get_recent_tasks() method", storage)
        return {
            "per_agent_per_hour": per_agent_limit,
            "global_per_hour": global_limit,
            "window_minutes": 60,
            "current_agent_count": 0,
            "current_global_count": 0,
            "agent_remaining": -1,
            "global_remaining": global_limit,
        }

    global_count = len(recent)
    global_remaining = max(0, global_limit - global_count)

    return {
        "per_agent_per_hour": per_agent_limit,
        "global_per_hour": global_limit,
        "window_minutes": 60,
        "current_agent_count": 0,
        "current_global_count": global_count,
        "agent_remaining": -1,
        "global_remaining": global_remaining,
    }
