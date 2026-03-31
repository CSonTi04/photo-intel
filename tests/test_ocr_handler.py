"""Tests for run_tesseract and OCRFullHandler."""

import uuid
from unittest.mock import MagicMock, patch

import pytesseract
import pytest

from src.tasks import TaskRetryableError
from src.tasks.handlers.ocr_handler import OCRFullHandler, run_tesseract
from tests.conftest import make_media_item

# ── run_tesseract (mocked pytesseract) ─────────────────────────


class TestRunTesseract:
    MOCK_DATA = {
        "text": ["Hello", "World", "", "Test", ""],
        "conf": [95, 88, -1, 72, -1],
        "block_num": [1, 1, 1, 2, 2],
        "left": [10, 60, 0, 10, 0],
        "top": [10, 10, 0, 50, 0],
        "width": [50, 50, 0, 40, 0],
        "height": [20, 20, 0, 20, 0],
    }

    @patch("pytesseract.image_to_data")
    def test_returns_full_text(self, mock_image_to_data, tmp_image):
        pytesseract.Output.DICT = "dict"
        mock_image_to_data.return_value = self.MOCK_DATA

        result = run_tesseract(tmp_image)

        assert "Hello" in result["full_text"]
        assert "World" in result["full_text"]
        assert "Test" in result["full_text"]

    @patch("pytesseract.image_to_data")
    def test_word_count(self, mock_image_to_data, tmp_image):
        pytesseract.Output.DICT = "dict"
        mock_image_to_data.return_value = self.MOCK_DATA

        result = run_tesseract(tmp_image)
        assert result["word_count"] == 3

    @patch("pytesseract.image_to_data")
    def test_confidence_average(self, mock_image_to_data, tmp_image):
        pytesseract.Output.DICT = "dict"
        mock_image_to_data.return_value = self.MOCK_DATA

        result = run_tesseract(tmp_image)
        # Confidences: 95, 88, 72 → average = 85.0
        assert result["confidence"] == 85.0

    @patch("pytesseract.image_to_data")
    def test_block_grouping(self, mock_image_to_data, tmp_image):
        pytesseract.Output.DICT = "dict"
        mock_image_to_data.return_value = self.MOCK_DATA

        result = run_tesseract(tmp_image)
        assert len(result["blocks"]) == 2  # Two distinct blocks

    @patch("pytesseract.image_to_data")
    def test_empty_text(self, mock_image_to_data, tmp_image):
        pytesseract.Output.DICT = "dict"
        mock_image_to_data.return_value = {
            "text": ["", ""], "conf": [-1, -1],
            "block_num": [0, 0], "left": [0, 0], "top": [0, 0],
            "width": [0, 0], "height": [0, 0],
        }

        result = run_tesseract(tmp_image)
        assert result["word_count"] == 0
        assert result["confidence"] == 0


# ── OCRFullHandler ─────────────────────────────────────────────


handler = OCRFullHandler()


class TestOCRFullHandler:
    @pytest.mark.asyncio
    async def test_media_not_found(self, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_session):
        item = make_media_item(file_path="/nonexistent/file.jpg")
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        with pytest.raises(TaskRetryableError, match="File not found"):
            await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

    @pytest.mark.asyncio
    async def test_successful_ocr(self, mock_session, tmp_image):
        item = make_media_item(file_path=str(tmp_image))
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = item
        mock_session.execute.return_value = result_mock

        fake_ocr_result = {
            "full_text": "Hello World from OCR",
            "blocks": [{"block_num": 1, "text": "Hello World from OCR"}],
            "confidence": 91.5,
            "word_count": 4,
        }

        with patch("src.tasks.handlers.ocr_handler.run_tesseract", return_value=fake_ocr_result):
            with patch("pytesseract.get_tesseract_version") as mock_tess:
                mock_tess.return_value = MagicMock(public="5.3.0")
                output = await handler.execute(uuid.uuid4(), {}, "hash", session=mock_session)

        assert output["word_count"] == 4
        assert output["confidence"] == 91.5
        assert "Hello World" in output["text_preview"]
        assert output["block_count"] == 1
