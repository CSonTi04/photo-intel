"""Tests for GenerateThumbnailHandler."""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.tasks.handlers.thumbnail_handler import GenerateThumbnailHandler
from src.tasks import TaskRetryableError
from tests.conftest import make_media_item


handler = GenerateThumbnailHandler()


class TestComputeInputHash:
    def test_deterministic(self):
        mid = uuid.uuid4()
        h1 = handler.compute_input_hash(mid, {"sizes": [256]})
        h2 = handler.compute_input_hash(mid, {"sizes": [256]})
        assert h1 == h2

    def test_different_config_different_hash(self):
        mid = uuid.uuid4()
        h1 = handler.compute_input_hash(mid, {"sizes": [256]})
        h2 = handler.compute_input_hash(mid, {"sizes": [512]})
        assert h1 != h2


class TestExecute:
    @pytest.mark.asyncio
    async def test_media_item_not_found(self, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_session):
        item = make_media_item(file_path="/nonexistent/image.jpg")
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="File not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_generates_thumbnails(self, mock_session, tmp_image, tmp_path):
        """Should create actual thumbnail files on disk with correct sizes."""
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        # Patch settings to use tmp_path for output
        mock_settings = MagicMock()
        mock_settings.thumbnail.sizes = [128, 256]
        mock_settings.thumbnail.format = "webp"
        mock_settings.thumbnail.quality = 80
        mock_settings.thumbnail.output_dir = str(tmp_path / "thumbs")

        config = {
            "sizes": [128, 256],
            "format": "webp",
            "quality": 80,
        }

        with patch("src.tasks.handlers.thumbnail_handler.settings", mock_settings):
            output = await handler.execute(uuid.uuid4(), config, "hash", session=mock_session)

        assert output["count"] == 2
        assert output["format"] == "webp"
        assert len(output["thumbnails"]) == 2

        # Verify files were actually created
        for thumb_info in output["thumbnails"]:
            thumb_path = Path(thumb_info["path"])
            assert thumb_path.exists()
            with Image.open(thumb_path) as img:
                assert max(img.size) <= thumb_info["size"]

    @pytest.mark.asyncio
    async def test_jpeg_format(self, mock_session, tmp_image, tmp_path):
        """Should handle JPEG output format (requires RGB conversion)."""
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        mock_settings = MagicMock()
        mock_settings.thumbnail.sizes = [128]
        mock_settings.thumbnail.format = "jpeg"
        mock_settings.thumbnail.quality = 85
        mock_settings.thumbnail.output_dir = str(tmp_path / "thumbs")

        config = {"sizes": [128], "format": "jpeg", "quality": 85}

        with patch("src.tasks.handlers.thumbnail_handler.settings", mock_settings):
            output = await handler.execute(uuid.uuid4(), config, "hash", session=mock_session)

        assert output["count"] == 1
        assert output["format"] == "jpeg"
        thumb_path = Path(output["thumbnails"][0]["path"])
        assert thumb_path.exists()
