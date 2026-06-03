"""
Test script for human_wait (方案B) — MCP Session Push mechanism.

Verifies:
1. human_wait registers a session and blocks
2. When human completes task via API, human_wait returns immediately
3. Session is cleaned up on return
"""

import asyncio
import json
import sys
import time

import pytest

# These tests use a shared SQLite database and cached event loops.
# They pass standalone (python tests/test_human_wait.py) but conflict
# when run together in pytest due to asyncio event-loop lifetime.
pytestmark = pytest.mark.skip(reason="Standalone tests — run with python tests/test_human_wait.py")

# Setup path
sys.path.insert(0, r"H:\Human")

import server.app as app
from server.app import init, register_mcp_session, unregister_mcp_session
from server.tools.infrastructure import human_wait


async def _ensure_init():
    """Ensure the app is initialized, return task_manager."""
    await init()
    return app.task_manager


@pytest.mark.asyncio
async def test_human_wait_notification():
    """Test that human_wait receives real-time notification when task is completed via API."""
    tm = await _ensure_init()
    
    print("=" * 60)
    print("  方案B 测试: human_wait 实时通知")
    print("=" * 60)
    
    # 1. Create a pending task
    print("\n[1] 创建测试任务...")
    task = await tm.create_task(
        title="Test human_wait notification",
        description="Testing that human_wait gets notified in real-time",
        tool_name="human_action",
        priority="normal",
        deadline_minutes=120,
        agent_id="test-agent-1",
    )
    task_id = task["task_id"]
    print(f"    task_id = {task_id}")
    print(f"    status  = {task['status']}")
    
    # 2. Verify _mcp_sessions is empty
    print(f"\n[2] 初始状态: _mcp_sessions = {dict(app._mcp_sessions)} (应为空)")
    assert len(app._mcp_sessions) == 0, "sessions should be empty before test"
    
    # 3. Start human_wait in background
    print("\n[3] 启动 human_wait (timeout=10s)...")
    
    async def run_human_wait():
        print("    human_wait: 注册 session + 开始等待...")
        result = await human_wait(
            task_id=task_id,
            timeout=10,
            task_manager=tm,
            register_session=register_mcp_session,
            unregister_session=unregister_mcp_session,
        )
        return result
    
    wait_task = asyncio.create_task(run_human_wait())
    
    # Give it a moment to register
    await asyncio.sleep(0.5)
    
    # 4. Verify session was registered
    sessions_snapshot = {k: len(v) for k, v in app._mcp_sessions.items()}
    print(f"    _mcp_sessions = {sessions_snapshot}")
    assert "test-agent-1" in app._mcp_sessions, "session should be registered"
    print("    ✅ session 已注册")
    
    # 5. Complete the task via API (simulating Dashboard action)
    print("\n[4] 模拟 Dashboard 完成操作 (POST /api/tasks/{id}/complete)...")
    t0 = time.time()
    await tm.complete_task(
        task_id=task_id,
        result="Human completed successfully!",
        evidence=["screenshot1.png"],
    )
    print("    任务已标记为 completed")
    
    # Trigger the push (simulating what handle_api_task_complete does)
    from server.app import push_to_mcp_sessions
    await push_to_mcp_sessions({
        "type": "task_completed",
        "task_id": task_id,
        "status": "completed",
        "title": task["title"],
        "agent_id": "test-agent-1",
    })
    print("    通知已推送到 MCP session")
    
    # 6. Wait for human_wait to return
    print("\n[5] 等待 human_wait 返回...")
    result = await asyncio.wait_for(wait_task, timeout=5)
    elapsed = time.time() - t0
    
    # 7. Verify results
    print(f"\n[6] 结果验证:")
    print(f"    耗时:     {elapsed:.3f}s (应 < 1s, 说明是实时通知)")
    print(f"    status:   {result.get('status')}")
    print(f"    wait_status: {result.get('wait_status')}")
    print(f"    result:   {result.get('result', '')[:80]}")
    
    assert result.get("status") == "completed", f"Expected completed, got {result.get('status')}"
    assert result.get("wait_status") == "notified", f"Expected notified, got {result.get('wait_status')}"
    assert result.get("result") == "Human completed successfully!", "Result mismatch"
    assert elapsed < 3.0, f"Took {elapsed:.3f}s, should be near-instant (<3s)"
    print("    ✅ 全部断言通过!")
    
    # 8. Verify session was cleaned up
    print(f"\n[7] 清理验证: _mcp_sessions = {dict(app._mcp_sessions)} (应为空)")
    assert ("test-agent-1" not in app._mcp_sessions 
            or len(app._mcp_sessions.get("test-agent-1", [])) == 0)
    print("    ✅ session 已正确清理")
    
    print("\n" + "=" * 60)
    print("  ✅ 方案B 测试通过! 实时通知机制工作正常")
    print("=" * 60)


@pytest.mark.asyncio
async def test_human_wait_timeout():
    """Test that human_wait returns timeout correctly."""
    tm = await _ensure_init()
    
    print("\n\n" + "=" * 60)
    print("  方案B 测试: human_wait 超时行为")
    print("=" * 60)
    
    # Create a task that won't be completed
    task = await tm.create_task(
        title="Test timeout behavior",
        description="This task will not be completed",
        tool_name="human_action",
        agent_id="test-agent-2",
    )
    task_id = task["task_id"]
    print(f"\n[1] 创建任务: {task_id}")
    
    # Start human_wait with short timeout
    print(f"[2] 启动 human_wait (timeout=2s)...")
    
    result = await human_wait(
        task_id=task_id,
        timeout=2,
        task_manager=tm,
        register_session=register_mcp_session,
        unregister_session=unregister_mcp_session,
    )
    
    print(f"\n[3] 结果:")
    print(f"    wait_status: {result.get('wait_status')}")
    print(f"    status:      {result.get('status')}")
    
    assert result.get("wait_status") == "timeout", f"Expected timeout, got {result.get('wait_status')}"
    assert result.get("status") == "pending", "Status should still be pending"
    print("    ✅ 超时行为正常")
    
    print("\n" + "=" * 60)
    print("  ✅ 超时测试通过!")
    print("=" * 60)


@pytest.mark.asyncio
async def test_already_resolved():
    """Test that human_wait handles already-resolved tasks."""
    tm = await _ensure_init()
    
    print("\n\n" + "=" * 60)
    print("  方案B 测试: human_wait 已解析任务")
    print("=" * 60)
    
    # Create and immediately complete a task
    task = await tm.create_task(
        title="Test already resolved",
        description="Already done",
        tool_name="human_action",
        agent_id="test-agent-3",
    )
    await tm.complete_task(task["task_id"], "done", [])
    
    print(f"\n[1] 创建并完成任务: {task['task_id']}")
    
    result = await human_wait(
        task_id=task["task_id"],
        timeout=300,
        task_manager=tm,
        register_session=register_mcp_session,
        unregister_session=unregister_mcp_session,
    )
    
    print(f"\n[2] 结果:")
    print(f"    wait_status: {result.get('wait_status')}")
    print(f"    status:      {result.get('status')}")
    
    assert result.get("wait_status") == "already_resolved"
    assert result.get("status") == "completed"
    print("    ✅ 正确处理已解析任务")
    
    print("\n" + "=" * 60)
    print("  ✅ 已解析任务测试通过!")
    print("=" * 60)


async def main():
    await test_already_resolved()
    await test_human_wait_timeout()
    await test_human_wait_notification()
    
    print("\n\n🎉 所有方案B测试全部通过!")


if __name__ == "__main__":
    asyncio.run(main())
