"""
环境感知的可用性检测。

通过 WebSocket 连接状态、最近人类活动时间、当前时间
来判断人类是否"在线"，并据此调整 HumanMCP 的行为模式。
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


class AvailabilityState(str, Enum):
    ACTIVE = "active"       # 人类正在活跃使用 Dashboard
    FOCUS = "focus"         # 人类在线但专注工作，不宜打断
    AWAY = "away"           # 人类暂时离开（会议/午餐等）
    OFFLINE = "offline"     # 人类已下班/长时间不在


# 默认工作时间窗口（UTC，可配置）
DEFAULT_WORK_HOURS = (1, 10)   # UTC 1:00-10:00 ≈ 北京 9:00-18:00
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)  # Mon-Fri

# 状态转换阈值（秒）
FOCUS_THRESHOLD = 300        # 5 分钟无操作 → focus
AWAY_THRESHOLD = 1800        # 30 分钟无操作 → away
OFFLINE_THRESHOLD = 7200     # 2 小时无操作 → offline


@dataclass
class AvailabilityStatus:
    state: AvailabilityState
    last_human_action: float      # Unix timestamp of last human action
    ws_connected: bool
    since_state_change: float     # 进入当前状态多久了（秒）
    pending_since_offline: int = 0  # 离线期间积累的任务数


class AvailabilityDetector:
    """检测人类可用性状态，并在状态变化时触发回调。"""

    def __init__(
        self,
        work_hours: tuple[int, int] = DEFAULT_WORK_HOURS,
        work_days: tuple[int, ...] = DEFAULT_WORK_DAYS,
        focus_threshold: float = FOCUS_THRESHOLD,
        away_threshold: float = AWAY_THRESHOLD,
        offline_threshold: float = OFFLINE_THRESHOLD,
    ):
        self.work_hours = work_hours
        self.work_days = work_days
        self.focus_threshold = focus_threshold
        self.away_threshold = away_threshold
        self.offline_threshold = offline_threshold

        self._state = AvailabilityState.ACTIVE
        self._last_human_action: float = time.time()
        self._ws_connected: bool = False
        self._state_changed_at: float = time.time()
        self._pending_since_offline: int = 0
        self._callbacks: list[Callable] = []

    # ── 公共接口 ──────────────────────────────────────────────

    @property
    def state(self) -> AvailabilityState:
        return self._state

    @property
    def last_human_action(self) -> float:
        return self._last_human_action

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected

    def record_human_action(self) -> None:
        """记录一次人类活动（Dashboard 交互、任务完成等）。"""
        self._last_human_action = time.time()

    def set_ws_connected(self, connected: bool) -> None:
        """更新 WebSocket 连接状态。"""
        old = self._ws_connected
        self._ws_connected = connected

        # Dashboard 刚打开 → 记录为人类活动
        if connected and not old:
            self.record_human_action()

    def on_state_change(self, callback):
        """注册状态变更回调。callback(old_state, new_state, status)。"""
        self._callbacks.append(callback)

    def get_state(self) -> AvailabilityState:
        """返回当前检测到的可用性状态（同步，无副作用）。"""
        now = time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        hour = now_dt.hour
        weekday = now_dt.weekday()

        # 非工作时间？
        is_work_hour = self.work_hours[0] <= hour <= self.work_hours[1]
        is_work_day = weekday in self.work_days

        if not is_work_hour or not is_work_day:
            return AvailabilityState.OFFLINE

        # 基于最后活动时间判断
        elapsed = now - self._last_human_action

        if elapsed < self.focus_threshold:
            return AvailabilityState.ACTIVE
        elif elapsed < self.away_threshold:
            return AvailabilityState.FOCUS
        elif elapsed < self.offline_threshold:
            return AvailabilityState.AWAY
        else:
            return AvailabilityState.OFFLINE

    async def tick(self) -> Optional[AvailabilityStatus]:
        """执行一次状态检测 tick。状态变化时触发回调并返回新状态。"""
        new_state = self.get_state()
        now = time.time()

        if new_state != self._state:
            old = self._state
            self._state = new_state
            self._state_changed_at = now

            status = AvailabilityStatus(
                state=new_state,
                last_human_action=self._last_human_action,
                ws_connected=self._ws_connected,
                since_state_change=0.0,
                pending_since_offline=self._pending_since_offline,
            )

            logger.info(
                "Availability state: %s → %s (last action %.0fs ago, ws=%s)",
                old.value, new_state.value,
                now - self._last_human_action,
                self._ws_connected,
            )

            # 触发回调
            for cb in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(old, new_state, status)
                    else:
                        cb(old, new_state, status)
                except Exception:
                    logger.debug("Availability callback failed", exc_info=True)

            if new_state in (AvailabilityState.AWAY, AvailabilityState.OFFLINE):
                self._pending_since_offline = 0

            return status

        return None

    # ── 离线期间任务计数 ──────────────────────────────────────

    def increment_pending_offline(self) -> None:
        """记录一个在离线期间创建的任务。"""
        if self._state in (AvailabilityState.AWAY, AvailabilityState.OFFLINE):
            self._pending_since_offline += 1

    def reset_pending_offline(self) -> int:
        """重置离线计数并返回累积数量。"""
        count = self._pending_since_offline
        self._pending_since_offline = 0
        return count


# ── 行为适配器 ──────────────────────────────────────────────

def get_block_timeout_for_state(state: AvailabilityState, priority: str) -> int:
    """根据可用性状态和任务优先级返回动态 BLOCK_TIMEOUT（秒）。"""
    if state == AvailabilityState.ACTIVE:
        return 180 if priority in ("high", "critical") else 60
    elif state == AvailabilityState.FOCUS:
        if priority == "critical":
            return 60
        elif priority == "high":
            return 15
        else:
            return 0
    elif state == AvailabilityState.AWAY:
        return 0
    else:  # OFFLINE
        return 0


def should_send_toast(state: AvailabilityState, priority: str) -> bool:
    """判断是否应该发送 Toast 通知。"""
    if state == AvailabilityState.ACTIVE:
        return True
    elif state == AvailabilityState.FOCUS:
        return priority in ("high", "critical")
    else:
        return False


def get_agent_feedback(state: AvailabilityState, task_id: str) -> str:
    """根据人类可用性状态生成给 Agent 的反馈消息。"""
    if state == AvailabilityState.ACTIVE:
        return (
            f"Task {task_id} queued. "
            f"Poll with human_poll('{task_id}') for the result."
        )
    elif state == AvailabilityState.FOCUS:
        return (
            f"Task {task_id} queued. Human is in focus mode — "
            f"response may be delayed. Poll with human_poll('{task_id}')."
        )
    elif state == AvailabilityState.AWAY:
        return (
            f"Task {task_id} queued. Human appears to be away "
            f"(no activity for >30 min). Task will be processed when they return."
        )
    else:
        return (
            f"Task {task_id} queued. Human is offline. "
            f"Task will be processed on next business day."
        )
