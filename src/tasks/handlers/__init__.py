"""Task handler implementations.

Import all handlers here to trigger auto-registration.
"""

from src.tasks.handlers.exif_handler import ExtractExifHandler
from src.tasks.handlers.thumbnail_handler import GenerateThumbnailHandler
from src.tasks.handlers.ocr_handler import OCRFullHandler, OCREntitiesHandler
from src.tasks.handlers.vlm_handler import VLMCaptionHandler, VLMActionabilityHandler, VLMMemorySummaryHandler
