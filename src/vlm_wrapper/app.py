"""VLM Wrapper Service — runs on GPU node (4070 Ti).

This is a SEPARATE FastAPI service deployed on the GPU machine.
It wraps Ollama and provides:
- Readiness gating (VRAM, cooldown, manual override)
- Prompt management per task type
- Image preprocessing
- Output validation
- Response caching
"""

import io
import json
import subprocess
import time
from datetime import datetime

import httpx
import structlog
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from pydantic_settings import BaseSettings

logger = structlog.get_logger()

# ── Settings ───────────────────────────────────────────────────


class WrapperSettings(BaseSettings):
    ollama_base_url: str = "http://localhost:11434"
    default_model: str = "llava:13b"
    vram_threshold_mb: int = 2000  # Minimum free VRAM to accept tasks
    cooldown_seconds: int = 5  # Cooldown between tasks
    manual_override: bool = False  # Manual kill switch
    max_image_dimension: int = 1344  # Max dimension for VLM input
    jpeg_quality: int = 85
    cache_dir: str = "/tmp/vlm_cache"
    bind_host: str = "0.0.0.0"
    bind_port: int = 8100

    class Config:
        env_prefix = "VLM_"


wrapper_settings = WrapperSettings()

# ── Prompt Registry ────────────────────────────────────────────

PROMPT_REGISTRY: dict[str, dict] = {
    "vlm_caption": {
        "version": 1,
        "system": "You are a precise image analyst. Always respond in valid JSON.",
        "prompt": (
            "Describe this image in 1-2 concise sentences. Identify:\n"
            "- The main scene or subject\n"
            "- Key objects, people, or text visible\n"
            "- The setting or context\n\n"
            "Respond ONLY in this JSON format:\n"
            '{"caption": "...", "scene_category": "indoor|outdoor|screen|document|food|people|nature|urban|other", '
            '"subjects": ["..."], "mood": "...", "has_text": true/false}'
        ),
        "output_schema": {
            "type": "object",
            "required": ["caption", "scene_category"],
            "properties": {
                "caption": {"type": "string"},
                "scene_category": {
                    "type": "string",
                    "enum": ["indoor", "outdoor", "screen", "document", "food", "people", "nature", "urban", "other"],
                },
                "subjects": {"type": "array", "items": {"type": "string"}},
                "mood": {"type": "string"},
                "has_text": {"type": "boolean"},
            },
        },
    },
    "vlm_actionability": {
        "version": 1,
        "system": "You are a personal assistant analyzing screenshots for actionable items. Always respond in valid JSON.",  # noqa: E501
        "prompt": (
            "Analyze this screenshot carefully. Determine if it contains any actionable information "
            "the user should act on. Look for:\n"
            "- Deadlines, appointments, or time-sensitive items\n"
            "- Prices, deals, or financial information\n"
            "- Bookings, reservations, or confirmations\n"
            "- Tasks, reminders, or to-do items\n"
            "- Error messages or warnings requiring attention\n\n"
            "Respond ONLY in this JSON format:\n"
            '{"is_actionable": true/false, "action_items": [{"item": "...", "due_date": "..." or null}], '
            '"urgency": "none|low|medium|high", "category": "deadline|financial|booking|task|error|info|other", '
            '"reasoning": "brief explanation"}'
        ),
        "output_schema": {
            "type": "object",
            "required": ["is_actionable", "urgency"],
            "properties": {
                "is_actionable": {"type": "boolean"},
                "action_items": {"type": "array", "items": {"type": "object"}},
                "urgency": {"type": "string", "enum": ["none", "low", "medium", "high"]},
                "category": {"type": "string"},
                "reasoning": {"type": "string"},
            },
        },
    },
    "vlm_memory_summary": {
        "version": 1,
        "system": "You are a personal memory assistant. Extract what's worth remembering from images. Always respond in valid JSON.",  # noqa: E501
        "prompt": (
            "Analyze this image and determine what is worth remembering for the user's "
            "personal knowledge base. Consider:\n"
            "- People present and their context\n"
            "- Places shown and their significance\n"
            "- Events or occasions captured\n"
            "- Information displayed (receipts, tickets, documents)\n"
            "- Emotional or sentimental value\n\n"
            "Respond ONLY in this JSON format:\n"
            '{"memory_relevance": "none|low|medium|high", '
            '"summary": "what to remember about this image", '
            '"people_hints": ["..."], "place_hints": ["..."], '
            '"time_hints": ["..."], "tags": ["..."], '
            '"emotional_context": "neutral|happy|important|milestone|routine"}'
        ),
        "output_schema": {
            "type": "object",
            "required": ["memory_relevance", "summary"],
            "properties": {
                "memory_relevance": {"type": "string", "enum": ["none", "low", "medium", "high"]},
                "summary": {"type": "string"},
                "people_hints": {"type": "array", "items": {"type": "string"}},
                "place_hints": {"type": "array", "items": {"type": "string"}},
                "time_hints": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "emotional_context": {"type": "string"},
            },
        },
    },
}

# ── State ──────────────────────────────────────────────────────


class WrapperState:
    def __init__(self):
        self.last_task_time: datetime | None = None
        self.tasks_processed: int = 0
        self.errors: int = 0


state = WrapperState()

# ── App ────────────────────────────────────────────────────────

app = FastAPI(
    title="VLM Wrapper Service",
    description="GPU-side wrapper for Vision LLM inference",
    version="0.1.0",
)


# ── Helpers ────────────────────────────────────────────────────


def get_nvidia_free_vram_mb() -> int:
    """Query nvidia-smi for free VRAM in MB."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip().split("\n")[0])
    except Exception as e:
        logger.warning("vram.check_failed", error=str(e))
    return 0


def preprocess_image(image_bytes: bytes, max_dim: int = 1344, quality: int = 85) -> bytes:
    """Resize and optimize image for VLM input."""
    img = Image.open(io.BytesIO(image_bytes))

    # Convert to RGB if needed
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Resize if needed
    if max(img.width, img.height) > max_dim:
        ratio = max_dim / max(img.width, img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    # Export as JPEG bytes
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def parse_vlm_json(raw_text: str) -> dict:
    """Try to extract JSON from VLM response text."""
    text = raw_text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        try:
            return json.loads(text[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        try:
            return json.loads(text[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try finding JSON object in text
    for i, char in enumerate(text):
        if char == "{":
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i : j + 1])
                        except json.JSONDecodeError:
                            break
            break

    # Fallback: return raw text wrapped
    return {"raw_response": text, "parse_error": True}


# ── Endpoints ──────────────────────────────────────────────────


class ReadinessResponse(BaseModel):
    ready: bool
    reason: str | None = None
    free_vram_mb: int | None = None
    cooldown_remaining_seconds: float | None = None


@app.get("/ready-for-vlm", response_model=ReadinessResponse)
async def check_readiness():
    """Check if the wrapper is ready to accept VLM tasks."""

    # Manual override
    if wrapper_settings.manual_override:
        return ReadinessResponse(ready=False, reason="manual_override_active")

    # VRAM check
    free_vram = get_nvidia_free_vram_mb()
    if free_vram < wrapper_settings.vram_threshold_mb:
        return ReadinessResponse(
            ready=False,
            reason="insufficient_vram",
            free_vram_mb=free_vram,
        )

    # Cooldown check
    if state.last_task_time:
        elapsed = (datetime.utcnow() - state.last_task_time).total_seconds()
        remaining = wrapper_settings.cooldown_seconds - elapsed
        if remaining > 0:
            return ReadinessResponse(
                ready=False,
                reason="cooldown_active",
                cooldown_remaining_seconds=round(remaining, 1),
            )

    # Check Ollama is responsive
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{wrapper_settings.ollama_base_url}/api/tags")
            if resp.status_code != 200:
                return ReadinessResponse(ready=False, reason="ollama_not_responding")
    except Exception:
        return ReadinessResponse(ready=False, reason="ollama_unreachable")

    return ReadinessResponse(ready=True, free_vram_mb=free_vram)


class TaskResponse(BaseModel):
    task_type: str
    model_used: str
    prompt_version: int
    output: dict
    processing_time_ms: int
    raw_response: str | None = None


@app.post("/run-task/{task_type}", response_model=TaskResponse)
async def run_task(
    task_type: str,
    image: UploadFile = File(...),
    config: str = Form("{}"),
):
    """Execute a VLM task on the provided image."""

    # Validate task type
    if task_type not in PROMPT_REGISTRY:
        raise HTTPException(404, f"Unknown task type: {task_type}")

    # Check readiness
    readiness = await check_readiness()
    if not readiness.ready:
        raise HTTPException(503, f"Not ready: {readiness.reason}")

    prompt_def = PROMPT_REGISTRY[task_type]
    task_config = json.loads(config)
    model = task_config.get("model", wrapper_settings.default_model)

    # Read and preprocess image
    image_bytes = await image.read()
    processed_bytes = preprocess_image(
        image_bytes,
        max_dim=wrapper_settings.max_image_dimension,
        quality=wrapper_settings.jpeg_quality,
    )

    # Build Ollama request
    import base64

    image_b64 = base64.b64encode(processed_bytes).decode("utf-8")

    ollama_payload = {
        "model": model,
        "prompt": prompt_def["prompt"],
        "system": prompt_def.get("system", ""),
        "images": [image_b64],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": task_config.get("max_tokens", 512),
        },
    }

    # Call Ollama
    start_time = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=wrapper_settings.ollama_base_url and 120) as client:
            resp = await client.post(
                f"{wrapper_settings.ollama_base_url}/api/generate",
                json=ollama_payload,
                timeout=120,
            )
            resp.raise_for_status()
            ollama_result = resp.json()
    except httpx.TimeoutException:
        raise HTTPException(504, "Ollama inference timed out")
    except Exception as e:
        raise HTTPException(502, f"Ollama error: {str(e)}")

    processing_time_ms = int((time.monotonic() - start_time) * 1000)

    # Parse response
    raw_text = ollama_result.get("response", "")
    output = parse_vlm_json(raw_text)

    # Update state
    state.last_task_time = datetime.utcnow()
    state.tasks_processed += 1

    logger.info(
        "vlm.task_completed",
        task_type=task_type,
        model=model,
        processing_time_ms=processing_time_ms,
        parse_success="parse_error" not in output,
    )

    return TaskResponse(
        task_type=task_type,
        model_used=model,
        prompt_version=prompt_def["version"],
        output=output,
        processing_time_ms=processing_time_ms,
        raw_response=raw_text if output.get("parse_error") else None,
    )


@app.get("/prompts")
async def list_prompts():
    """List all registered prompts and their versions."""
    return {
        task_type: {
            "version": p["version"],
            "has_schema": "output_schema" in p,
            "prompt_preview": p["prompt"][:100] + "...",
        }
        for task_type, p in PROMPT_REGISTRY.items()
    }


@app.get("/stats")
async def get_stats():
    """Get wrapper statistics."""
    free_vram = get_nvidia_free_vram_mb()
    return {
        "tasks_processed": state.tasks_processed,
        "errors": state.errors,
        "free_vram_mb": free_vram,
        "last_task_time": state.last_task_time.isoformat() if state.last_task_time else None,
        "manual_override": wrapper_settings.manual_override,
    }


@app.post("/override/{action}")
async def set_override(action: str):
    """Enable/disable manual override (kill switch)."""
    if action == "enable":
        wrapper_settings.manual_override = True
        return {"override": True}
    elif action == "disable":
        wrapper_settings.manual_override = False
        return {"override": False}
    raise HTTPException(400, "Use /override/enable or /override/disable")


# ── Startup ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.vlm_wrapper.app:app",
        host=wrapper_settings.bind_host,
        port=wrapper_settings.bind_port,
        reload=False,
    )
