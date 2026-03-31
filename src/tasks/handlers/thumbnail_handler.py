"""Thumbnail generation task handler."""

import hashlib
import json
import uuid
from pathlib import Path

import structlog
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.tables import MediaItem
from src.tasks import TaskRetryableError
from src.tasks.registry import register_task

logger = structlog.get_logger()


@register_task
class GenerateThumbnailHandler:
    task_type = "generate_thumbnail"

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
        result = await session.execute(
            select(MediaItem).where(MediaItem.id == media_item_id)
        )
        media = result.scalar_one_or_none()
        if not media:
            raise TaskRetryableError(f"MediaItem {media_item_id} not found")

        file_path = Path(media.file_path)
        if not file_path.exists():
            raise TaskRetryableError(f"File not found: {file_path}")

        sizes = task_config.get("sizes", settings.thumbnail.sizes)
        fmt = task_config.get("format", settings.thumbnail.format)
        quality = task_config.get("quality", settings.thumbnail.quality)
        output_dir = Path(settings.thumbnail.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        generated = []
        with Image.open(file_path) as img:
            # Preserve EXIF orientation
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)

            for size in sizes:
                thumb = img.copy()
                thumb.thumbnail((size, size), Image.Resampling.LANCZOS)

                thumb_filename = f"{media_item_id}_{size}.{fmt}"
                thumb_path = output_dir / thumb_filename

                if fmt == "webp":
                    thumb.save(thumb_path, "WEBP", quality=quality)
                elif fmt == "jpeg":
                    thumb = thumb.convert("RGB")
                    thumb.save(thumb_path, "JPEG", quality=quality)
                else:
                    thumb.save(thumb_path, fmt.upper())

                generated.append({
                    "size": size,
                    "path": str(thumb_path),
                    "width": thumb.width,
                    "height": thumb.height,
                    "format": fmt,
                })

                logger.debug("thumbnail.generated", size=size, path=str(thumb_path))

        output = {
            "thumbnails": generated,
            "count": len(generated),
            "format": fmt,
            "quality": quality,
        }

        logger.info(
            "thumbnail.complete",
            media_item_id=str(media_item_id),
            count=len(generated),
        )

        return output
