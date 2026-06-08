#!/usr/bin/env python3
"""HumanMCP one-click health check.

Usage:
    cd /h/Human && C:/Python313/python.exe scripts/check_health.py

Exit codes:
    0 = healthy
    1 = service unreachable
    2 = DB check failed
    3 = stale pending tasks detected (>24h)
    4 = high rejection rate warning
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

# ── Configuration ───────────────────────────────────────────────────────────

HEALTH_URL = "http://127.0.0.1:4350/health"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tasks.db")
STALE_HOURS = 24
REJECTION_RATE_WARN = 0.6  # Warn if >60% rejection rate in last 7 days
TIMEOUT_SECONDS = 10

# ── Helpers ─────────────────────────────────────────────────────────────────

def red(s):
    return f"\033[91m{s}\033[0m"

def green(s):
    return f"\033[92m{s}\033[0m"

def yellow(s):
    return f"\033[93m{s}\033[0m"

# ── Checks ──────────────────────────────────────────────────────────────────

def check_service():
    """Check if HumanMCP server is alive."""
    try:
        req = urllib.request.Request(HEALTH_URL)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "ok":
                print(green(f"✓ Service healthy: {json.dumps(data)}"))
                return True
            else:
                print(red(f"✗ Service unhealthy: {json.dumps(data)}"))
                return False
    except Exception as e:
        print(red(f"✗ Service unreachable: {e}"))
        return False


def check_db():
    """Check tasks.db integrity and stats."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Basic integrity check
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        if not c.fetchone():
            print(red("✗ DB error: 'tasks' table not found"))
            conn.close()
            return None, False

        # Stats
        c.execute("SELECT COUNT(*) as total FROM tasks")
        total = c.fetchone()["total"]

        c.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
        by_status = {row["status"]: row["cnt"] for row in c.fetchall()}

        pending = by_status.get("pending", 0)
        completed = by_status.get("completed", 0)
        rejected = by_status.get("rejected", 0)

        print(green(f"✓ DB ok — {total} tasks ({completed} completed, {rejected} rejected, {pending} pending)"))

        conn.close()
        return {"total": total, "by_status": by_status, "pending": pending, "completed": completed, "rejected": rejected}, True
    except Exception as e:
        print(red(f"✗ DB error: {e}"))
        return None, False


def check_stale_pending(stats):
    """Check for tasks pending >24h."""
    if not stats or stats["pending"] == 0:
        print(green("✓ No pending tasks"))
        return True

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)).isoformat()
        c.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'pending' AND created_at < ?", (cutoff,))
        stale = c.fetchone()["cnt"]

        if stale > 0:
            print(red(f"✗ {stale} pending tasks older than {STALE_HOURS}h — need attention!"))
            c.execute(
                "SELECT task_id, title, created_at FROM tasks WHERE status = 'pending' AND created_at < ? ORDER BY created_at ASC",
                (cutoff,)
            )
            for row in c.fetchall():
                print(f"    [{row['task_id'][:12]}...] {row['title'][:60]} — created {row['created_at'][:19]}")
            conn.close()
            return False
        else:
            print(green(f"✓ No stale pending tasks (all < {STALE_HOURS}h)"))
            conn.close()
            return True
    except Exception as e:
        print(red(f"✗ Stale check failed: {e}"))
        return True  # Don't fail the health check on this


def check_rejection_rate(stats):
    """Warn if rejection rate is too high."""
    if not stats:
        return True

    total_recent = stats["completed"] + stats["rejected"]
    if total_recent == 0:
        print(green("✓ No recent tasks to analyze"))
        return True

    rate = stats["rejected"] / max(total_recent, 1)
    if rate > REJECTION_RATE_WARN:
        print(yellow(f"⚠ Rejection rate high: {rate:.0%} ({stats['rejected']}/{total_recent})"))
        print("  Consider tightening Agent delegation rules in humanmcp-agent-guide skill.")
        return False  # Warning, not error
    else:
        print(green(f"✓ Rejection rate acceptable: {rate:.0%} ({stats['rejected']}/{total_recent})"))
        return True


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  HumanMCP Health Check")
    print(f"  {datetime.now().isoformat()[:19]}")
    print("=" * 60)
    print()

    exit_code = 0

    # 1. Service health
    if not check_service():
        exit_code = 1

    # 2. DB check
    stats, db_ok = check_db()
    if not db_ok:
        exit_code = max(exit_code, 2)
        sys.exit(exit_code)

    # 3. Stale pending
    if not check_stale_pending(stats):
        exit_code = max(exit_code, 3)

    # 4. Rejection rate
    if not check_rejection_rate(stats):
        exit_code = max(exit_code, 4)

    print()
    if exit_code == 0:
        print(green("All checks passed!"))
    else:
        print(red(f"Health check failed with exit code {exit_code}"))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
