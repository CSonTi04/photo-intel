"""CLI entry point for Photo Intelligence system."""

import asyncio
import click
import structlog

from src.utils.logging import setup_logging
from src.config.settings import settings

logger = structlog.get_logger()


@click.group()
@click.option("--log-level", default=settings.log_level, help="Log level")
@click.option("--json-logs", is_flag=True, help="JSON log output")
def main(log_level: str, json_logs: bool):
    """Photo Intelligence & Digest System."""
    setup_logging(log_level, json_logs)


@main.command()
@click.option("--dirs", multiple=True, help="Directories to scan")
@click.option("--batch-size", default=500, help="Batch size")
def scan(dirs: tuple, batch_size: int):
    """Run batch filesystem scan."""
    async def _scan():
        from src.models.database import async_session
        from src.ingest.scanner import run_batch_scan

        directories = list(dirs) if dirs else settings.ingest.watch_dirs
        click.echo(f"Scanning directories: {directories}")

        async with async_session() as session:
            stats = await run_batch_scan(session, directories, batch_size)
            click.echo(f"Scan complete: {stats}")

    asyncio.run(_scan())


@main.command()
@click.option("--dirs", multiple=True, help="Directories to scan")
@click.option("--batch-size", default=500, help="Batch size")
def ingest(dirs: tuple, batch_size: int):
    """Scan + plan tasks for new images."""
    async def _ingest():
        from src.models.database import async_session
        from src.ingest.scanner import run_batch_scan
        from src.queue.postgres_queue import PostgresQueue
        from src.tasks.planner import TaskPlanner
        from src.models.tables import MediaItem
        from sqlalchemy import select
        import src.tasks.handlers  # noqa - register handlers

        directories = list(dirs) if dirs else settings.ingest.watch_dirs
        click.echo(f"Ingesting from: {directories}")

        queue = PostgresQueue(async_session)
        planner = TaskPlanner(queue)

        async with async_session() as session:
            # Scan
            scan_stats = await run_batch_scan(session, directories, batch_size)
            click.echo(f"Scan: {scan_stats}")

            # Plan tasks for new items
            if scan_stats["registered"] > 0:
                result = await session.execute(
                    select(MediaItem.id)
                    .order_by(MediaItem.created_at.desc())
                    .limit(scan_stats["registered"])
                )
                new_ids = [row[0] for row in result.fetchall()]
                plan_stats = await planner.plan_batch(session, new_ids)
                click.echo(f"Planning: {plan_stats}")
                await session.commit()

    asyncio.run(_ingest())


@main.command()
@click.option("--type", "worker_type", required=True,
              type=click.Choice(["cpu", "vlm", "digest", "maintenance"]))
@click.option("--id", "worker_id", default=None, help="Worker ID")
def worker(worker_type: str, worker_id: str):
    """Start a worker process."""
    async def _worker():
        from src.workers.worker_loop import Worker, run_maintenance_worker
        import src.tasks.handlers  # noqa - register handlers

        if worker_type == "maintenance":
            await run_maintenance_worker()
        else:
            w = Worker(worker_type=worker_type, worker_id=worker_id)
            await w.run_loop()

    click.echo(f"Starting {worker_type} worker...")
    asyncio.run(_worker())


@main.command()
def api():
    """Start the FastAPI server."""
    import uvicorn
    click.echo(f"Starting API on port {settings.api_port}...")
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.debug,
    )


@main.command()
@click.option("--date", "target_date", default=None, help="Target date (YYYY-MM-DD)")
def digest(target_date: str):
    """Generate daily digest."""
    async def _digest():
        from datetime import date, datetime
        from src.models.database import async_session
        from src.digest.generator import DigestGenerator

        gen = DigestGenerator()
        td = datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else date.today()

        async with async_session() as session:
            result = await gen.generate_daily_digest(session, td)
            await session.commit()
            click.echo(f"Digest result: {result}")

    asyncio.run(_digest())


@main.command()
def stats():
    """Show system statistics."""
    async def _stats():
        from src.models.database import async_session
        from src.queue.postgres_queue import PostgresQueue
        from sqlalchemy import select, func
        from src.models.tables import MediaItem, TaskInstance
        from rich.console import Console
        from rich.table import Table

        console = Console()

        async with async_session() as session:
            q = PostgresQueue(async_session)
            q_stats = await q.get_queue_stats(session)

            total_media = (await session.execute(select(func.count(MediaItem.id)))).scalar()

            table = Table(title="Photo Intelligence — System Stats")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")

            table.add_row("Total Media Items", str(total_media))
            for state, count in sorted(q_stats.items()):
                table.add_row(f"Tasks: {state}", str(count))

            console.print(table)

    asyncio.run(_stats())


if __name__ == "__main__":
    main()
