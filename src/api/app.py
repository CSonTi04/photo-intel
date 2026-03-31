"""FastAPI application — admin API and future UI backend."""

from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text

from src.config.settings import settings
from src.ingest.scanner import run_batch_scan
from src.models.database import async_session
from src.models.tables import (
    DeadLetterTask,
    MediaItem,
    TaskDefinition,
    TaskInstance,
    TaskOutput,
    TaskState,
)
from src.queue.postgres_queue import PostgresQueue
from src.tasks.planner import TaskPlanner


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup/shutdown."""
    # Import handlers to register them
    import src.tasks.handlers  # noqa
    yield


app = FastAPI(
    title="Photo Intelligence API",
    description="Admin API for the Photo Intelligence & Digest System",
    version="0.1.0",
    lifespan=lifespan,
)

queue = PostgresQueue(async_session)
planner = TaskPlanner(queue)


# ── Health ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check."""
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "healthy", "db": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "db": str(e)}


# ── Stats ──────────────────────────────────────────────────────

class SystemStats(BaseModel):
    total_media: int
    photos: int
    screenshots: int
    queue_stats: dict
    tasks_completed_today: int
    tasks_failed_today: int


@app.get("/stats", response_model=SystemStats)
async def get_stats():
    """Get system-wide statistics."""
    async with async_session() as session:
        # Media counts
        total = (await session.execute(select(func.count(MediaItem.id)))).scalar()
        photos = (await session.execute(
            select(func.count(MediaItem.id)).where(MediaItem.media_kind == "photo")
        )).scalar()
        screenshots = (await session.execute(
            select(func.count(MediaItem.id)).where(MediaItem.media_kind == "screenshot")
        )).scalar()

        # Queue stats
        q_stats = await queue.get_queue_stats(session)

        # Today's completion stats
        today_completed = (await session.execute(text("""
            SELECT COUNT(*) FROM task_instance
            WHERE state = 'completed' AND completed_at >= CURRENT_DATE
        """))).scalar()

        today_failed = (await session.execute(text("""
            SELECT COUNT(*) FROM task_instance
            WHERE state IN ('failed', 'dead_letter') AND updated_at >= CURRENT_DATE
        """))).scalar()

        return SystemStats(
            total_media=total,
            photos=photos,
            screenshots=screenshots,
            queue_stats=q_stats,
            tasks_completed_today=today_completed,
            tasks_failed_today=today_failed,
        )


# ── Ingest ─────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    directories: list[str] | None = None
    batch_size: int = 500


class IngestResponse(BaseModel):
    scanned: int
    registered: int
    duplicates: int
    errors: int
    tasks_planned: int


@app.post("/ingest/scan", response_model=IngestResponse)
async def trigger_scan(req: IngestRequest):
    """Trigger a batch scan of configured directories."""
    dirs = req.directories or settings.ingest.watch_dirs

    async with async_session() as session:
        scan_stats = await run_batch_scan(session, dirs, req.batch_size)

        # Plan tasks for newly registered items
        tasks_planned = 0
        if scan_stats["registered"] > 0:
            # Get recently registered items
            result = await session.execute(
                select(MediaItem.id)
                .order_by(MediaItem.created_at.desc())
                .limit(scan_stats["registered"])
            )
            new_ids = [row[0] for row in result.fetchall()]
            plan_stats = await planner.plan_batch(session, new_ids)
            tasks_planned = plan_stats["tasks_created"]
            await session.commit()

        return IngestResponse(
            **scan_stats,
            tasks_planned=tasks_planned,
        )


# ── Media ──────────────────────────────────────────────────────

class MediaItemResponse(BaseModel):
    id: UUID
    file_path: str
    media_kind: str
    mime_type: str | None
    width: int | None
    height: int | None
    file_size: int | None
    captured_at: str | None
    created_at: str
    task_count: int = 0


@app.get("/media")
async def list_media(
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0),
    kind: str = Query(default=None),
):
    """List media items with pagination."""
    async with async_session() as session:
        query = select(MediaItem).order_by(MediaItem.created_at.desc()).limit(limit).offset(offset)
        if kind:
            query = query.where(MediaItem.media_kind == kind)
        result = await session.execute(query)
        items = result.scalars().all()

        return [
            MediaItemResponse(
                id=item.id,
                file_path=item.file_path,
                media_kind=item.media_kind.value,
                mime_type=item.mime_type,
                width=item.width,
                height=item.height,
                file_size=item.file_size,
                captured_at=item.captured_at.isoformat() if item.captured_at else None,
                created_at=item.created_at.isoformat(),
            )
            for item in items
        ]


@app.get("/media/{media_id}")
async def get_media_detail(media_id: UUID):
    """Get detailed info for a media item including all task outputs."""
    async with async_session() as session:
        result = await session.execute(
            select(MediaItem).where(MediaItem.id == media_id)
        )
        media = result.scalar_one_or_none()
        if not media:
            raise HTTPException(404, "Media item not found")

        # Get all task instances with outputs
        tasks_result = await session.execute(
            select(TaskInstance, TaskOutput)
            .outerjoin(TaskOutput, TaskOutput.task_instance_id == TaskInstance.id)
            .where(TaskInstance.media_item_id == media_id)
            .order_by(TaskInstance.task_type)
        )

        tasks = []
        for ti, to in tasks_result.fetchall():
            tasks.append({
                "task_type": ti.task_type,
                "task_version": ti.task_version,
                "state": ti.state.value,
                "attempts": ti.attempts,
                "output": to.output_json if to else None,
                "summary": to.summary_text if to else None,
                "completed_at": ti.completed_at.isoformat() if ti.completed_at else None,
            })

        return {
            "media": {
                "id": str(media.id),
                "file_path": media.file_path,
                "media_kind": media.media_kind.value,
                "mime_type": media.mime_type,
                "width": media.width,
                "height": media.height,
                "file_size": media.file_size,
                "content_hash": media.content_hash,
                "captured_at": media.captured_at.isoformat() if media.captured_at else None,
            },
            "tasks": tasks,
        }


# ── Tasks ──────────────────────────────────────────────────────

@app.get("/tasks/definitions")
async def list_task_definitions():
    """List all task definitions."""
    async with async_session() as session:
        result = await session.execute(
            select(TaskDefinition).order_by(TaskDefinition.priority)
        )
        defs = result.scalars().all()
        return [
            {
                "task_type": d.task_type,
                "version": d.version,
                "enabled": d.enabled,
                "priority": d.priority,
                "prerequisites": d.prerequisites,
                "applies_to": d.applies_to,
                "max_retries": d.max_retries,
                "prompt_preview": d.prompt_template[:100] + "..." if d.prompt_template else None,
            }
            for d in defs
        ]


@app.post("/tasks/replan/{media_id}")
async def replan_tasks(media_id: UUID):
    """Replan tasks for a specific media item."""
    async with async_session() as session:
        created = await planner.plan_for_media_item(session, media_id)
        await session.commit()
        return {"tasks_created": len(created), "task_ids": [str(t) for t in created]}


# ── DLQ ────────────────────────────────────────────────────────

@app.get("/dlq")
async def list_dead_letter_tasks(limit: int = Query(default=50)):
    """List dead letter queue entries."""
    async with async_session() as session:
        result = await session.execute(
            select(DeadLetterTask, TaskInstance)
            .join(TaskInstance, TaskInstance.id == DeadLetterTask.task_instance_id)
            .order_by(DeadLetterTask.created_at.desc())
            .limit(limit)
        )
        items = []
        for dlq, ti in result.fetchall():
            items.append({
                "task_instance_id": str(ti.id),
                "media_item_id": str(ti.media_item_id),
                "task_type": ti.task_type,
                "error_type": dlq.error_type,
                "error_message": dlq.error_message,
                "attempts": ti.attempts,
                "created_at": dlq.created_at.isoformat(),
            })
        return items


@app.post("/dlq/retry/{task_instance_id}")
async def retry_dead_letter(task_instance_id: UUID):
    """Retry a dead letter task by resetting it to pending."""
    async with async_session() as session:
        from sqlalchemy import delete, update

        # Reset task instance
        await session.execute(
            update(TaskInstance)
            .where(TaskInstance.id == task_instance_id)
            .values(
                state=TaskState.pending,
                attempts=0,
                error_message=None,
                lease_until=None,
                leased_by=None,
            )
        )
        # Remove from DLQ
        await session.execute(
            delete(DeadLetterTask)
            .where(DeadLetterTask.task_instance_id == task_instance_id)
        )
        await session.commit()
        return {"status": "retried", "task_instance_id": str(task_instance_id)}


# ── Metrics ────────────────────────────────────────────────────

@app.get("/metrics/processing")
async def get_processing_metrics(hours: int = Query(default=24)):
    """Get processing metrics for the last N hours."""
    async with async_session() as session:
        result = await session.execute(text(f"""
            SELECT
                task_type,
                COUNT(*) as total,
                AVG(duration_ms) as avg_ms,
                MAX(duration_ms) as max_ms,
                MIN(duration_ms) as min_ms,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as failures
            FROM processing_metric
            WHERE created_at >= NOW() - INTERVAL '{hours} hours'
            GROUP BY task_type
            ORDER BY task_type
        """))
        return [
            {
                "task_type": row.task_type,
                "total": row.total,
                "avg_ms": round(row.avg_ms) if row.avg_ms else 0,
                "max_ms": row.max_ms,
                "min_ms": row.min_ms,
                "successes": row.successes,
                "failures": row.failures,
            }
            for row in result.fetchall()
        ]


# ── Entry point ────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )
