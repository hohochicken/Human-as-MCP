"""Tests for models.py — Task serialization and deserialization."""

import pytest
from server.models import (
    Task,
    TaskCreateRequest,
    TaskStatus,
    Priority,
    RejectionReason,
)


class TestTaskModel:
    """Tests for the Task dataclass."""

    def test_task_creation_defaults(self):
        """Task should auto-generate task_id and created_at."""
        task = Task(
            title="Test task",
            description="A test description",
            tool_name="human_action",
        )
        assert task.task_id
        assert len(task.task_id) == 36  # UUID4
        assert task.created_at
        assert task.status == TaskStatus.PENDING
        assert task.priority == Priority.NORMAL
        assert task.agent_id == "unknown"
        assert task.deadline_minutes == 120
        assert task.result is None
        assert task.evidence is None
        assert task.cancel_requested is False

    def test_task_to_dict(self):
        """to_dict() should produce a JSON-compatible dictionary."""
        task = Task(
            title="Test task",
            description="Desc",
            tool_name="human_information",
            priority=Priority.HIGH,
            deadline_minutes=30,
            agent_id="agent-1",
        )
        d = task.to_dict()
        assert d["task_id"] == task.task_id
        assert d["title"] == "Test task"
        assert d["description"] == "Desc"
        assert d["tool_name"] == "human_information"
        assert d["priority"] == "high"
        assert d["status"] == "pending"
        assert d["agent_id"] == "agent-1"
        assert d["deadline_minutes"] == 30
        assert "created_at" in d

    def test_task_to_dict_with_result(self):
        """to_dict() should include result and evidence when set."""
        task = Task(
            title="Done task",
            description="Done",
            tool_name="human_action",
        )
        task.status = TaskStatus.COMPLETED
        task.result = "All done"
        task.evidence = ["screenshot.png"]
        task.rejection_reason = None

        d = task.to_dict()
        assert d["status"] == "completed"
        assert d["result"] == "All done"
        assert d["evidence"] == ["screenshot.png"]

    def test_task_to_dict_with_rejection(self):
        """to_dict() should serialize rejection fields."""
        task = Task(
            title="Rejected task",
            description="Bad",
            tool_name="human_access",
        )
        task.status = TaskStatus.REJECTED
        task.rejection_reason = RejectionReason.AI_CAN_DO
        task.rejection_note = "You can do this yourself"

        d = task.to_dict()
        assert d["status"] == "rejected"
        assert d["rejection_reason"] == "ai_can_do"
        assert d["rejection_note"] == "You can do this yourself"

    def test_task_roundtrip(self):
        """Task → to_dict → from_dict should produce an equivalent Task."""
        original = Task(
            title="Roundtrip test",
            description="Testing serialization",
            tool_name="human_decision",
            priority=Priority.CRITICAL,
            deadline_minutes=5,
            agent_id="test-agent",
        )
        original.result = "Decided"
        original.evidence = ["log.txt"]
        original.completed_at = "2026-06-02T12:00:00+00:00"
        original.cancel_requested = True
        original.cancel_action = "cancel"
        original.cancel_new_data = {"reason": "no longer needed"}
        original.params = {"options": ["A", "B"]}

        d = original.to_dict()
        restored = Task.from_dict(d)

        assert restored.task_id == original.task_id
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.tool_name == original.tool_name
        assert restored.priority == original.priority
        assert restored.status == original.status
        assert restored.agent_id == original.agent_id
        assert restored.result == original.result
        assert restored.evidence == original.evidence
        assert restored.cancel_requested == original.cancel_requested
        assert restored.params == original.params

    def test_task_params_field(self):
        """Task should store tool-specific params."""
        task = Task(
            title="With params",
            description="Test",
            tool_name="human_action",
            params={"steps": ["step1", "step2"], "action_type": "build"},
        )
        d = task.to_dict()
        assert d["params"] == {"steps": ["step1", "step2"], "action_type": "build"}


class TestEnums:
    """Tests for enum values."""

    def test_task_status_values(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.REJECTED.value == "rejected"

    def test_priority_values(self):
        assert Priority.LOW.value == "low"
        assert Priority.NORMAL.value == "normal"
        assert Priority.HIGH.value == "high"
        assert Priority.CRITICAL.value == "critical"

    def test_rejection_reason_values(self):
        assert RejectionReason.AI_CAN_DO.value == "ai_can_do"
        assert RejectionReason.UNCLEAR.value == "unclear"
        assert RejectionReason.OUT_OF_SCOPE.value == "out_of_scope"
        assert RejectionReason.INVALID_TASK.value == "invalid_task"


class TestTaskCreateRequest:
    """Tests for the TaskCreateRequest model."""

    def test_defaults(self):
        req = TaskCreateRequest(title="T", description="D")
        assert req.title == "T"
        assert req.description == "D"
        assert req.tool_name == "human_task"
        assert req.priority == "normal"
        assert req.deadline_minutes == 120
        assert req.agent_id == "unknown"
        assert req.params == {}

    def test_with_params(self):
        req = TaskCreateRequest(
            title="T",
            description="D",
            tool_name="human_action",
            priority="high",
            deadline_minutes=30,
            agent_id="agent-7",
            params={"steps": ["do X"]},
        )
        assert req.tool_name == "human_action"
        assert req.priority == "high"
        assert req.deadline_minutes == 30
        assert req.agent_id == "agent-7"
        assert req.params == {"steps": ["do X"]}
