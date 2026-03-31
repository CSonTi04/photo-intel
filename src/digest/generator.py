"""Digest generator — produces daily and resurface digests.

Daily digest: summarizes today's processed images
Resurface digest: surfaces older interesting content based on scoring
"""

import uuid
from datetime import date, datetime, timedelta

import structlog
from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tables import (
    DigestItem,
    DigestRun,
    DigestType,
)

logger = structlog.get_logger()


class DigestGenerator:
    """Generates structured digests from processed media."""

    async def generate_daily_digest(
        self,
        session: AsyncSession,
        target_date: date = None,
    ) -> dict:
        """Generate a daily digest for a specific date.

        Sections:
        - highlights: high-relevance items
        - actionable: items with action items
        - screenshots: OCR'd screenshots with info
        - photos: captioned photos
        - stats: processing statistics
        """
        target_date = target_date or date.today()

        # Check if already exists (idempotency)
        existing = await session.execute(
            select(DigestRun).where(
                and_(
                    DigestRun.digest_type == DigestType.daily,
                    DigestRun.target_date == target_date,
                )
            )
        )
        if existing.scalar_one_or_none():
            logger.info("digest.already_exists", type="daily", date=str(target_date))
            return {"status": "already_exists", "date": str(target_date)}

        # Create digest run
        digest_id = uuid.uuid4()
        await session.execute(
            pg_insert(DigestRun).values(
                id=digest_id,
                digest_type=DigestType.daily,
                target_date=target_date,
                status="generating",
            )
        )

        # Get all completed tasks for media items captured/created on target date
        next_date = target_date + timedelta(days=1)

        items_query = text("""
            SELECT
                mi.id AS media_item_id,
                mi.file_path,
                mi.media_kind,
                mi.captured_at,
                ti.task_type,
                "to".output_json,
                "to".summary_text
            FROM media_item mi
            JOIN task_instance ti ON ti.media_item_id = mi.id
            LEFT JOIN task_output "to" ON "to".task_instance_id = ti.id
            WHERE (mi.captured_at >= :start_date AND mi.captured_at < :end_date)
               OR (mi.created_at >= :start_date AND mi.created_at < :end_date)
            AND ti.state = 'completed'
            ORDER BY mi.captured_at, ti.task_type
        """)

        result = await session.execute(
            items_query,
            {
                "start_date": datetime.combine(target_date, datetime.min.time()),
                "end_date": datetime.combine(next_date, datetime.min.time()),
            },
        )
        rows = result.fetchall()

        # Group by media_item
        media_data = {}
        for row in rows:
            mid = row.media_item_id
            if mid not in media_data:
                media_data[mid] = {
                    "media_item_id": mid,
                    "file_path": row.file_path,
                    "media_kind": row.media_kind,
                    "captured_at": row.captured_at,
                    "tasks": {},
                }
            media_data[mid]["tasks"][row.task_type] = {
                "output": row.output_json,
                "summary": row.summary_text,
            }

        # Score and categorize items
        digest_items = []

        for mid, data in media_data.items():
            tasks = data["tasks"]
            score = 0.0
            section = "photos" if data["media_kind"] == "photo" else "screenshots"

            # Score based on VLM outputs
            if "vlm_actionability" in tasks:
                output = tasks["vlm_actionability"].get("output", {})
                if output.get("is_actionable"):
                    section = "actionable"
                    urgency_scores = {"high": 3.0, "medium": 2.0, "low": 1.0, "none": 0.0}
                    score += urgency_scores.get(output.get("urgency", "none"), 0)

            if "vlm_memory_summary" in tasks:
                output = tasks["vlm_memory_summary"].get("output", {})
                relevance_scores = {"high": 3.0, "medium": 2.0, "low": 1.0, "none": 0.0}
                rel_score = relevance_scores.get(output.get("memory_relevance", "none"), 0)
                if rel_score >= 2.0:
                    section = "highlights"
                score += rel_score

            if "vlm_caption" in tasks:
                score += 0.5  # Having a caption is a baseline positive

            # Build summary
            summary_parts = []
            if "vlm_caption" in tasks:
                caption = tasks["vlm_caption"].get("output", {}).get("caption", "")
                if caption:
                    summary_parts.append(caption)
            if "vlm_actionability" in tasks:
                reasoning = tasks["vlm_actionability"].get("output", {}).get("reasoning", "")
                if reasoning:
                    summary_parts.append(f"Action: {reasoning}")

            digest_items.append(
                {
                    "media_item_id": mid,
                    "section": section,
                    "rank_score": score,
                    "summary_text": " | ".join(summary_parts) if summary_parts else None,
                    "metadata_json": {"tasks_completed": list(tasks.keys())},
                }
            )

        # Sort by score within sections
        digest_items.sort(key=lambda x: (-x["rank_score"], x["section"]))

        # Insert digest items
        for item in digest_items:
            await session.execute(
                pg_insert(DigestItem).values(
                    id=uuid.uuid4(),
                    digest_run_id=digest_id,
                    **item,
                )
            )

        # Update digest run status
        from sqlalchemy import update

        await session.execute(
            update(DigestRun)
            .where(DigestRun.id == digest_id)
            .values(
                status="completed",
                total_items=len(digest_items),
                completed_at=datetime.utcnow(),
                config_snapshot_json={
                    "target_date": str(target_date),
                    "sections": list(set(i["section"] for i in digest_items)),
                },
            )
        )

        logger.info(
            "digest.daily.completed",
            date=str(target_date),
            total_items=len(digest_items),
            sections={
                s: len([i for i in digest_items if i["section"] == s]) for s in set(i["section"] for i in digest_items)
            },
        )

        return {
            "status": "completed",
            "digest_id": str(digest_id),
            "date": str(target_date),
            "total_items": len(digest_items),
            "sections": {
                section: [
                    {
                        "media_item_id": str(i["media_item_id"]),
                        "score": i["rank_score"],
                        "summary": i["summary_text"],
                    }
                    for i in digest_items
                    if i["section"] == section
                ]
                for section in sorted(set(i["section"] for i in digest_items))
            },
        }
