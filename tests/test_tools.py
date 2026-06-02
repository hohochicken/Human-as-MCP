"""Tests for MCP tools — action, information, poll, list_tasks.

Tests the simplified tool set by injecting mock task_manager and storage,
verifying that each tool handles its parameters correctly and produces the
expected output shapes.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, call


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from server.tools.action import human_action
from server.tools.information import human_information
from server.tools.infrastructure import human_poll, human_list_tasks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task_manager():
    """Return a MagicMock standing in for TaskManager."""
    tm = MagicMock()
    tm.create_task = AsyncMock()
    tm.get_task = AsyncMock()
    tm.list_tasks = AsyncMock()
    return tm


@pytest.fixture
def mock_storage():
    """Return a MagicMock standing in for Storage."""
    storage = MagicMock()
    storage.get_recent_tasks = AsyncMock(return_value=[])
    return storage


@pytest.fixture
def mock_run_pipeline(monkeypatch):
    """Replace run_pipeline in action.py and information.py with an AsyncMock.

    Returns the mock so tests can assert how the tool called the pipeline.
    """
    mock = AsyncMock(return_value={
        "task_id": "task-mock-001",
        "status": "pending",
        "sync": False,
        "message": "Task queued (mock).",
    })
    monkeypatch.setattr("server.tools.action.run_pipeline", mock)
    monkeypatch.setattr("server.tools.information.run_pipeline", mock)
    return mock


# ---------------------------------------------------------------------------
# human_action
# ---------------------------------------------------------------------------


class TestHumanAction:
    """Tests for human_action — unified delegation to a human operator."""

    @pytest.mark.asyncio
    async def test_human_action_basic(
        self, mock_task_manager, mock_storage, mock_run_pipeline,
    ):
        """Create a task with action_type="other" (the default)."""
        result = await human_action(
            title="Restart server",
            description="Please restart the staging server.",
            task_manager=mock_task_manager,
            storage=mock_storage,
        )

        assert result["task_id"] == "task-mock-001"
        assert result["status"] == "pending"
        mock_run_pipeline.assert_called_once()

        # Verify pipeline call parameters
        call_kwargs = mock_run_pipeline.call_args.kwargs
        assert call_kwargs["title"] == "Restart server"
        assert call_kwargs["tool_name"] == "human_action"
        assert call_kwargs["priority"] == "normal"
        assert call_kwargs["action_type"] == "other"

    @pytest.mark.asyncio
    async def test_human_action_coordination(
        self, mock_task_manager, mock_storage, mock_run_pipeline,
    ):
        """Create a coordination task with target_person."""
        result = await human_action(
            title="Sync with backend team",
            description="Align API contract for the new endpoint.",
            action_type="coordination",
            target_person="Zhang San",
            task_manager=mock_task_manager,
            storage=mock_storage,
        )

        assert result["status"] == "pending"
        call_kwargs = mock_run_pipeline.call_args.kwargs
        assert call_kwargs["action_type"] == "coordination"
        assert call_kwargs["target_person"] == "Zhang San"
        # Description should embed the target person
        assert "Zhang San" in call_kwargs["description"]

    @pytest.mark.asyncio
    async def test_human_action_rejects_empty_title(
        self, mock_task_manager, mock_storage, mock_run_pipeline,
    ):
        """Return error when title is empty — never call the pipeline."""
        result = await human_action(
            title="",
            description="Some description",
            task_manager=mock_task_manager,
            storage=mock_storage,
        )

        assert result["status"] == "error"
        assert "title" in result["message"].lower()
        mock_run_pipeline.assert_not_called()


# ---------------------------------------------------------------------------
# human_information
# ---------------------------------------------------------------------------


class TestHumanInformation:
    """Tests for human_information — ask a human operator a question."""

    @pytest.mark.asyncio
    async def test_human_information_basic(
        self, mock_task_manager, mock_storage, mock_run_pipeline,
    ):
        """Ask a simple question; pipeline receives tool_name='human_information'."""
        result = await human_information(
            question="Where is the deployment config stored?",
            context="I need to update the CI/CD pipeline.",
            task_manager=mock_task_manager,
            storage=mock_storage,
        )

        assert result["task_id"] == "task-mock-001"
        assert result["status"] == "pending"
        mock_run_pipeline.assert_called_once()

        call_kwargs = mock_run_pipeline.call_args.kwargs
        assert call_kwargs["tool_name"] == "human_information"
        # The question text becomes the title.
        assert "deployment config" in call_kwargs["title"]
        # Description includes the question and context.
        assert "Question:" in call_kwargs["description"]
        assert "CI/CD" in call_kwargs["description"]

    @pytest.mark.asyncio
    async def test_human_information_system_query(
        self, mock_task_manager, mock_storage, mock_run_pipeline,
    ):
        """Ask with source='database' and system='dashboard'.

        When source is not 'system', the system parameter is silently ignored
        (per _normalise_system).  The tool must still succeed.
        """
        result = await human_information(
            question="What is the current user count?",
            source="database",
            system="dashboard",
            task_manager=mock_task_manager,
            storage=mock_storage,
        )

        assert result["status"] == "pending"
        call_kwargs = mock_run_pipeline.call_args.kwargs
        # source is always stored in extra_params (and passed through as a kwarg).
        # system is passed through too — the pipeline receives both.
        assert call_kwargs["source"] == "database"
        # Description should include a source label for non-memory sources.
        assert "Source:" in call_kwargs["description"]


# ---------------------------------------------------------------------------
# human_poll
# ---------------------------------------------------------------------------


class TestHumanPoll:
    """Tests for human_poll — check status of one or more tasks."""

    @pytest.mark.asyncio
    async def test_human_poll_single(self, mock_task_manager):
        """Poll a single task_id and receive its dictionary."""
        expected = {
            "task_id": "task-1",
            "title": "Do something",
            "status": "pending",
            "tool_name": "human_action",
            "created_at": "2026-06-02T10:00:00+00:00",
        }
        mock_task_manager.get_task.return_value = expected

        result = await human_poll(task_id="task-1", task_manager=mock_task_manager)

        mock_task_manager.get_task.assert_called_once_with("task-1")
        assert result == expected

    @pytest.mark.asyncio
    async def test_human_poll_batch(self, mock_task_manager):
        """Poll a list of task_ids and receive a summary with all tasks."""
        task_a = {"task_id": "task-a", "title": "A", "status": "pending"}
        task_b = {"task_id": "task-b", "title": "B", "status": "completed"}

        def get_task_side_effect(tid):
            return {"task-a": task_a, "task-b": task_b}.get(tid)

        mock_task_manager.get_task.side_effect = get_task_side_effect

        result = await human_poll(
            task_id=["task-a", "task-b"], task_manager=mock_task_manager,
        )

        assert "tasks" in result
        assert "summary" in result
        assert len(result["tasks"]) == 2
        assert result["tasks"][0] == task_a
        assert result["tasks"][1] == task_b
        assert result["summary"] == {
            "pending": 1, "completed": 1, "rejected": 0, "not_found": 0,
        }
        assert mock_task_manager.get_task.call_count == 2

    @pytest.mark.asyncio
    async def test_human_poll_not_found(self, mock_task_manager):
        """Poll a nonexistent task_id returns an error dict."""
        mock_task_manager.get_task.return_value = None

        result = await human_poll(task_id="bad-id", task_manager=mock_task_manager)

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()
        assert "bad-id" in result["message"]


# ---------------------------------------------------------------------------
# human_list_tasks
# ---------------------------------------------------------------------------


class TestHumanListTasks:
    """Tests for human_list_tasks — list tasks by status."""

    @pytest.mark.asyncio
    async def test_human_list_tasks_pending(self, mock_task_manager):
        """List pending tasks (the default)."""
        mock_task_manager.list_tasks.return_value = [
            {"task_id": "t1", "title": "Task 1", "status": "pending"},
            {"task_id": "t2", "title": "Task 2", "status": "pending"},
        ]

        result = await human_list_tasks(status="pending", task_manager=mock_task_manager)

        assert result["tasks"] == mock_task_manager.list_tasks.return_value
        assert result["total"] == 2
        assert result["status_filter"] == "pending"
        mock_task_manager.list_tasks.assert_called_once_with(
            status="pending", agent_id=None, limit=50,
        )

    @pytest.mark.asyncio
    async def test_human_list_tasks_by_agent(self, mock_task_manager):
        """Filter tasks by agent_id."""
        mock_task_manager.list_tasks.return_value = [
            {"task_id": "t1", "title": "Agent task", "status": "pending", "agent_id": "agent-42"},
        ]

        result = await human_list_tasks(
            status="pending", agent_id="agent-42", task_manager=mock_task_manager,
        )

        assert result["total"] == 1
        mock_task_manager.list_tasks.assert_called_once_with(
            status="pending", agent_id="agent-42", limit=50,
        )

    @pytest.mark.asyncio
    async def test_human_list_tasks_all(self, mock_task_manager):
        """List all tasks regardless of status."""
        mock_task_manager.list_tasks.return_value = [
            {"task_id": "t1", "title": "Pending", "status": "pending"},
            {"task_id": "t2", "title": "Done", "status": "completed"},
            {"task_id": "t3", "title": "Rejected", "status": "rejected"},
        ]

        result = await human_list_tasks(status="all", task_manager=mock_task_manager)

        assert result["total"] == 3
        assert result["status_filter"] == "all"
        mock_task_manager.list_tasks.assert_called_once_with(
            status="all", agent_id=None, limit=50,
        )
