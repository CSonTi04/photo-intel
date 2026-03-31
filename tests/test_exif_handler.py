"""Tests for ExtractExifHandler."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tasks.handlers.exif_handler import ExtractExifHandler
from src.tasks import TaskRetryableError
from tests.conftest import make_media_item


handler = ExtractExifHandler()


class TestComputeInputHash:
    def test_deterministic(self):
        mid = uuid.uuid4()
        h1 = handler.compute_input_hash(mid, {"key": "val"})
        h2 = handler.compute_input_hash(mid, {"key": "val"})
        assert h1 == h2

    def test_different_config_different_hash(self):
        mid = uuid.uuid4()
        h1 = handler.compute_input_hash(mid, {"a": 1})
        h2 = handler.compute_input_hash(mid, {"b": 2})
        assert h1 != h2

    def test_different_media_id_different_hash(self):
        h1 = handler.compute_input_hash(uuid.uuid4(), {})
        h2 = handler.compute_input_hash(uuid.uuid4(), {})
        assert h1 != h2


class TestExecute:
    @pytest.mark.asyncio
    async def test_media_item_not_found(self, mock_session):
        """Should raise TaskRetryableError when media item is missing."""
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_session):
        """Should raise TaskRetryableError when file doesn't exist on disk."""
        item = make_media_item(file_path="/nonexistent/image.jpg")
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="File not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_successful_extraction(self, mock_session, tmp_image):
        """Should extract EXIF tags and return structured output."""
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        fake_tags = {
            "Image Make": MagicMock(__str__=lambda s: "Canon"),
            "Image Model": MagicMock(__str__=lambda s: "EOS R5"),
            "EXIF DateTimeOriginal": MagicMock(__str__=lambda s: "2024:03:15 10:00:00"),
            "GPS GPSLatitude": MagicMock(__str__=lambda s: "[47, 30, 0]"),
        }

        with patch("src.tasks.handlers.exif_handler.exifread.process_file", return_value=fake_tags):
            output = await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

        assert output["tag_count"] == 4
        assert output["has_gps"] is True
        assert output["has_camera"] is True
        assert output["camera_make"] == "Canon"

    @pytest.mark.asyncio
    async def test_no_exif_data(self, mock_session, tmp_image):
        """Should handle images with no EXIF gracefully."""
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        with patch("src.tasks.handlers.exif_handler.exifread.process_file", return_value={}):
            output = await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

        assert output["tag_count"] == 0
        assert output["has_gps"] is False
        assert output["has_camera"] is False
