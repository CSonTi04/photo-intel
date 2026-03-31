"""Postgres-backed task queue using SELECT ... FOR UPDATE SKIP LOCKED.

This is the heart of the orchestration layer. It provides:
- Atomic task claiming (lease-based)
- Exponential backoff retries
- Dead letter queue promotion
- Scheduling via available_at
- Idempotent task creation
"""

import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tables import DeadLetterTask, ProcessingMetric, TaskInstance, TaskOutput, TaskState

logger = structlog.get_logger()


class PostgresQueue:
    """Task queue backed by Postgres with advisory locking."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def enqueue(
        self,
        session: AsyncSession,
        media_item_id: uuid.UUID,
        task_type: str,
        task_version: int,
        input_hash: str,
        priority: int = 100,
        available_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> TaskInstance | None:
        """Enqueue a task. Uses INSERT ... ON CONFLICT DO NOTHING for idempotency."""
        stmt = pg_insert(TaskInstance).values(
            id=uuid.uuid4(),
            media_item_id=media_item_id,
            task_type=task_type,
            task_version=task_version,
            state=TaskState.pending,
            priority=priority,
            available_at=available_at or datetime.utcnow(),
            attempts=0,
            max_attempts=max_attempts,
            input_hash=input_hash,
        ).on_conflict_do_nothing(
            constraint="uq_task_idempotency"
        ).returning(TaskInstance.id)

        result = await session.execute(stmt)
        row = result.fetchone()
        if row:
            logger.info("task.enqueued", task_type=task_type, media_item_id=str(media_item_id))
            return row[0]
        else:
            logger.debug("task.already_exists", task_type=task_type, media_item_id=str(media_item_id))
            return None

    async def claim_tasks(
        self,
        session: AsyncSession,
        worker_id: str,
        task_types: list[str],
        limit: int = 1,
        lease_duration_seconds: int = 300,
    ) -> list[TaskInstance]:
        """Claim pending tasks using FOR UPDATE SKIP LOCKED.

        This ensures no two workers can claim the same task.
        """
        now = datetime.utcnow()
        lease_until = now + timedelta(seconds=lease_duration_seconds)

        # Raw SQL for FOR UPDATE SKIP LOCKED (SQLAlchemy ORM doesn't support SKIP LOCKED natively)
        claim_sql = text("""
            UPDATE task_instance
            SET state = 'leased',
                leased_by = :worker_id,
                lease_until = :lease_until,
                attempts = attempts + 1,
                updated_at = NOW()
            WHERE id IN (
                SELECT id FROM task_instance
                WHERE state = 'pending'
                  AND task_type = ANY(:task_types)
                  AND available_at <= :now
                ORDER BY priority ASC, available_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            RETURNING id, media_item_id, task_type, task_version, attempts, input_hash
        """)

        result = await session.execute(claim_sql, {
            "worker_id": worker_id,
            "lease_until": lease_until,
            "task_types": task_types,
            "now": now,
            "limit": limit,
        })

        rows = result.fetchall()
        tasks = []
        for row in rows:
            logger.info(
                "task.claimed",
                task_id=str(row.id),
                task_type=row.task_type,
                worker_id=worker_id,
                attempt=row.attempts,
            )
            tasks.append(row)

        return tasks

    async def complete_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        output_json: dict,
        summary_text: str | None = None,
        duration_ms: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Mark task as completed and store output."""
        await session.execute(
            update(TaskInstance)
            .where(TaskInstance.id == task_id)
            .values(state=TaskState.completed, completed_at=datetime.utcnow())
        )

        # Upsert output
        stmt = pg_insert(TaskOutput).values(
            id=uuid.uuid4(),
            task_instance_id=task_id,
            output_json=output_json,
            summary_text=summary_text,
        ).on_conflict_do_update(
            constraint="uq_task_output_instance",
            set_={"output_json": output_json, "summary_text": summary_text}
        )
        await session.execute(stmt)

        # Record metric
        if duration_ms is not None:
            await session.execute(
                pg_insert(ProcessingMetric).values(
                    id=uuid.uuid4(),
                    task_instance_id=task_id,
                    worker_id=worker_id,
                    task_type=(await session.execute(
                        select(TaskInstance.task_type).where(TaskInstance.id == task_id)
                    )).scalar(),
                    duration_ms=duration_ms,
                    success=True,
                )
            )

        logger.info("task.completed", task_id=str(task_id))

    async def fail_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        error_message: str,
        max_attempts: int = 3,
        base_backoff_seconds: int = 60,
    ) -> None:
        """Mark task as failed. Reschedule with backoff or move to DLQ."""
        # Get current state
        result = await session.execute(
            select(TaskInstance.attempts, TaskInstance.max_attempts, TaskInstance.task_type)
            .where(TaskInstance.id == task_id)
        )
        row = result.fetchone()
        if not row:
            return

        attempts, max_att, task_type = row

        if attempts >= max_att:
            # Move to dead letter queue
            await session.execute(
                update(TaskInstance)
                .where(TaskInstance.id == task_id)
                .values(state=TaskState.dead_letter, error_message=error_message)
            )
            await session.execute(
                pg_insert(DeadLetterTask).values(
                    id=uuid.uuid4(),
                    task_instance_id=task_id,
                    error_type="max_retries_exceeded",
                    error_message=error_message,
                    payload_json={"attempts": attempts},
                ).on_conflict_do_nothing(constraint="uq_dlq_instance")
            )
            logger.warning("task.dead_lettered", task_id=str(task_id), task_type=task_type, attempts=attempts)
        else:
            # Exponential backoff: 60s, 240s, 960s, ...
            backoff = base_backoff_seconds * (4 ** (attempts - 1))
            available_at = datetime.utcnow() + timedelta(seconds=backoff)
            await session.execute(
                update(TaskInstance)
                .where(TaskInstance.id == task_id)
                .values(
                    state=TaskState.pending,
                    available_at=available_at,
                    lease_until=None,
                    leased_by=None,
                    error_message=error_message,
                )
            )
            logger.info(
                "task.retrying",
                task_id=str(task_id),
                task_type=task_type,
                attempt=attempts,
                next_available=available_at.isoformat(),
            )

    async def reclaim_expired_leases(self, session: AsyncSession) -> int:
        """Find tasks whose lease has expired and reset them to pending."""
        now = datetime.utcnow()
        result = await session.execute(
            update(TaskInstance)
            .where(
                and_(
                    TaskInstance.state == TaskState.leased,
                    TaskInstance.lease_until < now,
                )
            )
            .values(
                state=TaskState.pending,
                leased_by=None,
                lease_until=None,
            )
            .returning(TaskInstance.id)
        )
        reclaimed = result.fetchall()
        if reclaimed:
            logger.warning("queue.reclaimed_expired", count=len(reclaimed))
        return len(reclaimed)

    async def get_queue_stats(self, session: AsyncSession) -> dict:
        """Get queue statistics by state."""
        result = await session.execute(text("""
            SELECT state, COUNT(*) as count
            FROM task_instance
            GROUP BY state
        """))
        return {row.state: row.count for row in result.fetchall()}

    async def promote_discovered_tasks(self, session: AsyncSession) -> int:
        """Check prerequisites and promote discovered tasks to pending.

        A task is promotable when all its prerequisite task_types for
        the same media_item have completed.
        """
        # This uses a CTE to find discovered tasks whose prerequisites are all met
        promote_sql = text("""
            WITH prerequisite_check AS (
                SELECT
                    ti.id AS task_id,
                    ti.media_item_id,
                    ti.task_type,
                    td.prerequisites
                FROM task_instance ti
                JOIN task_definition td ON td.task_type = ti.task_type AND td.version = ti.task_version
                WHERE ti.state = 'discovered'
                  AND td.prerequisites IS NOT NULL
                  AND td.prerequisites != '[]'::jsonb
            ),
            met_prerequisites AS (
                SELECT pc.task_id
                FROM prerequisite_check pc
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(pc.prerequisites) AS prereq(task_type)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM task_instance ti2
                        WHERE ti2.media_item_id = pc.media_item_id
                          AND ti2.task_type = prereq.task_type
                          AND ti2.state = 'completed'
                    )
                )
            ),
            no_prerequisites AS (
                SELECT ti.id AS task_id
                FROM task_instance ti
                JOIN task_definition td ON td.task_type = ti.task_type AND td.version = ti.task_version
                WHERE ti.state = 'discovered'
                  AND (td.prerequisites IS NULL OR td.prerequisites = '[]'::jsonb)
            )
            UPDATE task_instance
            SET state = 'pending', updated_at = NOW()
            WHERE id IN (
                SELECT task_id FROM met_prerequisites
                UNION
                SELECT task_id FROM no_prerequisites
            )
            RETURNING id
        """)

        result = await session.execute(promote_sql)
        promoted = result.fetchall()
        if promoted:
            logger.info("queue.promoted_discovered", count=len(promoted))
        return len(promoted)
