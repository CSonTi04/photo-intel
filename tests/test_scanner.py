"""Tests for the ingest scanner module."""

from pathlib import Path

from PIL import Image

from src.ingest.scanner import (
    classify_media_kind,
    compute_content_hash,
    extract_basic_metadata,
)
from src.models.tables import MediaKind


def create_test_image(path: Path, width: int = 800, height: int = 600, fmt: str = "JPEG"):
    """Create a test image file."""
    img = Image.new("RGB", (width, height), color="red")
    img.save(path, fmt)


class TestContentHash:
    def test_same_content_same_hash(self, tmp_path):
        p1 = tmp_path / "img1.jpg"
        p2 = tmp_path / "img2.jpg"
        create_test_image(p1, 100, 100)
        # Copy file
        p2.write_bytes(p1.read_bytes())
        assert compute_content_hash(p1) == compute_content_hash(p2)

    def test_different_content_different_hash(self, tmp_path):
        p1 = tmp_path / "img1.jpg"
        p2 = tmp_path / "img2.jpg"
        create_test_image(p1, 100, 100)
        create_test_image(p2, 200, 200)
        assert compute_content_hash(p1) != compute_content_hash(p2)


class TestMediaKindClassification:
    def test_screenshot_by_filename(self, tmp_path):
        path = tmp_path / "Screenshot 2024-01-01.png"
        assert classify_media_kind(path, 1920, 1080) == MediaKind.screenshot

    def test_screenshot_by_resolution_1080p(self, tmp_path):
        path = tmp_path / "IMG_1234.jpg"
        assert classify_media_kind(path, 1920, 1080) == MediaKind.screenshot

    def test_screenshot_by_resolution_macbook(self, tmp_path):
        path = tmp_path / "IMG_1234.jpg"
        assert classify_media_kind(path, 3024, 1964) == MediaKind.screenshot

    def test_photo_by_resolution(self, tmp_path):
        path = tmp_path / "IMG_1234.jpg"
        assert classify_media_kind(path, 4032, 3024) == MediaKind.photo

    def test_photo_default(self, tmp_path):
        path = tmp_path / "image.jpg"
        assert classify_media_kind(path, 800, 600) == MediaKind.photo

    def test_hungarian_screenshot_indicator(self, tmp_path):
        path = tmp_path / "képernyőkép_2024.png"
        assert classify_media_kind(path, 800, 600) == MediaKind.screenshot


class TestBasicMetadata:
    def test_extracts_dimensions(self, tmp_path):
        path = tmp_path / "test.jpg"
        create_test_image(path, 1024, 768)
        meta = extract_basic_metadata(path)
        assert meta["width"] == 1024
        assert meta["height"] == 768
        assert meta["mime_type"] == "image/jpeg"
        assert meta["file_size"] > 0

    def test_handles_missing_dimensions(self, tmp_path):
        path = tmp_path / "test.txt"
        path.write_text("not an image")
        meta = extract_basic_metadata(path)
        assert meta["width"] is None
        assert meta["height"] is None
