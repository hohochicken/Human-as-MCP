"""HumanMCP Caller — 持续等待直到人类回复或全局超时。

Usage:
  python humanmcp_call.py --title "标题" --steps "步骤1" "步骤2" ...
     [--priority normal] [--total-timeout 600]

工作流程:
  1. 调 human_action → 180秒同步等待
  2. 若超时 → 调 human_wait(600s) MCP实时推送
  3. 若再超时 → human_poll 轮询(每30秒)直到全局超时
  4. 人类任何时候回复 → 立即返回结果

Exit codes: 0=completed, 1=rejected, 2=timeout, 3=error
"""
import asyncio, sys, json, argparse, time
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

HUMANMCP_URL = "http://127.0.0.1:4350/mcp"
POLL_INTERVAL = 30  # 轮询间隔


async def call_and_wait(title, steps, desc, priority, total_timeout):
    async with streamablehttp_client(HUMANMCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            full_desc = desc or "请完成以下步骤：\n" + "\n".join(f"- {s}" for s in steps)

            print(f"🤖 → 👤 {title}", file=sys.stderr)
            print(f"   步骤: {len(steps)} 步, 最长等待: {total_timeout}s", file=sys.stderr)

            t_start = time.time()

            # ── 阶段1: human_action (180s同步等待) ──
            result = await session.call_tool("human_action", {
                "title": title, "description": full_desc,
                "steps": steps, "action_type": "command", "priority": priority,
            })

            data = _parse_result(result)
            if not data:
                return _error("human_action 返回异常")

            # 人类在180秒内响应了
            if data.get("status") in ("completed", "rejected"):
                return _done(data, time.time() - t_start)

            # sync=false — 人类未在窗口内响应，进入阶段2
            task_id = data.get("task_id", "")
            if not task_id:
                return _error("未获取到 task_id")

            elapsed = time.time() - t_start
            remaining = max(0, total_timeout - elapsed)
            print(f"   180s内未响应, 进入MCP推送等待(最多{remaining:.0f}s)...", file=sys.stderr)

            # ── 阶段2: human_wait MCP实时推送 ──
            wait_timeout = min(remaining, 600)
            result2 = await session.call_tool("human_wait", {
                "task_id": task_id, "timeout": int(wait_timeout),
            })
            data2 = _parse_result(result2)

            if data2 and data2.get("wait_status") == "notified":
                _status = data2.get("status", "")
                if _status == "completed":
                    return _done(data2, time.time() - t_start)
                elif _status == "rejected":
                    return _done(data2, time.time() - t_start)

            # ── 阶段3: human_poll 轮询 ──
            elapsed = time.time() - t_start
            remaining = max(0, total_timeout - elapsed)
            print(f"   推送未响应, 进入轮询(每{POLL_INTERVAL}s, 剩余{remaining:.0f}s)...", file=sys.stderr)

            while time.time() - t_start < total_timeout:
                await asyncio.sleep(POLL_INTERVAL)
                r = await session.call_tool("human_poll", {"task_id": task_id})
                d = _parse_result(r)
                if d and d.get("status") in ("completed", "rejected"):
                    return _done(d, time.time() - t_start)

            # ── 全局超时 ──
            total_elapsed = time.time() - t_start
            print(json.dumps({
                "status": "timeout",
                "task_id": task_id,
                "message": f"等待 {total_elapsed:.0f}s 后超时, 任务仍在排队",
                "elapsed_seconds": round(total_elapsed, 1),
            }))
            return 2


def _parse_result(result):
    for c in result.content:
        if hasattr(c, "text"):
            try:
                return json.loads(c.text)
            except json.JSONDecodeError:
                return None
    return None


def _done(data, elapsed):
    status = data.get("status", "?")
    out = {
        "status": status,
        "elapsed_seconds": round(elapsed, 1),
    }
    if status == "completed":
        out["result"] = data.get("result", "")
        out["evidence"] = data.get("evidence", [])
    elif status == "rejected":
        out["reason"] = data.get("rejection_reason", "")
        out["note"] = data.get("rejection_note", "")

    print(json.dumps(out))
    return 0 if status == "completed" else 1


def _error(msg):
    print(json.dumps({"status": "error", "message": msg}))
    return 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", help="任务标题（新建任务时必填）")
    parser.add_argument("--steps", nargs="*", help="执行步骤（新建任务时必填）")
    parser.add_argument("--desc", default="")
    parser.add_argument("--priority", default="normal",
                        choices=["low", "normal", "high", "critical"])
    parser.add_argument("--total-timeout", type=int, default=1800,
                        help="最大总等待秒数（默认1800=30分钟）")
    parser.add_argument("--task-id", default="",
                        help="查询已有任务（不创建新任务，仅等待结果）")

    args = parser.parse_args()

    if args.task_id:
        # Query mode: just poll an existing task
        sys.exit(asyncio.run(poll_existing(
            args.task_id, args.total_timeout
        )))
    else:
        if not args.title or not args.steps:
            print(json.dumps({"status": "error", "message": "--title and --steps required for new task"}))
            sys.exit(3)
        sys.exit(asyncio.run(call_and_wait(
            args.title, args.steps, args.desc, args.priority, args.total_timeout
        )))


async def poll_existing(task_id, total_timeout):
    """查询已有任务，等待人类回复"""
    async with streamablehttp_client(HUMANMCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            t_start = time.time()
            print(f"🔍 查询任务 {task_id[:20]}...", file=sys.stderr)

            # Try human_wait first
            remaining = min(total_timeout, 600)
            result = await session.call_tool("human_wait", {
                "task_id": task_id, "timeout": int(remaining),
            })
            data = _parse_result(result)

            if data and data.get("wait_status") == "notified":
                return _done(data, time.time() - t_start)

            # Fall back to polling
            while time.time() - t_start < total_timeout:
                await asyncio.sleep(POLL_INTERVAL)
                r = await session.call_tool("human_poll", {"task_id": task_id})
                d = _parse_result(r)
                if d and d.get("status") in ("completed", "rejected"):
                    return _done(d, time.time() - t_start)

            elapsed = time.time() - t_start
            print(json.dumps({
                "status": "timeout",
                "task_id": task_id,
                "message": f"等待 {elapsed:.0f}s 后超时。人类仍未回复。",
                "retry_hint": f"python H:/Human/scripts/humanmcp_call.py --task-id {task_id} --total-timeout 3600",
                "elapsed_seconds": round(elapsed, 1),
            }))
            return 2


if __name__ == "__main__":
    main()
