"""
HumanMCP Server — Main Application.

Bridges human operators into the MCP (Model Context Protocol) ecosystem
as callable tools.  Provides:

- 3 domain tools (human_action, human_decision, human_information)
- 3 infrastructure tools (human_poll, human_cancel, human_list_tasks)
- HTTP dashboard at /dashboard
- REST API at /api/*
- WebSocket at /ws for real-time task updates
- Health check at /health
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(r"H:\Human")
CONFIG_PATH = PROJECT_ROOT / "config" / "server_config.yaml"
STATIC_DIR = PROJECT_ROOT / "static"
DASHBOARD_FILE = STATIC_DIR / "dashboard.html"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("human_mcp")

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_config_cache: Optional[dict[str, Any]] = None


def load_config() -> dict[str, Any]:
    """Load server configuration from YAML.  Results are cached after first load."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if not CONFIG_PATH.exists():
        logger.warning("Config file not found at %s, using defaults.", CONFIG_PATH)
        _config_cache = _default_config()
        return _config_cache

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg: dict[str, Any] = yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        logger.exception("Failed to parse %s, using defaults.", CONFIG_PATH)
        cfg = {}

    merged = _default_config()
    _deep_merge(merged, cfg)
    _config_cache = merged
    return _config_cache


def _default_config() -> dict[str, Any]:
    return {
        "server": {"host": "127.0.0.1", "port": 4350, "name": "HumanMCP"},
        "task_defaults": {
            "default_priority": "normal",
            "default_deadline_minutes": 120,
            "max_title_length": 200,
            "max_description_length": 10000,
        },
        "rate_limits": {"per_agent_per_hour": 30, "global_per_hour": 100},
        "notification": {"toast_enabled": True, "toast_duration_seconds": 10},
        "storage": {"db_path": "data/tasks.db"},
        "websocket": {
            "auth_enabled": False,
            "shared_secret": "",
        },
    }


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge *override* into *base* in-place."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def reload_config() -> None:
    """Force the next ``load_config()`` call to re-read the YAML file."""
    global _config_cache
    _config_cache = None


# ---------------------------------------------------------------------------
# Imports (after config path is established)
# ---------------------------------------------------------------------------

from server.task_manager import TaskManager
from server.storage import Storage
from server.tools import (
    human_action as _human_action_impl,
    human_decision as _human_decision_impl,
    human_information as _human_information_impl,
    human_poll as _human_poll_impl,
    human_cancel as _human_cancel_impl,
    human_list_tasks as _human_list_tasks_impl,
    human_wait as _human_wait_impl,
)

# ---------------------------------------------------------------------------
# FastMCP application
# ---------------------------------------------------------------------------

mcp = FastMCP("HumanMCP")

# ---------------------------------------------------------------------------
# Global state (populated by ``init()`` at startup)
# ---------------------------------------------------------------------------

_config: dict[str, Any] = {}
storage: Optional[Storage] = None
task_manager: Optional[TaskManager] = None
ws_clients: set = set()  # Connected WebSocket clients

# Per-agent "last seen" timestamps for piggyback injection.
# When any tool is called by agent X, we check for tasks completed since
# X's last_seen and surface them in the response.
_agent_last_seen: dict[str, str] = {}

# Active MCP client SSE sessions for push notifications.
# Dict keyed by agent_id → list of asyncio.Queue for that agent's sessions.
# An agent may have multiple sessions (e.g. reconnects, parallel tabs).
_mcp_sessions: dict[str, list[asyncio.Queue]] = {}

# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


async def broadcast(message: dict) -> None:
    """Send a JSON message to every connected WebSocket client.

    Disconnected clients are silently purged from the set.
    """
    if not ws_clients:
        return
    payload = json.dumps(message, ensure_ascii=False)
    stale: list = []
    for ws in list(ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            stale.append(ws)
    for ws in stale:
        ws_clients.discard(ws)
        logger.debug("Removed stale WebSocket client.")


# ---------------------------------------------------------------------------
# Piggyback injection — surfaces recently-resolved tasks in every tool response
# ---------------------------------------------------------------------------


async def _inject_piggyback(result: dict, agent_id: str) -> dict:
    """Check for tasks resolved since this agent's last activity.

    Appends ``pending_results`` and ``recently_completed_task_ids`` to
    *result* so the AI knows there are new results to fetch **without**
    needing an explicit poll.

    Also updates the agent's ``last_seen`` timestamp.
    """
    if task_manager is None:
        return result

    now = datetime.now(timezone.utc).isoformat()
    since = _agent_last_seen.get(agent_id)

    # Update last-seen *before* the query so we don't miss anything that
    # arrives between the query and the next call.
    _agent_last_seen[agent_id] = now

    if not since:
        return result

    try:
        recent = await task_manager.get_recently_completed(
            since=since, agent_id=agent_id,
        )
    except Exception:
        logger.debug("Piggyback query failed", exc_info=True)
        return result

    if recent:
        result["pending_results"] = True
        result["recently_completed_task_ids"] = [
            {
                "task_id": t["task_id"],
                "title": t["title"],
                "status": t["status"],
                "completed_at": t.get("completed_at"),
            }
            for t in recent
        ]
        logger.debug(
            "Piggyback: agent=%s has %d resolved task(s) since %s",
            agent_id, len(recent), since,
        )
    else:
        result["pending_results"] = False

    return result


# ---------------------------------------------------------------------------
# MCP session push — pushes task-status notifications to active MCP clients
# ---------------------------------------------------------------------------


def register_mcp_session(agent_id: str) -> asyncio.Queue:
    """Create and register a new MCP client session queue for *agent_id*.

    Returns an async queue.  Call ``unregister_mcp_session(agent_id, q)``
    when the session ends.
    """
    q: asyncio.Queue = asyncio.Queue()
    _mcp_sessions.setdefault(agent_id, []).append(q)
    total = sum(len(v) for v in _mcp_sessions.values())
    logger.debug(
        "MCP session registered: agent=%s (%d total sessions across %d agents).",
        agent_id, total, len(_mcp_sessions),
    )
    return q


def unregister_mcp_session(agent_id: str, q: asyncio.Queue) -> None:
    """Remove a previously registered MCP session queue."""
    queues = _mcp_sessions.get(agent_id, [])
    if q in queues:
        queues.remove(q)
    if not queues:
        _mcp_sessions.pop(agent_id, None)
    total = sum(len(v) for v in _mcp_sessions.values())
    logger.debug(
        "MCP session unregistered: agent=%s (%d total sessions remaining).",
        agent_id, total,
    )


async def push_to_mcp_sessions(notification: dict) -> None:
    """Push a notification to the MCP sessions of the agent who owns the task.

    Notification must include ``agent_id`` — only sessions registered under
    that agent receive the push.  Other agents' sessions are never notified
    about tasks they don't own.
    """
    agent_id = notification.get("agent_id", "")
    if not agent_id or agent_id not in _mcp_sessions:
        return

    queues = _mcp_sessions[agent_id]
    stale: list[asyncio.Queue] = []
    for q in queues:
        try:
            q.put_nowait(notification)
        except asyncio.QueueFull:
            stale.append(q)
        except Exception:
            stale.append(q)

    for q in stale:
        unregister_mcp_session(agent_id, q)

    if notification.get("type") == "task_completed":
        logger.info(
            "Pushed task_completed to agent=%s (%d session(s)): task_id=%s",
            agent_id, len(queues) - len(stale),
            notification.get("task_id"),
        )


# ---------------------------------------------------------------------------
# Agent identity helper
# ---------------------------------------------------------------------------


def _get_agent_id() -> str:
    """Return an identifier for the calling AI agent.

    Attempts to extract the agent identity from:
    1. The ``X-Agent-ID`` HTTP header (set by MCP clients).
    2. FastMCP session metadata (if available).

    Falls back to ``"unknown"`` when no identity is available.
    """
    try:
        request = get_http_request()
        if request is not None:
            agent_id = request.headers.get("X-Agent-ID", "").strip()
            if agent_id:
                return agent_id
    except Exception:
        pass

    return "unknown"


# ---------------------------------------------------------------------------
# MCP Tools — thin wrappers that delegate to tools/ implementations
# ---------------------------------------------------------------------------


def _tool_context() -> dict:
    """Build the dependency dict injected into every tool implementation."""
    return {
        "task_manager": task_manager,
        "storage": storage,
        "broadcast": broadcast,
        "agent_id": _get_agent_id(),
    }


async def _with_piggyback(coro) -> dict:
    """Await *coro* and inject piggyback data into the returned dict.

    Every ``@mcp.tool()`` wrapper routes through this so AI agents always
    receive ``pending_results`` and ``recently_completed_task_ids`` (when
    applicable) without needing an explicit poll.
    """
    result = await coro
    if isinstance(result, dict):
        return await _inject_piggyback(result, _get_agent_id())
    return result


@mcp.tool()
async def human_action(
    title: str,
    description: str,
    steps: list[str],
    action_type: str = "other",
    target_person: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
) -> dict:
    """Delegate a physical-world action, privileged command, or people-coordination to a human.

    **⚠️ 核心规则 — 使用前必须确认**
    这个操作 AI 自己真的做不到吗？如果能通过本地命令、文件读写、
    API 调用、搜索完成——不要用这个工具。人类是你的工具箱中最昂贵
    的工具，只在真正需要时使用。

    **触发条件（任一满足即调用）**
    - 操作物理设备 / GUI-only 软件 / 移动端
    - 命令需要管理员权限，AI 当前沙箱无法执行
    - 需要协调其他人（设为 action_type="coordination"）
    - 需要操作 AI 不可达的机器或环境

    **不触发的情况（AI 应自己完成）**
    - 本地文件读写、代码编辑、可用的 Bash 命令
    - 纯信息检索或推理任务
    - 可通过已有 MCP 工具完成的操作
    - 单纯的信息传递（用 human_information）

    **拒绝后的行为（4 种理由）**
    - ai_can_do → 反思误判，自己完成，不要重试
    - unclear   → 补充上下文后重新构建任务（新 task_id，不重试原 task）
    - out_of_scope → 考虑 human_information 找正确的人，或标记阻塞
    - invalid_task → 修正错误前提后重新构建，绝不重试

    **参数**
    title: 简短标题（必填，最长200字符）
    description: 完整描述，包含目标和期望结果（必填）
    steps: 逐步操作说明（必填！人类按步骤执行，不需要思考。每步一行，越细越好）
    action_type: operation|command|coordination|build|config|other
    target_person: 协调目标（仅 action_type="coordination" 时相关）
    priority: low|normal|high|critical（默认 normal）
    deadline_minutes: SLA 时限/分钟（默认 120）

    **返回**
    同步响应(sync=true): {"task_id":"...", "status":"completed"|"rejected", "result":"...", ...}
    超时回退(sync=false): {"task_id":"...", "status":"pending", "message":"Poll with human_poll(...)"}
    """
    return await _with_piggyback(_human_action_impl(
        title=title,
        description=description,
        steps=steps,
        action_type=action_type,
        target_person=target_person,
        priority=priority,
        deadline_minutes=deadline_minutes,
        **_tool_context(),
    ))


@mcp.tool()
async def human_decision(
    title: str,
    context: str,
    options: list[str],
    recommendation: Optional[str] = None,
    decision_type: str = "other",
    impact: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
) -> dict:
    """Ask a human to make a subjective decision or value judgment.

    **⚠️ 触发阈值**
    只在以下情况使用：提交代码、选择架构方案、涉及法律/合规/伦理、
    需要大规模修改工程内容。其他日常技术选择 AI 应基于架构知识和
    数据自行决定——不要拿技术选型来问人。

    **触发条件（任一满足）**
    - 多个技术上等价的方案，需基于审美/品牌/用户体感选择
    - 涉及法律、合规、伦理的风险判断
    - 需要产品方向/业务优先级的决策（超出 AI 授权范围）
    - 成本-收益权衡涉及非量化因素（用户口碑、品牌形象）

    **不触发的情况**
    - 可通过数据、指标、规则明确判断的决策
    - 纯技术选型（AI 应基于架构知识给出建议并自行决定）
    - 已有明确决策标准或策略文档覆盖的情况

    **参数**
    title: 决定标题（必填）
    context: 完整背景、约束条件、AI 已做的分析（必填）
    options: 2-5 个可选方案，每个附简短优缺点（必填）
    recommendation: AI 推荐哪个选项 + 理由（帮助人类快速决策）
    decision_type: architecture|resource|risk|direction|other
    impact: 决定的影响范围描述
    priority: low|normal|high|critical（默认 normal）
    deadline_minutes: SLA 时限/分钟（默认 120）

    **返回**: 同 human_action 的返回结构
    """
    return await _with_piggyback(_human_decision_impl(
        title=title,
        context=context,
        options=options,
        recommendation=recommendation,
        decision_type=decision_type,
        impact=impact,
        priority=priority,
        deadline_minutes=deadline_minutes,
        **_tool_context(),
    ))


@mcp.tool()
async def human_information(
    question: str,
    context: Optional[str] = None,
    domain: Optional[str] = None,
    source: str = "memory",
    system: Optional[str] = None,
    priority: str = "normal",
    deadline_minutes: int = 120,
) -> dict:
    """Ask a human for information you cannot obtain through your available tools.

    **⚠️ 使用前确认**
    这个信息能通过搜索代码库、读取文件、查询 API、网络搜索获取吗？
    能通过推理从已有数据中推导吗？如果能——不要问人，自己去查。

    **触发条件（任一满足）**
    - 信息不在代码库/文档/网络/任何可访问工具中（隐性知识、口口相传的约定）
    - 需要从人类记忆/经验中提取（如"当初为什么这样设计"）
    - 需要查询封闭系统：内部平台、数据库、管理后台（设 source="system"）
    - 需要查阅物理文档、纸质记录、离线数据
    - 需要外部人员确认（设 source="colleague"）

    **不触发的情况**
    - 可通过搜索、读取文件、API 获取的信息 → 自己去查
    - 可通过推理从现有数据推导的信息 → 自己推导
    - 代码逻辑或文档中已明确记录的信息 → 自己读

    **参数**
    question: 精确的问题，自包含（人可能看不到上下文）（必填）
    context: 为什么需要、会怎么用（帮助人给出更有用的回答）
    domain: architecture|design|history|convention|workflow|contact|other
    source: memory(默认)|system|document|database|colleague|other
    system: 当 source="system" 时，具体系统名
    priority: low|normal|high|critical（默认 normal）
    deadline_minutes: SLA 时限/分钟（默认 120）

    **返回**: 同 human_action 的返回结构
    """
    return await _with_piggyback(_human_information_impl(
        question=question,
        context=context,
        domain=domain,
        source=source,
        system=system,
        priority=priority,
        deadline_minutes=deadline_minutes,
        **_tool_context(),
    ))


@mcp.tool()
async def human_poll(task_id: str | list[str]) -> dict:
    """Check the status and result of previously submitted human tasks.

    **用法**
    - 单个查询: human_poll("task_xxx") → 返回该任务的完整状态
    - 批量查询: human_poll(["task_1", "task_2"]) → 返回 {"tasks": [...], "summary": {...}}

    **编排提示**
    - Fan-Out 模式：同时派发多个独立任务后，用批量 human_poll 收集结果
    - 谁先完成先用谁的结果，不必等最慢的那个
    - 对每个返回结果执行四步验证：格式→一致性→完整性→合理性

    **参数**
    task_id: 单个任务 ID（字符串）或 ID 列表

    **返回**
    单个: {"task_id":"...", "status":"pending"|"completed"|"rejected", "result":"...", ...}
    批量: {"tasks":[...], "summary":{"pending":N, "completed":N, "rejected":N, "not_found":N}}
    """
    return await _with_piggyback(_human_poll_impl(task_id=task_id, task_manager=task_manager))


@mcp.tool()
async def human_cancel(
    task_id: str,
    reason: Optional[str] = None,
    action: str = "cancel",
    new_data: Optional[dict] = None,
) -> dict:
    """Cancel or modify a pending task that is no longer needed.

    **⚠️ 关键规则**
    - 任务不再需要时立即取消——不要浪费人类时间
    - 取消后不要重试同一个任务（换措辞也不行）
    - 人类确认后生效，非即时

    **参数**
    task_id: 要取消/修改的任务 ID（必填）
    reason: 原因说明
    action: "cancel"（默认）| "modify"
    new_data: 修改时的新字段 {"title":"...", "description":"...", "priority":"..."}

    **返回**
    {"status":"cancelled"|"modified", "task_id":"...", "message":"..."}
    """
    return await _with_piggyback(_human_cancel_impl(
        task_id=task_id,
        reason=reason,
        action=action,
        new_data=new_data,
        task_manager=task_manager,
        broadcast=broadcast,
    ))


@mcp.tool()
async def human_list_tasks(
    status: str = "pending",
    agent_id: Optional[str] = None,
    limit: int = 50,
    since: Optional[str] = None,
) -> dict:
    """List tasks by status, optionally filtered by agent and time.

    **用途**
    - 恢复上下文：新会话开始时，查看之前派了哪些任务
    - 队列监控：派发新任务前，检查是否已有积压
    - 跨会话追踪：AI 不需要自己记住所有 task_id

    **编排提示**
    - 启动时先调这个看有没有未完成的 pending 任务
    - 如果积压 >5 个，优先推进不依赖人类的工作
    - 结合 human_poll 获取已完成任务的结果
    - 传递 since 参数仅获取增量变化（ISO-8601 时间戳）

    **参数**
    status: "pending"(默认)|"completed"|"rejected"|"all"
    agent_id: 筛选特定 agent（默认不过滤）
    limit: 最大返回数（默认 50）
    since: ISO-8601 时间戳，只返回此时间之后创建或完成的任务

    **返回**
    {"tasks": [{"task_id":"...", "title":"...", "status":"...", ...}], "total": N}
    """
    return await _with_piggyback(_human_list_tasks_impl(
        status=status,
        agent_id=agent_id,
        limit=limit,
        since=since,
        task_manager=task_manager,
    ))


@mcp.tool()
async def human_wait(
    task_id: str,
    timeout: int = 300,
) -> dict:
    """Wait for a human to complete or reject a pending task.

    **用途** — 人类已离线，但你想第一时间拿到结果时使用
    派发 human_action/human_decision/human_information 后，如果返回
    sync=false（人类不在 180s 窗口内响应），调用 human_wait 阻塞等待。

    **工作方式**
    在服务端注册一个通知监听器，人类通过 Dashboard 完成任务时，服务端
    立即推送通知，human_wait 收到后返回完整结果。这是一个实时推送机制，
    不需要 Agent 反复轮询 human_poll。

    **参数**
    task_id: 要等待的任务 ID（必填）
    timeout: 最长等待秒数（默认 300s，最大 600s）

    **返回**
    {
      "task_id": "...", "status": "completed"|"rejected"|"pending",
      "wait_status": "notified"|"timeout"|"already_resolved",
      "result": "...",  // if completed
      "rejection_reason": "...",  // if rejected
      ...
    }

    wait_status 含义：
    - "notified" — 人类已响应，以下为完整结果
    - "timeout"  — 等待超时，人类尚未响应。可用 human_poll 继续轮询
    - "already_resolved" — 调用时任务已处于终态（可能中间已经完成了）
    """
    return await _human_wait_impl(
        task_id=task_id,
        timeout=timeout,
        task_manager=task_manager,
        register_session=register_mcp_session,
        unregister_session=unregister_mcp_session,
    )


# ===========================================================================
# HTTP route handlers (Dashboard + REST API)
# ===========================================================================


def _json_response(data: Any, status_code: int = 200) -> Any:
    """Return a JSONResponse with restrictive CORS headers."""
    from starlette.responses import JSONResponse

    cfg_server = _config.get("server", {})
    host = cfg_server.get("host", "127.0.0.1")
    port = cfg_server.get("port", 4350)
    origin = f"http://{host}:{port}"

    return JSONResponse(
        data,
        status_code=status_code,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Agent-ID",
        },
    )


async def _cors_preflight(request: Any) -> Any:
    """Handle CORS preflight OPTIONS requests."""
    from starlette.responses import Response

    cfg_server = _config.get("server", {})
    host = cfg_server.get("host", "127.0.0.1")
    port = cfg_server.get("port", 4350)
    origin = f"http://{host}:{port}"

    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Agent-ID",
    })


# --- Health ---------------------------------------------------------------


async def handle_health(request: Any) -> Any:
    """GET /health — liveness/readiness check."""
    db_ok = True
    if storage is not None:
        try:
            ok, _detail = await storage.check_integrity()
            db_ok = ok
        except Exception:
            db_ok = False

    status_code = 200 if db_ok else 503
    return _json_response(
        {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"},
        status_code=status_code,
    )


# --- Dashboard -----------------------------------------------------------


async def handle_dashboard(request: Any) -> Any:
    """GET /dashboard — serve the operator dashboard HTML page."""
    if DASHBOARD_FILE.exists():
        from starlette.responses import FileResponse
        return FileResponse(str(DASHBOARD_FILE), media_type="text/html; charset=utf-8")
    return _json_response(
        {"error": "Dashboard not found.  Create static/dashboard.html."},
        status_code=404,
    )


# --- API: pending tasks --------------------------------------------------


async def handle_api_tasks_pending(request: Any) -> Any:
    """GET /api/tasks/pending — return all pending tasks."""
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    try:
        tasks = await task_manager.get_pending_tasks()
        return _json_response(tasks)
    except Exception:
        logger.exception("Failed to fetch pending tasks")
        return _json_response({"error": "Internal server error"}, status_code=500)


# --- API: history --------------------------------------------------------


async def handle_api_tasks_history(request: Any) -> Any:
    """GET /api/tasks/history?limit=100&offset=0 — paginated task history."""
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    try:
        limit = int(request.query_params.get("limit", 100))
        offset = int(request.query_params.get("offset", 0))
        tasks = await task_manager.get_history_tasks(limit=limit, offset=offset)
        return _json_response(tasks)
    except ValueError:
        return _json_response({"error": "limit and offset must be integers"}, status_code=400)
    except Exception:
        logger.exception("Failed to fetch history tasks")
        return _json_response({"error": "Internal server error"}, status_code=500)


# --- API: single task ----------------------------------------------------


async def handle_api_task_detail(request: Any) -> Any:
    """GET /api/tasks/{task_id} — return a single task by ID."""
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    task_id = request.path_params.get("task_id", "")
    try:
        task = await task_manager.get_task(task_id)
    except Exception:
        logger.exception("Failed to fetch task %s", task_id)
        return _json_response({"error": "Internal server error"}, status_code=500)
    if task is None:
        return _json_response({"error": f"Task not found: {task_id}"}, status_code=404)
    return _json_response(task)


# --- API: complete task --------------------------------------------------


async def handle_api_task_complete(request: Any) -> Any:
    """POST /api/tasks/{task_id}/complete — mark a task as completed.

    Expects JSON body: ``{"result": "...", "evidence": [...]}``
    """
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    task_id = request.path_params.get("task_id", "")
    try:
        body: dict = await request.json()
    except Exception:
        body = {}
    result_text = body.get("result", "")
    evidence = body.get("evidence", [])

    try:
        task = await task_manager.complete_task(task_id, result_text, evidence)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to complete task %s", task_id)
        return _json_response({"error": "Internal server error"}, status_code=500)

    await broadcast({
        "type": "task_updated",
        "task_id": task_id,
        "status": "completed",
        "task": task,
    })

    # Push to active MCP client sessions so AI gets notified immediately.
    await push_to_mcp_sessions({
        "type": "task_completed",
        "task_id": task_id,
        "status": "completed",
        "title": task.get("title", ""),
        "agent_id": task.get("agent_id", ""),
        "completed_at": task.get("completed_at"),
    })

    return _json_response(task)


# --- API: reject task ----------------------------------------------------


async def handle_api_task_reject(request: Any) -> Any:
    """POST /api/tasks/{task_id}/reject — reject a task.

    Expects JSON body: ``{"reason": "...", "note": "..."}``
    """
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    task_id = request.path_params.get("task_id", "")
    try:
        body: dict = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "")
    note = body.get("note", "")

    try:
        task = await task_manager.reject_task(task_id, reason, note)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to reject task %s", task_id)
        return _json_response({"error": "Internal server error"}, status_code=500)

    await broadcast({
        "type": "task_updated",
        "task_id": task_id,
        "status": "rejected",
        "task": task,
    })

    await push_to_mcp_sessions({
        "type": "task_completed",
        "task_id": task_id,
        "status": "rejected",
        "title": task.get("title", ""),
        "agent_id": task.get("agent_id", ""),
        "rejection_reason": task.get("rejection_reason"),
        "completed_at": task.get("completed_at"),
    })

    return _json_response(task)


# --- API: confirm cancel -------------------------------------------------


async def handle_api_task_confirm_cancel(request: Any) -> Any:
    """POST /api/tasks/{task_id}/confirm-cancel — confirm a cancellation request."""
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    task_id = request.path_params.get("task_id", "")

    try:
        task = await task_manager.confirm_cancel(task_id)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to confirm cancel for task %s", task_id)
        return _json_response({"error": "Internal server error"}, status_code=500)

    await broadcast({
        "type": "task_updated",
        "task_id": task_id,
        "status": "cancelled",
    })
    return _json_response(task)


# --- API: session messages ------------------------------------------------


async def handle_api_session_messages(request: Any) -> Any:
    """GET /api/session/messages?limit=50 — 返回 HumanMCP 会话日志。"""
    try:
        limit = int(request.query_params.get("limit", 50))
    except ValueError:
        limit = 50

    log_path = PROJECT_ROOT / "data" / "session_log.jsonl"
    if not log_path.exists():
        return _json_response({"messages": [], "total": 0})

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return _json_response({"error": "Failed to read session log"}, status_code=500)

    messages = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        messages.append(msg)

    return _json_response({
        "messages": messages,
        "total": len(messages),
    })


async def handle_api_session_clear(request: Any) -> Any:
    """DELETE /api/session/messages — 清空会话日志。"""
    log_path = PROJECT_ROOT / "data" / "session_log.jsonl"
    try:
        if log_path.exists():
            log_path.unlink()
        return _json_response({"status": "cleared"})
    except Exception:
        return _json_response({"error": "Failed to clear session log"}, status_code=500)


# --- API: stats ----------------------------------------------------------


async def handle_api_stats(request: Any) -> Any:
    """GET /api/stats — return task manager statistics."""
    if task_manager is None:
        return _json_response({"error": "Server not initialized."}, status_code=503)
    try:
        stats = await task_manager.get_stats()
        return _json_response(stats)
    except Exception:
        logger.exception("Failed to fetch stats")
        return _json_response({"error": "Internal server error"}, status_code=500)


# ===========================================================================
# WebSocket handler (with optional token authentication)
# ===========================================================================


def _verify_ws_token(websocket: Any) -> bool:
    """Check the WebSocket query-string token against the configured shared secret.

    Returns True if auth is disabled, the token matches, or no secret is configured.
    """
    ws_cfg = _config.get("websocket", {})
    if not ws_cfg.get("auth_enabled", False):
        return True

    expected = ws_cfg.get("shared_secret", "").strip()
    if not expected:
        return True  # Auth enabled but no secret configured → allow all.

    # Extract token from query string.
    query_string = getattr(websocket, "query_params", None)
    if query_string is None:
        # Fallback: try to parse from the URL scope.
        scope = getattr(websocket, "scope", {})
        query_string = dict(
            q.split("=", 1) for q in scope.get("query_string", b"").decode().split("&")
            if "=" in q
        ) if scope.get("query_string") else {}

    token = ""
    if isinstance(query_string, dict):
        token = query_string.get("token", "")
    elif hasattr(query_string, "get"):
        token = query_string.get("token", "")

    if token != expected:
        logger.warning("WebSocket auth failed: bad token.")
        return False

    return True


async def handle_ws(websocket: Any) -> None:
    """WebSocket endpoint at /ws for real-time task updates.

    Supports optional token authentication via ``?token=xxx`` query parameter.
    Clients receive JSON messages like:
      - ``{"type": "new_task", "task": {...}}``
      - ``{"type": "task_updated", "task_id": "...", "status": "..."}``

    Also forwards ``user_chat`` messages from Dashboard → other clients (Gateway Plugin).
    """
    if not _verify_ws_token(websocket):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    ws_clients.add(websocket)
    logger.info("WebSocket client connected (%d total).", len(ws_clients))
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug("WS message received: %s", data[:200])
            # Forward user_chat messages to all clients (Dashboard → Gateway Plugin)
            try:
                msg = json.loads(data)
                if msg.get("type") == "user_chat":
                    await broadcast(msg)
            except (json.JSONDecodeError, Exception):
                pass
    except Exception:
        pass
    finally:
        ws_clients.discard(websocket)
        logger.info("WebSocket client disconnected (%d remaining).", len(ws_clients))


# ===========================================================================
# Route definitions
# ===========================================================================


def _build_routes() -> list:
    """Build the list of Starlette Route / WebSocketRoute / Mount objects."""
    from starlette.routing import Route, WebSocketRoute, Mount
    from starlette.staticfiles import StaticFiles

    api_routes: list = [
        # Health
        Route("/health", handle_health, methods=["GET"]),
        Route("/health", _cors_preflight, methods=["OPTIONS"]),
        # Dashboard
        Route("/dashboard", handle_dashboard, methods=["GET"]),
        Route("/dashboard", _cors_preflight, methods=["OPTIONS"]),
        # Pending tasks
        Route("/api/tasks/pending", handle_api_tasks_pending, methods=["GET"]),
        Route("/api/tasks/pending", _cors_preflight, methods=["OPTIONS"]),
        # History
        Route("/api/tasks/history", handle_api_tasks_history, methods=["GET"]),
        Route("/api/tasks/history", _cors_preflight, methods=["OPTIONS"]),
        # Single task
        Route("/api/tasks/{task_id:str}", handle_api_task_detail, methods=["GET"]),
        Route("/api/tasks/{task_id:str}", _cors_preflight, methods=["OPTIONS"]),
        # Complete
        Route("/api/tasks/{task_id:str}/complete", handle_api_task_complete, methods=["POST", "OPTIONS"]),
        # Reject
        Route("/api/tasks/{task_id:str}/reject", handle_api_task_reject, methods=["POST", "OPTIONS"]),
        # Confirm cancel
        Route("/api/tasks/{task_id:str}/confirm-cancel", handle_api_task_confirm_cancel, methods=["POST", "OPTIONS"]),
        # Stats
        Route("/api/stats", handle_api_stats, methods=["GET"]),
        Route("/api/stats", _cors_preflight, methods=["OPTIONS"]),
        # Session messages
        Route("/api/session/messages", handle_api_session_messages, methods=["GET"]),
        Route("/api/session/messages", handle_api_session_clear, methods=["DELETE"]),
        Route("/api/session/messages", _cors_preflight, methods=["OPTIONS"]),
        # WebSocket
        WebSocketRoute("/ws", handle_ws),
    ]

    # Mount static files if the directory exists
    if STATIC_DIR.exists():
        api_routes.insert(
            0,
            Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
        )

    return api_routes


# ===========================================================================
# Initialization
# ===========================================================================


async def init() -> None:
    """Initialise the server: load config, create Storage and TaskManager.

    Must be called once before the server starts.  Safe to call multiple
    times; subsequent calls are no-ops.
    """
    global _config, storage, task_manager

    if task_manager is not None:
        return  # Already initialised.

    _config = load_config()

    # --- Storage -----------------------------------------------------------
    db_path = _config.get("storage", {}).get("db_path", "data/tasks.db")
    storage = Storage(str(PROJECT_ROOT / db_path))

    # Initialise DB (create tables, run migrations, check integrity).
    await storage.initialize()

    # --- Task Manager ------------------------------------------------------
    task_manager = TaskManager(storage=storage, config=_config)

    logger.info("Storage initialised: db=%s", storage)
    logger.info("TaskManager initialised.")


def _print_startup_message() -> None:
    """Print a startup banner with service URLs."""
    cfg_server = _config.get("server", {})
    host = cfg_server.get("host", "127.0.0.1")
    port = cfg_server.get("port", 4350)
    name = cfg_server.get("name", "HumanMCP")

    banner = [
        "",
        "=" * 62,
        f"  {name} Server",
        f"  MCP Endpoint : http://{host}:{port}/mcp",
        f"  Dashboard    : http://{host}:{port}/dashboard",
        f"  Health Check : http://{host}:{port}/health",
        f"  API Base     : http://{host}:{port}/api",
        f"  WebSocket    : ws://{host}:{port}/ws",
        "=" * 62,
        "",
    ]
    for line in banner:
        logger.info(line)


def _register_custom_routes() -> None:
    """Register HTTP/WebSocket routes and static-file mount with FastMCP's
    underlying Starlette application.

    Uses FastMCP's built-in ``_additional_http_routes`` list which is
    consumed by ``http_app()`` every time it builds the Starlette app,
    ensuring routes survive across the full app lifecycle.
    """
    custom_routes = _build_routes()

    # FastMCP >=2.12 maintains a list of additional HTTP routes that are
    # always included when building the Starlette app.  This is the only
    # reliable way to add routes without losing them on rebuild.
    mcp._additional_http_routes.extend(custom_routes)

    logger.info("Custom HTTP routes and WebSocket registered (%d routes).", len(custom_routes))
    for r in custom_routes:
        path_repr = getattr(r, "path", str(r))
        methods = getattr(r, "methods", None)
        if methods:
            logger.debug("  %-7s %s", ",".join(methods), path_repr)
        else:
            logger.debug("  %-7s %s", "WS", path_repr)


# ===========================================================================
# Application factory
# ===========================================================================


def create_app() -> FastMCP:
    """Create and fully configure the HumanMCP FastMCP application.

    Returns the FastMCP instance ready to be served via::

        fastmcp run server.app:mcp

    or programmatically::

        from server.app import create_app
        mcp = create_app()
        mcp.run()
    """
    # Run the async init synchronously (this is the app-factory entry point).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # We're inside an async context — the caller must have called
        # ``await init()`` before ``create_app()``.
        if task_manager is None:
            raise RuntimeError(
                "init() must be awaited before create_app() in an async context. "
                "Call: await init() first."
            )
    else:
        # Synchronous context — run init in a new event loop.
        asyncio.run(init())

    _print_startup_message()
    _register_custom_routes()
    return mcp
