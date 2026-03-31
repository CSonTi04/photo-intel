"""VLM (Vision Language Model) task handlers.

These handlers communicate with the VLM Wrapper Service running on the GPU node.
They NEVER call Ollama directly — always go through the wrapper.
"""

import hashlib
import json
import uuid
from pathlib import Path

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.tables import MediaItem
from src.tasks import TaskHandler, TaskRetryableError, TaskPermanentError
from src.tasks.registry import register_task

logger = structlog.get_logger()


class BaseVLMHandler:
    """Base class for VLM task handlers.

    Handles:
    - Readiness checking
    - Image sending
    - Response validation
    - Error classification
    """

    task_type: str = ""

    def compute_input_hash(self, media_item_id: uuid.UUID, config: dict) -> str:
        raw = f"{media_item_id}:{self.task_type}:{json.dumps(config, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def check_vlm_ready(self) -> bool:
        """Check if VLM Wrapper is ready to accept tasks."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{settings.vlm.base_url}/ready-for-vlm")
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("ready", False)
                return False
        except Exception as e:
            logger.warning("vlm.readiness_check_failed", error=str(e))
            return False

    async def call_vlm_wrapper(
        self,
        task_type: str,
        image_path: Path,
        config: dict,
    ) -> dict:
        """Send task to VLM Wrapper Service."""
        url = f"{settings.vlm.base_url}/run-task/{task_type}"

        try:
            async with httpx.AsyncClient(timeout=settings.vlm.timeout_seconds) as client:
                with open(image_path, "rb") as f:
                    files = {"image": (image_path.name, f, "image/jpeg")}
                    data = {"config": json.dumps(config)}
                    resp = await client.post(url, files=files, data=data)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 503:
                    raise TaskRetryableError("VLM Wrapper not ready (503)")
                elif resp.status_code == 429:
                    raise TaskRetryableError("VLM Wrapper rate limited (429)")
                else:
                    raise TaskRetryableError(
                        f"VLM Wrapper error: {resp.status_code} - {resp.text[:500]}"
                    )

        except httpx.ConnectError as e:
            raise TaskRetryableError(f"VLM Wrapper unreachable: {e}")
        except httpx.TimeoutException as e:
            raise TaskRetryableError(f"VLM Wrapper timeout: {e}")

    async def execute(
        self,
        media_item_id: uuid.UUID,
        task_config: dict,
        input_hash: str,
        session: AsyncSession = None,
    ) -> dict:
        # Check readiness first
        if not await self.check_vlm_ready():
            raise TaskRetryableError("VLM Wrapper not ready")

        # Get media item
        result = await session.execute(
            select(MediaItem).where(MediaItem.id == media_item_id)
        )
        media = result.scalar_one_or_none()
        if not media:
            raise TaskPermanentError(f"MediaItem {media_item_id} not found")

        file_path = Path(media.file_path)
        if not file_path.exists():
            raise TaskRetryableError(f"File not found: {file_path}")

        # Call wrapper
        vlm_result = await self.call_vlm_wrapper(
            task_type=self.task_type,
            image_path=file_path,
            config=task_config,
        )

        logger.info(
            f"vlm.{self.task_type}.completed",
            media_item_id=str(media_item_id),
        )

        return vlm_result


@register_task
class VLMCaptionHandler(BaseVLMHandler):
    """Generate image captions and scene classification."""
    task_type = "vlm_caption"


@register_task
class VLMActionabilityHandler(BaseVLMHandler):
    """Detect actionable information in screenshots."""
    task_type = "vlm_actionability"


@register_task
class VLMMemorySummaryHandler(BaseVLMHandler):
    """Generate memory-worthy summaries for personal knowledge base."""
    task_type = "vlm_memory_summary"
