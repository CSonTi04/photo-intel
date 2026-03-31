"""Filesystem scanner — discovers new images and registers them as media_items.

Supports:
- Batch scanning (backfill mode)
- Incremental polling
- Content hash deduplication
- Basic metadata extraction (no heavy processing)
"""

import hashlib
import mimetypes
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import structlog
from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.tables import MediaItem, MediaKind

logger = structlog.get_logger()

# Heuristics for screenshot detection
SCREENSHOT_INDICATORS = {
    "screenshot", "képernyőkép", "screen shot", "snip", "capture",
    "bildschirmfoto", "schermafbeelding",
}


def compute_content_hash(file_path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA-256 hash of file content."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()


def classify_media_kind(file_path: Path, width: int, height: int) -> MediaKind:
    """Heuristic classification: photo vs screenshot.

    Rules:
    - Filename contains screenshot indicators → screenshot
    - Exact standard screen resolutions → screenshot
    - Aspect ratio is very wide or has specific patterns → screenshot
    - Otherwise → photo
    """
    name_lower = file_path.name.lower()
    for indicator in SCREENSHOT_INDICATORS:
        if indicator in name_lower:
            return MediaKind.screenshot

    # Common screen resolutions
    screen_sizes = {
        (1920, 1080), (2560, 1440), (3840, 2160), (1440, 900),
        (2880, 1800), (1366, 768), (1536, 864), (2560, 1600),
        (3024, 1964), (2880, 1864),  # MacBook resolutions
        (1170, 2532), (1284, 2778), (1179, 2556),  # iPhone
        (1080, 2400), (1440, 3200),  # Android
    }
    if (width, height) in screen_sizes or (height, width) in screen_sizes:
        return MediaKind.screenshot

    return MediaKind.photo


def extract_basic_metadata(file_path: Path) -> dict:
    """Extract basic metadata without heavy processing."""
    stat = file_path.stat()
    mime_type, _ = mimetypes.guess_type(str(file_path))

    result = {
        "file_size": stat.st_size,
        "mime_type": mime_type or "application/octet-stream",
        "width": None,
        "height": None,
        "captured_at": datetime.fromtimestamp(stat.st_mtime),
    }

    try:
        with Image.open(file_path) as img:
            result["width"] = img.width
            result["height"] = img.height
    except Exception as e:
        logger.warning("metadata.image_open_failed", path=str(file_path), error=str(e))

    return result


async def scan_directory(
    directory: Path,
    extensions: set[str],
) -> AsyncGenerator[Path, None]:
    """Yield image files from directory recursively."""
    for root, dirs, files in os.walk(directory):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for filename in sorted(files):
            if Path(filename).suffix.lower() in extensions:
                yield Path(root) / filename


async def register_media_item(
    session: AsyncSession,
    file_path: Path,
) -> uuid.UUID | None:
    """Register a single image file. Returns media_item_id or None if duplicate."""
    content_hash = compute_content_hash(file_path)

    # Check if already exists
    existing = await session.execute(
        select(MediaItem.id).where(MediaItem.content_hash == content_hash)
    )
    if existing.scalar():
        logger.debug("ingest.duplicate", path=str(file_path), hash=content_hash[:12])
        return None

    metadata = extract_basic_metadata(file_path)
    media_kind = classify_media_kind(
        file_path,
        metadata.get("width") or 0,
        metadata.get("height") or 0,
    )

    item_id = uuid.uuid4()
    stmt = pg_insert(MediaItem).values(
        id=item_id,
        content_hash=content_hash,
        file_path=str(file_path),
        source="filesystem",
        captured_at=metadata["captured_at"],
        mime_type=metadata["mime_type"],
        width=metadata["width"],
        height=metadata["height"],
        file_size=metadata["file_size"],
        media_kind=media_kind,
    ).on_conflict_do_nothing(constraint="uq_media_content_hash")

    result = await session.execute(stmt)
    if result.rowcount > 0:
        logger.info(
            "ingest.registered",
            media_item_id=str(item_id),
            path=str(file_path),
            kind=media_kind.value,
            size=metadata["file_size"],
        )
        return item_id
    return None


async def run_batch_scan(
    session: AsyncSession,
    directories: list[str],
    batch_size: int = 500,
) -> dict:
    """Run batch scan across directories. Returns stats."""
    stats = {"scanned": 0, "registered": 0, "duplicates": 0, "errors": 0}

    for dir_path in directories:
        directory = Path(dir_path)
        if not directory.exists():
            logger.warning("ingest.dir_not_found", path=str(directory))
            continue

        batch = []
        async for file_path in scan_directory(directory, settings.ingest.supported_extensions):
            stats["scanned"] += 1
            try:
                item_id = await register_media_item(session, file_path)
                if item_id:
                    stats["registered"] += 1
                    batch.append(item_id)
                else:
                    stats["duplicates"] += 1

                if len(batch) >= batch_size:
                    await session.commit()
                    logger.info("ingest.batch_committed", count=len(batch))
                    batch = []

            except Exception as e:
                stats["errors"] += 1
                logger.error("ingest.error", path=str(file_path), error=str(e))

        # Commit remaining
        if batch:
            await session.commit()
            logger.info("ingest.batch_committed", count=len(batch))

    return stats
