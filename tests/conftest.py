"""Shared pytest fixtures for the photo-intel test suite."""

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image


# ── Image fixtures ─────────────────────────────────────────────


@pytest.fixture
def tmp_image(tmp_path: Path) -> Path:
    """Create a real JPEG image on disk."""
    path = tmp_path / "test_photo.jpg"
    img = Image.new("RGB", (800, 600), color="blue")
    img.save(path, "JPEG")
    return path


@pytest.fixture
def tmp_png_image(tmp_path: Path) -> Path:
    """Create a real PNG image on disk (screenshot-style)."""
    path = tmp_path / "Screenshot 2024-01-01.png"
    img = Image.new("RGB", (1920, 1080), color="white")
    img.save(path, "PNG")
    return path


# ── Mock DB session ────────────────────────────────────────────


@pytest.fixture
def mock_session():
    """AsyncMock of an SQLAlchemy AsyncSession."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ── Fake ORM objects ───────────────────────────────────────────


def make_media_item(
    file_path: str = "/fake/image.jpg",
    media_kind_value: str = "photo",
    item_id: uuid.UUID | None = None,
    width: int = 800,
    height: int = 600,
):
    """Build a fake MediaItem-like object with the given attributes."""
    item = MagicMock()
    item.id = item_id or uuid.uuid4()
    item.file_path = file_path
    item.content_hash = "abc123"
    item.source = "filesystem"
    item.captured_at = datetime(2024, 3, 15, 10, 0, 0)
    item.created_at = datetime(2024, 3, 15, 10, 0, 0)
    item.mime_type = "image/jpeg"
    item.width = width
    item.height = height
    item.file_size = 12345

    # media_kind is an enum-like object
    kind = MagicMock()
    kind.value = media_kind_value
    item.media_kind = kind

    return item


@pytest.fixture
def mock_media_item(tmp_image: Path):
    """Returns a fake MediaItem pointing at the real temp JPEG."""
    return make_media_item(file_path=str(tmp_image))
