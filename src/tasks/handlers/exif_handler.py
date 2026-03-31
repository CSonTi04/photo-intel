"""EXIF extraction task handler."""

import hashlib
import json
import uuid
from pathlib import Path

import exifread
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tables import MediaItem, MediaExif
from src.tasks import TaskRetryableError
from src.tasks.registry import register_task

logger = structlog.get_logger()


@register_task
class ExtractExifHandler:
    task_type = "extract_exif"

    def compute_input_hash(self, media_item_id: uuid.UUID, config: dict) -> str:
        raw = f"{media_item_id}:{json.dumps(config, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def execute(
        self,
        media_item_id: uuid.UUID,
        task_config: dict,
        input_hash: str,
        session: AsyncSession = None,
    ) -> dict:
        # Fetch media item
        result = await session.execute(
            select(MediaItem).where(MediaItem.id == media_item_id)
        )
        media = result.scalar_one_or_none()
        if not media:
            raise TaskRetryableError(f"MediaItem {media_item_id} not found")

        file_path = Path(media.file_path)
        if not file_path.exists():
            raise TaskRetryableError(f"File not found: {file_path}")

        # Extract EXIF
        exif_data = {}
        try:
            with open(file_path, "rb") as f:
                tags = exifread.process_file(f, details=False)
                for key, value in tags.items():
                    exif_data[key] = str(value)
        except Exception as e:
            logger.warning("exif.extraction_failed", error=str(e), path=str(file_path))

        # Store in media_exif
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(MediaExif).values(
            id=uuid.uuid4(),
            media_item_id=media_item_id,
            exif_json=exif_data,
        ).on_conflict_do_update(
            constraint="uq_media_exif_item",
            set_={"exif_json": exif_data},
        )
        await session.execute(stmt)

        # Build output
        output = {
            "tag_count": len(exif_data),
            "has_gps": any("GPS" in k for k in exif_data),
            "has_camera": any(k.startswith("Image Make") or k.startswith("Image Model") for k in exif_data),
            "camera_make": exif_data.get("Image Make", ""),
            "camera_model": exif_data.get("Image Model", ""),
            "datetime_original": exif_data.get("EXIF DateTimeOriginal", ""),
        }

        logger.info(
            "exif.extracted",
            media_item_id=str(media_item_id),
            tag_count=output["tag_count"],
            has_gps=output["has_gps"],
        )

        return output
