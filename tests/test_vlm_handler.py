"""Tests for BaseVLMHandler with mocked HTTP calls."""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.tasks import TaskRetryableError, TaskPermanentError
from src.tasks.handlers.vlm_handler import BaseVLMHandler, VLMCaptionHandler
from tests.conftest import make_media_item


# ── check_vlm_ready ───────────────────────────────────────────


class TestCheckVLMReady:
    @pytest.mark.asyncio
    async def test_ready_true(self):
        handler = BaseVLMHandler()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ready": True}

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await handler.check_vlm_ready()
        assert result is True

    @pytest.mark.asyncio
    async def test_ready_false(self):
        handler = BaseVLMHandler()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ready": False, "reason": "cooldown_active"}

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await handler.check_vlm_ready()
        assert result is False

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self):
        handler = BaseVLMHandler()

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.get.side_effect = httpx.ConnectError("refused")
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await handler.check_vlm_ready()
        assert result is False


# ── call_vlm_wrapper ──────────────────────────────────────────


class TestCallVLMWrapper:
    @pytest.mark.asyncio
    async def test_success(self, tmp_image):
        handler = BaseVLMHandler()
        handler.task_type = "vlm_caption"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "task_type": "vlm_caption",
            "output": {"caption": "A test image"},
        }

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await handler.call_vlm_wrapper("vlm_caption", tmp_image, {})

        assert result["output"]["caption"] == "A test image"

    @pytest.mark.asyncio
    async def test_503_raises_retryable(self, tmp_image):
        handler = BaseVLMHandler()

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(TaskRetryableError, match="503"):
                await handler.call_vlm_wrapper("vlm_caption", tmp_image, {})

    @pytest.mark.asyncio
    async def test_429_raises_retryable(self, tmp_image):
        handler = BaseVLMHandler()

        mock_response = MagicMock()
        mock_response.status_code = 429

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(TaskRetryableError, match="429"):
                await handler.call_vlm_wrapper("vlm_caption", tmp_image, {})

    @pytest.mark.asyncio
    async def test_connection_error_raises_retryable(self, tmp_image):
        handler = BaseVLMHandler()

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.post.side_effect = httpx.ConnectError("unreachable")
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(TaskRetryableError, match="unreachable"):
                await handler.call_vlm_wrapper("vlm_caption", tmp_image, {})

    @pytest.mark.asyncio
    async def test_timeout_raises_retryable(self, tmp_image):
        handler = BaseVLMHandler()

        with patch("src.tasks.handlers.vlm_handler.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.post.side_effect = httpx.TimeoutException("timeout")
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(TaskRetryableError, match="timeout"):
                await handler.call_vlm_wrapper("vlm_caption", tmp_image, {})


# ── Full execute flow ─────────────────────────────────────────


class TestVLMExecute:
    @pytest.mark.asyncio
    async def test_not_ready_raises_retryable(self, mock_session):
        handler = VLMCaptionHandler()

        with patch.object(handler, "check_vlm_ready", return_value=False):
            with pytest.raises(TaskRetryableError, match="not ready"):
                await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_media_not_found_raises_permanent(self, mock_session):
        handler = VLMCaptionHandler()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with patch.object(handler, "check_vlm_ready", return_value=True):
            with pytest.raises(TaskPermanentError, match="not found"):
                await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_successful_execution(self, mock_session, tmp_image):
        handler = VLMCaptionHandler()
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        vlm_output = {"caption": "A lovely photo", "scene_category": "outdoor"}

        with patch.object(handler, "check_vlm_ready", return_value=True):
            with patch.object(handler, "call_vlm_wrapper", return_value=vlm_output):
                output = await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

        assert output["caption"] == "A lovely photo"
