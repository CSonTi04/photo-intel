"""Task handler implementations.

Import all handlers here to trigger auto-registration.
"""

from src.tasks.handlers.exif_handler import ExtractExifHandler
from src.tasks.handlers.ocr_handler import OCREntitiesHandler, OCRFullHandler
from src.tasks.handlers.thumbnail_handler import GenerateThumbnailHandler
from src.tasks.handlers.vlm_handler import VLMActionabilityHandler, VLMCaptionHandler, VLMMemorySummaryHandler

__all__ = [
    "ExtractExifHandler",
    "OCREntitiesHandler",
    "OCRFullHandler",
    "GenerateThumbnailHandler",
    "VLMActionabilityHandler",
    "VLMCaptionHandler",
    "VLMMemorySummaryHandler",
]
