"""Tests for TaskPlanner with mocked session and queue."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tasks.planner import TaskPlanner
from tests.conftest import make_media_item


def make_task_definition(
    task_type="extract_exif",
    version=1,
    enabled=True,
    applies_to=None,
    prerequisites=None,
    priority=100,
    max_retries=3,
    config_json=None,
):
    td = MagicMock()
    td.task_type = task_type
    td.version = version
    td.enabled = enabled
    td.applies_to = applies_to or ["photo", "screenshot"]
    td.prerequisites = prerequisites or []
    td.priority = priority
    td.max_retries = max_retries
    td.config_json = config_json or {}
    return td


class TestPlanForMediaItem:
    @pytest.mark.asyncio
    async def test_creates_tasks_for_all_definitions(self):
        mock_queue = AsyncMock()
        mock_queue.enqueue.return_value = uuid.uuid4()
        planner = TaskPlanner(mock_queue)

        media = make_media_item(media_kind_value="photo")
        defs = [
            make_task_definition(task_type="extract_exif"),
            make_task_definition(task_type="generate_thumbnail"),
        ]

        session = AsyncMock()
        # First call: select MediaItem
        media_result = MagicMock()
        media_result.scalar_one_or_none.return_value = media
        # Second call: select TaskDefinitions
        defs_result = MagicMock()
        defs_result.scalars.return_value.all.return_value = defs

        session.execute.side_effect = [media_result, defs_result, AsyncMock(), AsyncMock()]

        created = await planner.plan_for_media_item(session, media.id)
        assert len(created) == 2

    @pytest.mark.asyncio
    async def test_filters_by_media_kind(self):
        """Photo-only task should be skipped for a screenshot."""
        mock_queue = AsyncMock()
        mock_queue.enqueue.return_value = uuid.uuid4()
        planner = TaskPlanner(mock_queue)

        media = make_media_item(media_kind_value="screenshot")
        defs = [
            make_task_definition(task_type="extract_exif", applies_to=["photo"]),
        ]

        session = AsyncMock()
        media_result = MagicMock()
        media_result.scalar_one_or_none.return_value = media
        defs_result = MagicMock()
        defs_result.scalars.return_value.all.return_value = defs
        session.execute.side_effect = [media_result, defs_result]

        created = await planner.plan_for_media_item(session, media.id)
        assert len(created) == 0

    @pytest.mark.asyncio
    async def test_media_not_found_returns_empty(self):
        mock_queue = AsyncMock()
        planner = TaskPlanner(mock_queue)

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        created = await planner.plan_for_media_item(session, uuid.uuid4())
        assert created == []

    @pytest.mark.asyncio
    async def test_skips_unregistered_handler(self):
        """Tasks whose handler isn't in the registry should be skipped."""
        mock_queue = AsyncMock()
        planner = TaskPlanner(mock_queue)

        media = make_media_item(media_kind_value="photo")
        defs = [
            make_task_definition(task_type="unknown_task_type_xyz"),
        ]

        session = AsyncMock()
        media_result = MagicMock()
        media_result.scalar_one_or_none.return_value = media
        defs_result = MagicMock()
        defs_result.scalars.return_value.all.return_value = defs
        session.execute.side_effect = [media_result, defs_result]

        created = await planner.plan_for_media_item(session, media.id)
        assert len(created) == 0


class TestPlanBatch:
    @pytest.mark.asyncio
    async def test_delegates_to_plan_for_media_item(self):
        mock_queue = AsyncMock()
        planner = TaskPlanner(mock_queue)
        ids = [uuid.uuid4(), uuid.uuid4()]

        with patch.object(planner, "plan_for_media_item", return_value=[uuid.uuid4()]) as mock_plan:
            stats = await planner.plan_batch(AsyncMock(), ids)

        assert mock_plan.call_count == 2
        assert stats["tasks_created"] == 2
        assert stats["total"] == 2
