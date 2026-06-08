"""Cron-mode task queue analyzer — writes output to stdout."""
import sqlite3
import json
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tasks.db")

def analyze():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    results = {}

    # Total tasks
    c.execute("SELECT COUNT(*) as cnt FROM tasks")
    results["total"] = c.fetchone()["cnt"]

    # By status
    c.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
    status_counts = {}
    for row in c.fetchall():
        status_counts[row["status"]] = row["cnt"]
    results["by_status"] = status_counts

    # Pending breakdown
    pending = status_counts.get("pending", 0)
    results["pending_total"] = pending

    # Stale pending (>24h)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    c.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'pending' AND created_at < ?", (cutoff,))
    results["pending_stale_24h"] = c.fetchone()["cnt"]

    # Recent rejected (last 7 days)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    c.execute(
        "SELECT task_id, title, rejection_reason, params_json, created_at, completed_at "
        "FROM tasks WHERE status = 'rejected' AND completed_at > ? ORDER BY completed_at DESC LIMIT 20",
        (week_ago,)
    )
    rejected = [dict(row) for row in c.fetchall()]
    results["rejected_recent"] = rejected

    # Rejection reasons breakdown
    c.execute(
        "SELECT rejection_reason, COUNT(*) as cnt FROM tasks "
        "WHERE status = 'rejected' AND completed_at > ? GROUP BY rejection_reason",
        (week_ago,)
    )
    rejection_reasons = {}
    for row in c.fetchall():
        reason = row["rejection_reason"] or "(none)"
        rejection_reasons[reason] = row["cnt"]
    results["rejection_reasons"] = rejection_reasons

    # Recent completed (last 7 days)
    c.execute(
        "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'completed' AND completed_at > ?",
        (week_ago,)
    )
    results["completed_7d"] = c.fetchone()["cnt"]

    # Recent pending / in_progress
    c.execute(
        "SELECT task_id, title, status, tool_name, params_json, created_at FROM tasks "
        "WHERE status IN ('pending', 'in_progress') ORDER BY created_at DESC LIMIT 10"
    )
    pending_ing_raw = []
    for row in c.fetchall():
        item = dict(row)
        # Parse action_type from params_json
        try:
            params = json.loads(item["params_json"] or "{}")
            item["action_type"] = params.get("action_type", "N/A")
        except:
            item["action_type"] = "N/A"
        pending_ing_raw.append(item)
    results["pending_ing"] = pending_ing_raw

    # Tool name breakdown
    c.execute("SELECT tool_name, COUNT(*) as cnt FROM tasks GROUP BY tool_name")
    tool_names = {}
    for row in c.fetchall():
        tool_names[row["tool_name"] or "(unknown)"] = row["cnt"]
    results["tool_names"] = tool_names

    conn.close()

    # Pretty print
    print("=" * 60)
    print("HumanMCP Task Queue Report")
    print(f"Generated: {datetime.now().isoformat()}")
    print("=" * 60)
    print(f"\nTotal tasks: {results['total']}")
    print(f"\nBy status:")
    for s, c in sorted(results["by_status"].items()):
        print(f"  {s}: {c}")
    print(f"\nCompleted in last 7 days: {results['completed_7d']}")
    print(f"\nPending (>24h stale): {results['pending_stale_24h']}")
    print(f"\nTool name breakdown:")
    for tn, c in sorted(results["tool_names"].items()):
        print(f"  {tn}: {c}")

    if results["pending_ing"]:
        print(f"\nActive pending/in_progress tasks:")
        for t in results["pending_ing"]:
            print(f"  [{t['status']}] {t['task_id'][:12]}... - {t['title'][:60]} ({t['created_at'][:19]})")

    if results["rejected_recent"]:
        print(f"\nRejected in last 7 days: {len(results['rejected_recent'])}")
        print("Rejection reasons:")
        for r, c in sorted(results["rejection_reasons"].items()):
            print(f"  {r}: {c}")
        print("\nDetailed:")
        for t in results["rejected_recent"]:
            print(f"  [{t['completed_at'][:19]}] {t['task_id'][:12]}... - {t['title'][:60]}")
            print(f"    Reason: {t['rejection_reason']}")
            # Parse params_json for tool type
            if t.get("params_json"):
                try:
                    params = json.loads(t["params_json"])
                    print(f"    Tool: {params.get('tool', 'unknown')}")
                except:
                    pass

    else:
        print("\nNo rejected tasks in the last 7 days.")

    print("\n" + "=" * 60)

    # Output JSON for programmatic use
    print("\n---RAW_JSON---")
    # Convert datetime objects to strings
    for r in results["rejected_recent"]:
        for k in ("created_at", "completed_at"):
            if r.get(k):
                r[k] = str(r[k])
    for p in results["pending_ing"]:
        for k in ("created_at",):
            if p.get(k):
                p[k] = str(p[k])
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    analyze()
