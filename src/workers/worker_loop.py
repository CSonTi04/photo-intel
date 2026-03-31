"""Worker loop — the core execution engine.

Each worker:
1. Claims tasks from the queue (specific types based on worker role)
2. Executes the task handler
3. Writes output on success
4. Handles failures with retry/DLQ logic
5. Enqueues follow-up tasks if needed

Worker types:
- cpu_worker: exif, thumbnail, ocr (local CPU tasks)
- vlm_worker: VLM tasks (calls GPU wrapper over HTTP)
- digest_worker: digest generation
- maintenance_worker: lease reclamation, promotion
"""

import asyncio
import time
import signal
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.database import async_session
from src.queue.postgres_queue import PostgresQueue
from src.tasks import TaskRetryableError, TaskPermanentError
from src.tasks.registry import registry

logger = structlog.get_logger()


# Task type groupings
WORKER_TASK_TYPES = {
    "cpu": ["extract_exif", "generate_thumbnail", "ocr_full", "ocr_entities"],
    "vlm": ["vlm_caption", "vlm_actionability", "vlm_memory_summary"],
    "digest": ["generate_daily_digest", "generate_resurface_digest"],
}


class Worker:
    """Generic worker that processes tasks from the queue."""

    def __init__(
        self,
        worker_type: str,
        worker_id: Optional[str] = None,
        concurrency: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        self.worker_type = worker_type
        self.worker_id = worker_id or f"{worker_type}-{settings.worker.worker_id}"
        self.task_types = WORKER_TASK_TYPES.get(worker_type, [])
        self.running = True
        self.queue = PostgresQueue(async_session)

        # Set concurrency based on worker type
        if concurrency is not None:
            self.concurrency = concurrency
        elif worker_type == "vlm":
            self.concurrency = settings.worker.vlm_concurrency
        elif worker_type == "cpu":
            self.concurrency = settings.worker.max_concurrent_tasks
        else:
            self.concurrency = 1

        self.batch_size = batch_size or (
            settings.worker.vlm_batch_size if worker_type == "vlm" else self.concurrency
        )

        logger.info(
            "worker.init",
            worker_id=self.worker_id,
            worker_type=self.worker_type,
            task_types=self.task_types,
            concurrency=self.concurrency,
        )

    def handle_shutdown(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT."""
        logger.info("worker.shutdown_requested", worker_id=self.worker_id)
        self.running = False

    async def process_task(self, task_row) -> None:
        """Process a single claimed task."""
        task_id = task_row.id
        task_type = task_row.task_type
        media_item_id = task_row.media_item_id
        input_hash = task_row.input_hash

        handler = registry.get(task_type)
        if not handler:
            logger.error("worker.no_handler", task_type=task_type)
            async with async_session() as session:
                await self.queue.fail_task(
                    session, task_id,
                    error_message=f"No handler registered for {task_type}",
                )
                await session.commit()
            return

        start_time = time.monotonic()
        try:
            # Get task config from task_definition
            from sqlalchemy import select
            from src.models.tables import TaskDefinition

            async with async_session() as session:
                result = await session.execute(
                    select(TaskDefinition.config_json)
                    .where(
                        TaskDefinition.task_type == task_type,
                        TaskDefinition.version == task_row.task_version,
                    )
                )
                config_row = result.fetchone()
                task_config = config_row.config_json if config_row else {}

                # Execute handler
                output = await handler.execute(
                    media_item_id=media_item_id,
                    task_config=task_config,
                    input_hash=input_hash,
                    session=session,
                )

                duration_ms = int((time.monotonic() - start_time) * 1000)

                # Complete task
                await self.queue.complete_task(
                    session=session,
                    task_id=task_id,
                    output_json=output,
                    summary_text=output.get("summary", output.get("text_preview", "")),
                    duration_ms=duration_ms,
                    worker_id=self.worker_id,
                )
                await session.commit()

                logger.info(
                    "worker.task_completed",
                    task_id=str(task_id),
                    task_type=task_type,
                    duration_ms=duration_ms,
                )

        except TaskRetryableError as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning(
                "worker.task_retryable",
                task_id=str(task_id),
                task_type=task_type,
                error=str(e),
                duration_ms=duration_ms,
            )
            async with async_session() as session:
                await self.queue.fail_task(session, task_id, error_message=str(e))
                await session.commit()

        except TaskPermanentError as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "worker.task_permanent_failure",
                task_id=str(task_id),
                task_type=task_type,
                error=str(e),
                duration_ms=duration_ms,
            )
            async with async_session() as session:
                await self.queue.fail_task(
                    session, task_id,
                    error_message=str(e),
                    max_attempts=0,  # Force DLQ
                )
                await session.commit()

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.exception(
                "worker.task_unexpected_error",
                task_id=str(task_id),
                task_type=task_type,
                error=str(e),
                duration_ms=duration_ms,
            )
            async with async_session() as session:
                await self.queue.fail_task(session, task_id, error_message=str(e))
                await session.commit()

    async def run_loop(self) -> None:
        """Main worker loop — claim and process tasks continuously."""
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)

        logger.info("worker.started", worker_id=self.worker_id)

        while self.running:
            try:
                # Promote discovered → pending (check prerequisites)
                async with async_session() as session:
                    await self.queue.promote_discovered_tasks(session)
                    await session.commit()

                # Reclaim expired leases
                async with async_session() as session:
                    await self.queue.reclaim_expired_leases(session)
                    await session.commit()

                # Claim tasks
                async with async_session() as session:
                    tasks = await self.queue.claim_tasks(
                        session=session,
                        worker_id=self.worker_id,
                        task_types=self.task_types,
                        limit=self.batch_size,
                        lease_duration_seconds=settings.worker.lease_duration_seconds,
                    )
                    await session.commit()

                if not tasks:
                    await asyncio.sleep(settings.worker.poll_interval_seconds)
                    continue

                # Process tasks (sequentially for VLM, concurrent for CPU)
                if self.worker_type == "vlm":
                    for task in tasks:
                        if not self.running:
                            break
                        await self.process_task(task)
                else:
                    # Concurrent processing for CPU tasks
                    semaphore = asyncio.Semaphore(self.concurrency)

                    async def bounded_process(task):
                        async with semaphore:
                            await self.process_task(task)

                    await asyncio.gather(
                        *[bounded_process(task) for task in tasks],
                        return_exceptions=True,
                    )

            except Exception as e:
                logger.exception("worker.loop_error", error=str(e))
                await asyncio.sleep(10)  # Back off on unexpected errors

        logger.info("worker.stopped", worker_id=self.worker_id)


async def run_cpu_worker():
    """Entry point for CPU worker."""
    # Import handlers to trigger registration
    import src.tasks.handlers  # noqa
    worker = Worker(worker_type="cpu")
    await worker.run_loop()


async def run_vlm_worker():
    """Entry point for VLM worker."""
    import src.tasks.handlers  # noqa
    worker = Worker(worker_type="vlm")
    await worker.run_loop()


async def run_maintenance_worker():
    """Periodic maintenance: reclaim leases, promote tasks."""
    import src.tasks.handlers  # noqa
    queue = PostgresQueue(async_session)

    while True:
        try:
            async with async_session() as session:
                reclaimed = await queue.reclaim_expired_leases(session)
                promoted = await queue.promote_discovered_tasks(session)
                await session.commit()

                if reclaimed or promoted:
                    logger.info(
                        "maintenance.cycle",
                        reclaimed=reclaimed,
                        promoted=promoted,
                    )
        except Exception as e:
            logger.exception("maintenance.error", error=str(e))

        await asyncio.sleep(30)
