"""
Shared constants for the Human-as-MCP server.

Centralises magic numbers that were previously repeated across
app.py and the tools/ modules.
"""

# ---------------------------------------------------------------------------
# Task field length limits
# ---------------------------------------------------------------------------
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 10000

# ---------------------------------------------------------------------------
# Deadline clamping (minutes)
# ---------------------------------------------------------------------------
MIN_DEADLINE_MINUTES = 1
MAX_DEADLINE_MINUTES = 10080  # 7 days

# ---------------------------------------------------------------------------
# Synchronous blocking behaviour (seconds)
# ---------------------------------------------------------------------------
BLOCK_TIMEOUT = 180   # How long _create_and_notify waits for a human response
POLL_INTERVAL = 3     # How often we poll the DB during the blocking window

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
DEFAULT_PER_AGENT_PER_HOUR = 30
DEFAULT_GLOBAL_PER_HOUR = 100

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_PRIORITY = "normal"
DEFAULT_DEADLINE_MINUTES = 120

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4350
DEFAULT_SERVER_NAME = "Human-as-MCP"

# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
DEFAULT_TOAST_ENABLED = True
DEFAULT_TOAST_DURATION_SECONDS = 10

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = "data/tasks.db"
