"""Tests for boundary_gate.py — rate limiting logic."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone
from server import boundary_gate


def _make_recent(agent_counts: dict[str, int], hours_ago: float = 0.1):
    """Build a list of recent-task dicts for testing.

    Each entry has ``agent_id`` and ``created_at``.
    """
    now = datetime.now(timezone.utc)
    tasks = []
    for agent_id, count in agent_counts.items():
        for i in range(count):
            tasks.append({
                "agent_id": agent_id,
                "created_at": (now - timedelta(hours=hours_ago)).isoformat(),
            })
    return tasks


class TestRateLimits:
    """Tests for check_rate_limit."""

    @pytest.mark.asyncio
    async def test_allowed_when_under_limit(self, monkeypatch):
        """Should return (True, "") when under both limits."""
        monkeypatch.setattr(
            boundary_gate, "_RATE_LIMIT_CACHE",
            {"per_agent_per_hour": 30, "global_per_hour": 100},
        )

        mock_storage = MagicMock()
        mock_storage.get_recent_tasks = AsyncMock(return_value=_make_recent(
            {"agent-1": 5, "agent-2": 3}
        ))

        allowed, reason = await boundary_gate.check_rate_limit("agent-1", mock_storage)
        assert allowed is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_per_agent_limit_blocked(self, monkeypatch):
        """Should return False when per-agent limit is exceeded."""
        monkeypatch.setattr(
            boundary_gate, "_RATE_LIMIT_CACHE",
            {"per_agent_per_hour": 10, "global_per_hour": 100},
        )

        mock_storage = MagicMock()
        mock_storage.get_recent_tasks = AsyncMock(return_value=_make_recent(
            {"agent-1": 10, "agent-2": 1}
        ))

        allowed, reason = await boundary_gate.check_rate_limit("agent-1", mock_storage)
        assert allowed is False
        assert "agent 'agent-1'" in reason
        assert "10" in reason

    @pytest.mark.asyncio
    async def test_global_limit_blocked(self, monkeypatch):
        """Should return False when global limit is exceeded."""
        monkeypatch.setattr(
            boundary_gate, "_RATE_LIMIT_CACHE",
            {"per_agent_per_hour": 30, "global_per_hour": 3},
        )

        mock_storage = MagicMock()
        mock_storage.get_recent_tasks = AsyncMock(return_value=_make_recent(
            {"agent-1": 1, "agent-2": 1, "agent-3": 1}
        ))

        allowed, reason = await boundary_gate.check_rate_limit("agent-4", mock_storage)
        assert allowed is False
        assert "Global hourly limit" in reason

    @pytest.mark.asyncio
    async def test_fail_open_when_no_method(self, monkeypatch):
        """Should return (True, "") when storage lacks get_recent_tasks."""
        monkeypatch.setattr(
            boundary_gate, "_RATE_LIMIT_CACHE",
            {"per_agent_per_hour": 30, "global_per_hour": 100},
        )

        # MagicMock without get_recent_tasks will raise AttributeError,
        # which check_rate_limit catches and fails-open.
        mock_storage = MagicMock(spec=[])  # No methods at all.
        allowed, reason = await boundary_gate.check_rate_limit("agent-1", mock_storage)
        assert allowed is True


class TestConfig:
    """Tests for config loading."""

    def test_defaults_when_no_cache(self, monkeypatch):
        monkeypatch.setattr(boundary_gate, "_RATE_LIMIT_CACHE", None)
        monkeypatch.setattr(boundary_gate, "_CONFIG_PATH", boundary_gate.Path("nonexistent.yaml"))
        limits = boundary_gate._load_rate_limits()
        assert limits["per_agent_per_hour"] == 30
        assert limits["global_per_hour"] == 100

    def test_reload_config(self, monkeypatch):
        monkeypatch.setattr(
            boundary_gate, "_RATE_LIMIT_CACHE",
            {"per_agent_per_hour": 99, "global_per_hour": 99},
        )
        boundary_gate.reload_config()
        assert boundary_gate._RATE_LIMIT_CACHE is None
