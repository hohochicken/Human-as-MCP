"""Tests for task_manager.py — task lifecycle state machine."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from server.models import Task, TaskStatus, Priority, RejectionReason
from server.task_manager import TaskManager


@pytest.fixture
def mock_storage():
    """Return a mock Storage with async methods."""
    storage = MagicMock()
    storage.save_task = MagicMock()
    storage.get_task = MagicMock(return_value=None)
    storage.get_tasks_by_status = MagicMock(return_value=[])
    storage.get_history_tasks = MagicMock(return_value=[])
    storage.update_task = MagicMock()
    storage.get_task_counts = MagicMock(return_value={
        "pending": 0, "completed": 0, "rejected": 0, "total": 0,
    })
    return storage


@pytest.fixture
def config():
    return {
        "task_defaults": {
            "default_priority": "normal",
            "default_deadline_minutes": 120,
            "max_title_length": 200,
            "max_description_length": 10000,
        },
    }


@pytest.fixture
def task_manager(mock_storage, config):
    return TaskManager(storage=mock_storage, config=config)


class TestTaskCreation:
    """Tests for create_task."""

    @pytest.mark.asyncio
    async def test_create_task_basic(self, task_manager, mock_storage):
        result = await task_manager.create_task(
            title="Test task",
            description="Test description",
            tool_name="human_action",
        )
        assert result["task_id"]
        assert result["title"] == "Test task"
        assert result["status"] == "pending"
        assert result["tool_name"] == "human_action"
        mock_storage.save_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_with_priority(self, task_manager):
        result = await task_manager.create_task(
            title="Urgent",
            description="Do it now",
            tool_name="human_decision",
            priority="critical",
            deadline_minutes=10,
            agent_id="agent-42",
        )
        assert result["priority"] == "critical"
        assert result["deadline_minutes"] == 10
        assert result["agent_id"] == "agent-42"

    @pytest.mark.asyncio
    async def test_create_task_stores_extra_params(self, task_manager, mock_storage):
        await task_manager.create_task(
            title="With steps",
            description="Has steps",
            tool_name="human_action",
            steps=["step1", "step2"],
            action_type="build",
        )
        # The saved Task should have params.
        saved_task = mock_storage.save_task.call_args[0][0]
        assert saved_task.params is not None
        assert saved_task.params.get("steps") == ["step1", "step2"]
        assert saved_task.params.get("action_type") == "build"


class TestTaskLifecycle:
    """Tests for complete, reject, cancel, confirm_cancel."""

    @pytest.mark.asyncio
    async def test_complete_task(self, task_manager, mock_storage):
        task = Task(
            title="To complete",
            description="Complete me",
            tool_name="human_information",
        )
        mock_storage.get_task.return_value = task

        result = await task_manager.complete_task(
            task.task_id, "Done!", ["evidence.txt"]
        )
        assert result["status"] == "completed"
        assert result["result"] == "Done!"
        assert result["evidence"] == ["evidence.txt"]
        assert result["completed_at"] is not None
        mock_storage.update_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_nonexistent_task(self, task_manager, mock_storage):
        mock_storage.get_task.return_value = None
        with pytest.raises(ValueError, match="Task not found"):
            await task_manager.complete_task("bad-id", "result", [])

    @pytest.mark.asyncio
    async def test_complete_already_completed_task(self, task_manager, mock_storage):
        task = Task(title="Done", description="Already done", tool_name="human_action")
        task.status = TaskStatus.COMPLETED
        mock_storage.get_task.return_value = task

        with pytest.raises(ValueError, match="not pending"):
            await task_manager.complete_task(task.task_id, "again", [])

    @pytest.mark.asyncio
    async def test_reject_task(self, task_manager, mock_storage):
        task = Task(
            title="To reject",
            description="Reject me",
            tool_name="human_access",
        )
        mock_storage.get_task.return_value = task

        result = await task_manager.reject_task(
            task.task_id, "ai_can_do", "You can do it"
        )
        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "ai_can_do"
        assert result["rejection_note"] == "You can do it"
        assert result["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_reject_with_unclear_reason(self, task_manager, mock_storage):
        task = Task(title="Unclear", description="???", tool_name="human_coordinate")
        mock_storage.get_task.return_value = task

        result = await task_manager.reject_task(task.task_id, "unclear", "")
        assert result["rejection_reason"] == "unclear"

    @pytest.mark.asyncio
    async def test_cancel_task(self, task_manager, mock_storage):
        task = Task(title="To cancel", description="Cancel me", tool_name="human_action")
        mock_storage.get_task.return_value = task

        result = await task_manager.cancel_task(task.task_id, "no longer needed")
        assert result["cancel_requested"] is True
        assert result["cancel_action"] == "cancel"

    @pytest.mark.asyncio
    async def test_confirm_cancel(self, task_manager, mock_storage):
        task = Task(title="Cancel pending", description="...", tool_name="human_action")
        task.cancel_requested = True
        mock_storage.get_task.return_value = task

        result = await task_manager.confirm_cancel(task.task_id)
        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "ai_can_do"

    @pytest.mark.asyncio
    async def test_confirm_cancel_without_request(self, task_manager, mock_storage):
        task = Task(title="No cancel req", description="...", tool_name="human_action")
        mock_storage.get_task.return_value = task

        with pytest.raises(ValueError, match="no pending cancel request"):
            await task_manager.confirm_cancel(task.task_id)


class TestTaskUpdate:
    """Tests for update_task."""

    @pytest.mark.asyncio
    async def test_update_title(self, task_manager, mock_storage):
        task = Task(title="Old", description="Desc", tool_name="human_action")
        mock_storage.get_task.return_value = task

        result = await task_manager.update_task(task.task_id, {"title": "New Title"})
        assert result["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_update_ignores_invalid_keys(self, task_manager, mock_storage):
        task = Task(title="Safe", description="Desc", tool_name="human_action")
        mock_storage.get_task.return_value = task

        result = await task_manager.update_task(
            task.task_id, {"status": "completed", "agent_id": "hacker"}
        )
        # These keys should be ignored.
        assert result["status"] == "pending"
        assert result["agent_id"] == "unknown"


class TestStats:
    """Tests for get_stats."""

    @pytest.mark.asyncio
    async def test_get_stats(self, task_manager, mock_storage):
        mock_storage.get_task_counts.return_value = {
            "pending": 3, "completed": 10, "rejected": 2, "total": 15,
        }
        stats = await task_manager.get_stats()
        assert stats["pending"] == 3
        assert stats["completed"] == 10
        assert stats["total"] == 15
