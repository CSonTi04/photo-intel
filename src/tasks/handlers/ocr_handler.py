"""OCR task handlers — full text extraction and entity detection."""

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Optional

import structlog
from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tables import MediaItem, MediaOCR
from src.tasks import TaskRetryableError, TaskPermanentError
from src.tasks.registry import register_task

logger = structlog.get_logger()


def run_tesseract(image_path: Path, lang: str = "eng+hun", dpi: int = 300) -> dict:
    """Run Tesseract OCR on image. Returns text + confidence."""
    import pytesseract

    try:
        img = Image.open(image_path)

        # Preprocess for better OCR
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Get full text with confidence data
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)

        # Build full text
        words = []
        confidences = []
        blocks = []
        current_block = {"block_num": -1, "text": "", "confidence": 0}

        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            conf = int(data["conf"][i])
            block_num = data["block_num"][i]

            if text and conf > 0:
                words.append(text)
                confidences.append(conf)

                if block_num != current_block["block_num"]:
                    if current_block["text"]:
                        blocks.append(current_block.copy())
                    current_block = {
                        "block_num": block_num,
                        "text": text,
                        "confidence": conf,
                        "left": data["left"][i],
                        "top": data["top"][i],
                        "width": data["width"][i],
                        "height": data["height"][i],
                    }
                else:
                    current_block["text"] += " " + text
                    current_block["confidence"] = (current_block["confidence"] + conf) / 2

        if current_block["text"]:
            blocks.append(current_block)

        full_text = " ".join(words)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        return {
            "full_text": full_text,
            "blocks": blocks,
            "confidence": round(avg_confidence, 2),
            "word_count": len(words),
        }

    except Exception as e:
        raise TaskRetryableError(f"Tesseract failed: {e}")


@register_task
class OCRFullHandler:
    task_type = "ocr_full"

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

        lang = task_config.get("lang", "eng+hun")
        dpi = task_config.get("dpi", 300)

        ocr_result = run_tesseract(file_path, lang=lang, dpi=dpi)

        # Store in media_ocr
        engine_version = "5.x"  # Tesseract version
        try:
            import pytesseract
            engine_version = pytesseract.get_tesseract_version().public
        except Exception:
            pass

        stmt = pg_insert(MediaOCR).values(
            id=uuid.uuid4(),
            media_item_id=media_item_id,
            engine="tesseract",
            engine_version=str(engine_version),
            full_text=ocr_result["full_text"],
            structured_blocks_json=ocr_result["blocks"],
            confidence=ocr_result["confidence"],
        ).on_conflict_do_update(
            constraint="uq_media_ocr_engine",
            set_={
                "full_text": ocr_result["full_text"],
                "structured_blocks_json": ocr_result["blocks"],
                "confidence": ocr_result["confidence"],
            },
        )
        await session.execute(stmt)

        output = {
            "word_count": ocr_result["word_count"],
            "confidence": ocr_result["confidence"],
            "text_preview": ocr_result["full_text"][:500],
            "block_count": len(ocr_result["blocks"]),
        }

        logger.info(
            "ocr.completed",
            media_item_id=str(media_item_id),
            word_count=output["word_count"],
            confidence=output["confidence"],
        )

        return output


@register_task
class OCREntitiesHandler:
    """Extract structured entities from OCR text (dates, URLs, prices, etc.)."""

    task_type = "ocr_entities"

    # Regex patterns for entity detection
    PATTERNS = {
        "dates": [
            r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b",
            r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b",
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b",
            r"\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}\b",
        ],
        "urls": [
            r"https?://[^\s<>\"']+",
            r"www\.[^\s<>\"']+",
        ],
        "prices": [
            r"[$€£¥]\s?\d[\d,]*\.?\d*",
            r"\d[\d,]*\.?\d*\s?(?:USD|EUR|HUF|Ft|GBP)",
            r"\d[\d\s]*\s?Ft\b",
        ],
        "emails": [
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        ],
        "addresses": [
            r"\d{4,5}\s+[A-Z][a-záéíóöőúüű]+",  # Postal code + city (HU pattern)
        ],
        "phone_numbers": [
            r"\+?\d[\d\s-]{7,15}",
        ],
    }

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
        # Get OCR text (prerequisite: ocr_full must be completed)
        result = await session.execute(
            select(MediaOCR.full_text).where(MediaOCR.media_item_id == media_item_id)
        )
        row = result.fetchone()
        if not row or not row.full_text:
            return {"entities": {}, "entity_count": 0, "note": "no_ocr_text_available"}

        full_text = row.full_text
        detect_types = task_config.get("detect", list(self.PATTERNS.keys()))

        entities = {}
        total_count = 0

        for entity_type in detect_types:
            patterns = self.PATTERNS.get(entity_type, [])
            found = set()
            for pattern in patterns:
                matches = re.findall(pattern, full_text, re.IGNORECASE)
                found.update(matches)
            entities[entity_type] = sorted(found)
            total_count += len(found)

        output = {
            "entities": entities,
            "entity_count": total_count,
            "detected_types": [k for k, v in entities.items() if v],
        }

        logger.info(
            "ocr_entities.extracted",
            media_item_id=str(media_item_id),
            entity_count=total_count,
            types=output["detected_types"],
        )

        return output
