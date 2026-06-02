"""Tests for config loading and merging in app.py."""

import pytest
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(r"H:\Human")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.app import _default_config, _deep_merge, load_config, reload_config


class TestDefaultConfig:
    """Tests for _default_config()."""

    def test_has_required_sections(self):
        cfg = _default_config()
        assert "server" in cfg
        assert "task_defaults" in cfg
        assert "rate_limits" in cfg
        assert "notification" in cfg
        assert "storage" in cfg
        assert "websocket" in cfg

    def test_server_defaults(self):
        cfg = _default_config()
        assert cfg["server"]["host"] == "127.0.0.1"
        assert cfg["server"]["port"] == 4350

    def test_rate_limit_defaults(self):
        cfg = _default_config()
        assert cfg["rate_limits"]["per_agent_per_hour"] == 30
        assert cfg["rate_limits"]["global_per_hour"] == 100

    def test_websocket_defaults(self):
        cfg = _default_config()
        assert cfg["websocket"]["auth_enabled"] is False
        assert cfg["websocket"]["shared_secret"] == ""


class TestDeepMerge:
    """Tests for _deep_merge()."""

    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        _deep_merge(base, override)
        assert base == {"a": 1, "b": 99}

    def test_nested_merge(self):
        base = {"server": {"host": "127.0.0.1", "port": 4350}}
        override = {"server": {"port": 9999}}
        _deep_merge(base, override)
        assert base["server"]["host"] == "127.0.0.1"
        assert base["server"]["port"] == 9999

    def test_new_key_added(self):
        base = {"a": 1}
        override = {"b": 2}
        _deep_merge(base, override)
        assert base == {"a": 1, "b": 2}

    def test_nested_new_key(self):
        base = {"server": {"host": "127.0.0.1"}}
        override = {"server": {"name": "custom"}}
        _deep_merge(base, override)
        assert base["server"]["host"] == "127.0.0.1"
        assert base["server"]["name"] == "custom"

    def test_deeply_nested_merge(self):
        base = {
            "a": {
                "b": {"c": 1, "d": 2},
                "e": 3,
            }
        }
        override = {
            "a": {
                "b": {"d": 99},
            }
        }
        _deep_merge(base, override)
        assert base["a"]["b"]["c"] == 1
        assert base["a"]["b"]["d"] == 99
        assert base["a"]["e"] == 3


class TestConfigCache:
    """Tests for config caching."""

    def test_reload_config_clears_cache(self):
        # Force a load then reload.
        reload_config()
        cfg1 = load_config()
        reload_config()
        cfg2 = load_config()
        assert cfg1 == cfg2  # Should produce the same defaults when no file
