# Photo Intelligence — Main application image
# Used for: API, workers (CPU, VLM, digest, maintenance), CLI

FROM python:3.11-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-hun \
    tesseract-ocr-eng \
    libmagic1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir .

# Copy source
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()" || exit 1

EXPOSE 8000

CMD ["python", "-m", "src.cli", "api"]
