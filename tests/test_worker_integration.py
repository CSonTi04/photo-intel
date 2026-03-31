"""Integration tests for Worker.process_task with mocked handler and queue."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tasks import TaskPermanentError, TaskRetryableError

# ── Helpers ────────────────────────────────────────────────────


def make_task_row(
    task_type="extract_exif",
    task_id=None,
    media_item_id=None,
    task_version=1,
    input_hash="abc123",
    attempts=0,
    max_attempts=3,
    task_config=None,
):
    """Build a fake task row as returned by claim_tasks."""
    row = MagicMock()
    # It must have .config_json, not just mock dictionary behavior
    row.id = task_id or uuid.uuid4()
    row.media_item_id = media_item_id or uuid.uuid4()
    row.task_type = task_type
    row.task_version = task_version
    row.input_hash = input_hash
    row.attempts = attempts
    row.max_attempts = max_attempts
    row.state = "leased"
    row.config_json = task_config or {}
    return row


class TestProcessTask:
    @pytest.mark.asyncio
    async def test_successful_task(self):
        """Handler executes successfully → complete_task called."""
        from src.workers.worker_loop import Worker

        mock_handler = MagicMock()
        mock_handler.execute = AsyncMock(return_value={"result": "done"})

        mock_queue = AsyncMock()
        mock_queue.complete_task = AsyncMock()

        task_row = make_task_row(task_type="extract_exif")

        with (
            patch("src.workers.worker_loop.registry") as mock_registry,
            patch("src.workers.worker_loop.PostgresQueue", return_value=mock_queue),
            patch("src.workers.worker_loop.async_session") as mock_session_factory,
        ):
            mock_registry.get.return_value = mock_handler

            worker = Worker("cpu")
            worker.queue = mock_queue

            session = AsyncMock()
            # Fix: mock session.execute to return a Result-like object with fetchone()
            result_mock = MagicMock()
            result_mock.fetchone.return_value = make_task_row()  # a row with config_json
            session.execute.return_value = result_mock

            mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await worker.process_task(task_row)

        mock_handler.execute.assert_called_once()
        mock_queue.complete_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_retryable_error_calls_fail(self):
        """Handler raises TaskRetryableError → fail_task called."""
        from src.workers.worker_loop import Worker

        mock_handler = MagicMock()
        mock_handler.execute = AsyncMock(side_effect=TaskRetryableError("transient error"))

        mock_queue = AsyncMock()
        mock_queue.fail_task = AsyncMock()

        task_row = make_task_row(task_type="extract_exif")

        with (
            patch("src.workers.worker_loop.registry") as mock_registry,
            patch("src.workers.worker_loop.PostgresQueue", return_value=mock_queue),
            patch("src.workers.worker_loop.async_session") as mock_session_factory,
        ):
            mock_registry.get.return_value = mock_handler

            worker = Worker("cpu")
            worker.queue = mock_queue

            session = AsyncMock()
            result_mock = MagicMock()
            result_mock.fetchone.return_value = make_task_row()
            session.execute.return_value = result_mock

            mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await worker.process_task(task_row)

        mock_queue.fail_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_permanent_error_calls_fail(self):
        """Handler raises TaskPermanentError → fail_task called."""
        from src.workers.worker_loop import Worker

        mock_handler = MagicMock()
        mock_handler.execute = AsyncMock(side_effect=TaskPermanentError("fatal error"))

        mock_queue = AsyncMock()
        mock_queue.fail_task = AsyncMock()

        task_row = make_task_row(task_type="extract_exif")

        with (
            patch("src.workers.worker_loop.registry") as mock_registry,
            patch("src.workers.worker_loop.PostgresQueue", return_value=mock_queue),
            patch("src.workers.worker_loop.async_session") as mock_session_factory,
        ):
            mock_registry.get.return_value = mock_handler

            worker = Worker("cpu")
            worker.queue = mock_queue

            session = AsyncMock()
            result_mock = MagicMock()
            result_mock.fetchone.return_value = make_task_row()
            session.execute.return_value = result_mock

            mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await worker.process_task(task_row)

        mock_queue.fail_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handler_not_found_fails_task(self):
        """When no handler is registered for the task type, the task should fail."""
        from src.workers.worker_loop import Worker

        mock_queue = AsyncMock()
        mock_queue.fail_task = AsyncMock()

        task_row = make_task_row(task_type="nonexistent_task")

        with (
            patch("src.workers.worker_loop.registry") as mock_registry,
            patch("src.workers.worker_loop.PostgresQueue", return_value=mock_queue),
            patch("src.workers.worker_loop.async_session") as mock_session_factory,
        ):
            mock_registry.get.return_value = None

            worker = Worker("cpu")
            worker.queue = mock_queue

            session = AsyncMock()
            result_mock = MagicMock()
            result_mock.fetchone.return_value = make_task_row()
            session.execute.return_value = result_mock

            mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await worker.process_task(task_row)

        mock_queue.fail_task.assert_called_once()
