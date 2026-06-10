"""HumanMCP Caller — Claude Code to HumanMCP bridge script.

Usage:
  python humanmcp_call.py --title "任务标题" --steps "步骤1" "步骤2" ...
     [--desc "详细描述"] [--priority normal|high|critical]
     [--timeout 180] [--action command|editor|build|other]

The script connects to HumanMCP via MCP streamable-http, creates a task,
and waits synchronously for the human to respond via Dashboard or API.

Exit codes: 0=completed, 1=rejected, 2=timeout, 3=error
"""
import asyncio, sys, json, argparse, time
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

HUMANMCP_URL = "http://127.0.0.1:4350/mcp"

async def call_human_action(title, steps, desc, priority, timeout):
    async with streamablehttp_client(HUMANMCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Build description
            full_desc = desc or f"请完成以下步骤：\n" + "\n".join(f"- {s}" for s in steps)
            
            print(f"🤖 AI → 👤 人类: {title}", file=sys.stderr)
            print(f"   步骤: {len(steps)} 步", file=sys.stderr)
            print(f"   等待人类响应（最多 {timeout} 秒）...", file=sys.stderr)
            
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    session.call_tool("human_action", {
                        "title": title,
                        "description": full_desc,
                        "steps": steps,
                        "action_type": "command",
                        "priority": priority,
                    }),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                print(json.dumps({"status": "timeout", "message": f"人类 {timeout} 秒内未响应"}))
                return 2
            
            elapsed = time.time() - t0
            
            for c in result.content:
                if hasattr(c, 'text'):
                    data = json.loads(c.text)
                    
                    if data.get("status") == "completed":
                        print(json.dumps({
                            "status": "completed",
                            "result": data.get("result", ""),
                            "evidence": data.get("evidence", []),
                            "elapsed_seconds": round(elapsed, 1),
                        }))
                        return 0
                    
                    elif data.get("status") == "rejected":
                        print(json.dumps({
                            "status": "rejected",
                            "reason": data.get("rejection_reason", ""),
                            "note": data.get("rejection_note", ""),
                            "elapsed_seconds": round(elapsed, 1),
                        }))
                        return 1
                    
                    elif data.get("status") == "pending":
                        # Human didn't respond within 180s sync window
                        print(json.dumps({
                            "status": "pending",
                            "task_id": data.get("task_id", ""),
                            "message": "人类未在180秒内响应，任务已排队。请稍后重试或人工检查Dashboard。",
                        }))
                        return 2
                    
                    else:
                        # Error
                        print(json.dumps(data))
                        return 3
            
            return 3

def main():
    parser = argparse.ArgumentParser(description="Call HumanMCP from Claude Code")
    parser.add_argument("--title", required=True, help="任务标题")
    parser.add_argument("--steps", nargs="+", required=True, help="执行步骤（每步一个字符串）")
    parser.add_argument("--desc", default="", help="详细描述（可选，默认从steps生成）")
    parser.add_argument("--priority", default="normal", choices=["low","normal","high","critical"])
    parser.add_argument("--timeout", type=int, default=200, help="等待超时秒数（默认200）")
    parser.add_argument("--action", default="command", help="任务类型")
    
    args = parser.parse_args()
    exit_code = asyncio.run(call_human_action(
        args.title, args.steps, args.desc, args.priority, args.timeout
    ))
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
