"""Task Planner — determines which tasks apply to each media item.

The planner:
1. Reads enabled task_definitions from DB
2. For each new media_item, evaluates which tasks apply
3. Creates task_instance records in 'discovered' state
4. Respects media_kind filters (photo vs screenshot)
5. Handles task versioning
"""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tables import MediaItem, TaskDefinition, TaskInstance, TaskState
from src.queue.postgres_queue import PostgresQueue
from src.tasks.registry import registry

logger = structlog.get_logger()


class TaskPlanner:
    """Plans tasks for media items based on task definitions."""

    def __init__(self, queue: PostgresQueue):
        self.queue = queue

    async def plan_for_media_item(
        self,
        session: AsyncSession,
        media_item_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Create task instances for a media item based on enabled definitions.

        Returns list of created task_instance IDs.
        """
        # Get media item
        result = await session.execute(select(MediaItem).where(MediaItem.id == media_item_id))
        media = result.scalar_one_or_none()
        if not media:
            logger.warning("planner.media_not_found", media_item_id=str(media_item_id))
            return []

        # Get enabled task definitions
        result = await session.execute(select(TaskDefinition).where(TaskDefinition.enabled))
        definitions = result.scalars().all()

        created = []
        for td in definitions:
            # Check if task applies to this media_kind
            applies_to = td.applies_to or ["photo", "screenshot"]
            if media.media_kind.value not in applies_to:
                logger.debug(
                    "planner.skipped_kind",
                    task_type=td.task_type,
                    media_kind=media.media_kind.value,
                )
                continue

            # Check if handler is registered
            handler = registry.get(td.task_type)
            if not handler:
                logger.warning("planner.no_handler", task_type=td.task_type)
                continue

            # Compute input hash for idempotency
            input_hash = handler.compute_input_hash(media_item_id, td.config_json or {})

            # Determine initial state based on prerequisites
            has_prerequisites = td.prerequisites and len(td.prerequisites) > 0

            # Create task instance via queue (handles idempotency)
            task_id = await self.queue.enqueue(
                session=session,
                media_item_id=media_item_id,
                task_type=td.task_type,
                task_version=td.version,
                input_hash=input_hash,
                priority=td.priority,
                max_attempts=td.max_retries,
            )

            if task_id:
                # If it has prerequisites, set to discovered
                if has_prerequisites:
                    from sqlalchemy import update

                    await session.execute(
                        update(TaskInstance).where(TaskInstance.id == task_id).values(state=TaskState.discovered)
                    )
                created.append(task_id)

        logger.info(
            "planner.planned",
            media_item_id=str(media_item_id),
            tasks_created=len(created),
            media_kind=media.media_kind.value,
        )

        return created

    async def plan_batch(
        self,
        session: AsyncSession,
        media_item_ids: list[uuid.UUID],
    ) -> dict:
        """Plan tasks for a batch of media items."""
        stats = {"total": len(media_item_ids), "tasks_created": 0}
        for mid in media_item_ids:
            created = await self.plan_for_media_item(session, mid)
            stats["tasks_created"] += len(created)
        return stats
