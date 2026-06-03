"""
HumanMCP → Hermes Gateway 平台适配器。

Dashboard 操作回调 → Gateway 会话投递 → Agent 实时处理。
Gateway 启动时自动拉起 HumanMCP 服务。
支持通过 config 配置工作目录（自动 cd）。
"""
import asyncio
import contextlib
import json
import logging
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger("gateway.platforms.humanmcp")

SESSION_LOG = Path(r"H:\Human\data\session_log.jsonl")
SESSION_LOG_MAX_LINES = 500
HUMANMCP_SERVER = r"H:\Human\server\main.py"
HUMANMCP_URL = "http://127.0.0.1:4350"
HUMANMCP_PYTHON = r"C:\Python313\python.exe"


def _write_session_entry(role: str, text: str, task_id: str = "") -> None:
    try:
        os.makedirs(SESSION_LOG.parent, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "text": text,
            "task_id": task_id or "",
        }
        with open(SESSION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim_session_log()
    except Exception as e:
        logger.debug("[humanmcp] 写入会话日志失败: %s", e)


def _trim_session_log() -> None:
    try:
        if not SESSION_LOG.exists():
            return
        with open(SESSION_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > SESSION_LOG_MAX_LINES:
            with open(SESSION_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-SESSION_LOG_MAX_LINES:])
    except Exception:
        pass


def _is_humanmcp_running() -> bool:
    try:
        req = urllib.request.Request(
            f"{HUMANMCP_URL}/health",
            headers={"User-Agent": "humanmcp-plugin/1.0"},
        )
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception:
        return False


async def _ensure_humanmcp_running() -> Optional[subprocess.Popen]:
    if _is_humanmcp_running():
        logger.info("[humanmcp] HumanMCP 服务已在运行")
        return None
    logger.info("[humanmcp] HumanMCP 未运行，自动拉起...")
    try:
        proc = subprocess.Popen(
            [HUMANMCP_PYTHON, HUMANMCP_SERVER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for _ in range(30):
            await asyncio.sleep(1)
            if _is_humanmcp_running():
                logger.info("[humanmcp] ✅ HumanMCP 已就绪 (PID %d)", proc.pid)
                return proc
        logger.warning("[humanmcp] ⚠️ HumanMCP 启动超时")
        return proc
    except Exception as e:
        logger.error("[humanmcp] 启动 HumanMCP 失败: %s", e)
        return None


class HumanMCPAdapter(BasePlatformAdapter):
    """HumanMCP WebSocket → Gateway 会话。"""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("humanmcp"))
        self._ws_url = config.extra.get("ws_url", "ws://127.0.0.1:4350/ws")
        self._workdir = config.extra.get("workdir", "")
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[subprocess.Popen] = None
        self._init_done = False
        self.gateway_runner: Any = None

    async def connect(self) -> bool:
        self._proc = await _ensure_humanmcp_running()
        self._task = asyncio.create_task(self._listen())
        logger.info("[humanmcp] 启动 — WS: %s | workdir: %s", self._ws_url, self._workdir or "(未配置)")
        return True

    async def disconnect(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._proc:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                logger.info("[humanmcp] HumanMCP 进程已终止 (PID %d)", self._proc.pid)
            except Exception as e:
                logger.debug("[humanmcp] 终止 HumanMCP 时出错: %s", e)
            self._proc = None

    async def _listen(self) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=30, ping_timeout=10,
                ) as ws:
                    logger.info("[humanmcp] WebSocket 已连接")
                    # 首次连接时设置工作目录
                    if self._workdir and not self._init_done:
                        await asyncio.sleep(2)
                        await self._send_system_cd()
                        self._init_done = True
                    while True:
                        raw = await ws.recv()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await self._on_callback(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[humanmcp] 断开: %s — 5s 重连", e)
                await asyncio.sleep(5)

    async def _send_system_cd(self) -> None:
        """发送 cd 指令到 Agent 会话。"""
        logger.info("[humanmcp] 设工作目录: %s", self._workdir)
        from gateway.session import SessionSource
        source = SessionSource(
            platform=Platform("humanmcp"),
            chat_id="humanmcp:dashboard",
            chat_name="HumanMCP Dashboard",
            chat_type="dm",
            user_id="humanmcp",
            user_name="HumanMCP",
        )
        event = MessageEvent(
            text=f"cd {self._workdir}",
            message_type=MessageType.TEXT,
            source=source,
            message_id="hmcp_init_cd",
            internal=True,
            auto_skill="humanmcp",
            channel_prompt=(
                f"[系统指令] 工作目录: {self._workdir}。"
                f"所有 terminal 操作默认在此目录下执行。"
            ),
        )
        try:
            await self.handle_message(event)
            logger.info("[humanmcp] ✅ cd 指令已发送")
        except Exception as e:
            logger.error("[humanmcp] cd 发送失败: %s", e)
    async def _on_callback(self, data: dict) -> None:
        event_type = data.get("type", "")

        # Dashboard 用户输入
        if event_type == "user_chat":
            text = data.get("text", "").strip()
            if text:
                await self._route_to_gateway(text, task_id="")
            return

        # Dashboard 切换工作目录
        if event_type == "workdir_changed":
            new_dir = data.get("workdir", "")
            if new_dir and new_dir != self._workdir:
                self._workdir = new_dir
                self._init_done = False  # 强制重发
                await self._send_system_cd()
                self._init_done = True
            return

        if event_type not in ("task_updated", "new_task"):
            return

        task_id = data.get("task_id", "")
        status = data.get("status", "")
        emoji = {"completed": "✅", "rejected": "❌", "modified": "✏️", "cancelled": "🚫"}.get(status, "📨")
        status_cn = {"completed": "已完成", "rejected": "已拒绝", "modified": "已修改", "cancelled": "已取消"}.get(status, status)

        task_data = data.get("task", {})
        title = task_data.get("title", "?")
        agent = task_data.get("agent_id", "?")
        result = task_data.get("result", "")
        rejection = task_data.get("rejection_reason", "")
        rejection_note = task_data.get("rejection_note", "")

        prompt = f"{emoji} **{title}**\n"
        prompt += f"状态: {status_cn} | 来自: {agent}\n\n"
        if status == "completed" and result:
            prompt += f"**结果:** {result}\n"
        elif status == "rejected":
            prompt += f"**理由:** {rejection}"
            if rejection_note:
                prompt += f" ({rejection_note})"
            prompt += "\n"
        prompt += f"task_id: {task_id}"

        await self._route_to_gateway(prompt, task_id)

    async def _route_to_gateway(self, text: str, task_id: str) -> None:
        from gateway.session import SessionSource

        if not text.startswith("/") and not text.startswith("请将工作目录切换到") and not text.startswith("cd "):
            _write_session_entry("user", text, task_id)

        source = SessionSource(
            platform=Platform("humanmcp"),
            chat_id="humanmcp:dashboard",
            chat_name="HumanMCP Dashboard",
            chat_type="dm",
            user_id="humanmcp",
            user_name="HumanMCP",
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"hmcp_{task_id}",
            internal=True,
            auto_skill="humanmcp",
        )

        try:
            await self.handle_message(event)
            logger.info("[humanmcp] ✅ 已投递到 Gateway Session")
        except Exception as e:
            logger.error("[humanmcp] Gateway 投递失败: %s", e)

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None,
                   **kwargs) -> SendResult:
        _write_session_entry("agent", content)
        logger.info("[humanmcp] Agent 回复 (%d chars) → %s", len(content), chat_id)
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "HumanMCP Dashboard", "type": "humanmcp"}


def register(ctx):
    ctx.register_platform(
        name="humanmcp",
        label="HumanMCP Dashboard",
        adapter_factory=lambda cfg: HumanMCPAdapter(cfg),
        check_fn=_check,
        emoji="👤",
    )


def _check() -> bool:
    try:
        import websockets  # noqa
        return True
    except ImportError:
        return False
