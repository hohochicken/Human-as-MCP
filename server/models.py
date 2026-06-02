from __future__ import annotations

import uuid
import datetime
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class RejectionReason(str, Enum):
    AI_CAN_DO = "ai_can_do"
    UNCLEAR = "unclear"
    OUT_OF_SCOPE = "out_of_scope"
    INVALID_TASK = "invalid_task"


@dataclass
class Task:
    """Core model representing a task delegated from AI to a human."""
    title: str
    description: str
    tool_name: str
    task_id: str = field(default="")
    priority: Priority = Priority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    agent_id: str = "unknown"
    created_at: str = ""
    deadline_minutes: int = 120
    result: Optional[str] = None
    evidence: Optional[list[str]] = None
    rejection_reason: Optional[RejectionReason] = None
    rejection_note: Optional[str] = None
    completed_at: Optional[str] = None
    cancel_requested: bool = False
    cancel_action: Optional[str] = None
    cancel_new_data: Optional[dict] = None
    params: Optional[dict] = None

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Serialize the Task to a JSON-compatible dictionary."""
        d: dict = {}
        d["task_id"] = self.task_id
        d["title"] = self.title
        d["description"] = self.description
        d["tool_name"] = self.tool_name
        d["priority"] = self.priority.value
        d["status"] = self.status.value
        d["agent_id"] = self.agent_id
        d["created_at"] = self.created_at
        d["deadline_minutes"] = self.deadline_minutes
        d["result"] = self.result
        d["evidence"] = self.evidence
        d["rejection_reason"] = self.rejection_reason.value if self.rejection_reason else None
        d["rejection_note"] = self.rejection_note
        d["completed_at"] = self.completed_at
        d["cancel_requested"] = self.cancel_requested
        d["cancel_action"] = self.cancel_action
        d["cancel_new_data"] = self.cancel_new_data
        d["params"] = self.params
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        """Deserialize a Task from a JSON-compatible dictionary."""
        data = dict(d)
        data["priority"] = Priority(data["priority"])
        data["status"] = TaskStatus(data["status"])
        if data.get("rejection_reason"):
            data["rejection_reason"] = RejectionReason(data["rejection_reason"])
        return cls(**data)


@dataclass
class TaskCreateRequest:
    """Input model for creating a task via a human_* tool."""
    title: str
    description: str
    tool_name: str = "human_task"
    priority: str = "normal"
    deadline_minutes: int = 120
    agent_id: str = "unknown"
    params: dict = field(default_factory=dict)
