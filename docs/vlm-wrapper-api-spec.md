# VLM Wrapper Service — API Specification

Base URL: `http://gpu-node:8100`

## Endpoints

### GET /ready-for-vlm

Check if the GPU node is ready to accept VLM tasks.

**Response** `200 OK`:
```json
{
  "ready": true,
  "reason": null,
  "free_vram_mb": 10240,
  "cooldown_remaining_seconds": null
}
```

**Not Ready reasons:**
| reason | description |
|--------|-------------|
| `manual_override_active` | Manual kill switch enabled via `/override/enable` |
| `insufficient_vram` | Free VRAM below threshold (default 2000 MB) |
| `cooldown_active` | Cooldown between tasks (default 5 seconds) |
| `ollama_not_responding` | Ollama API returned non-200 |
| `ollama_unreachable` | Cannot connect to Ollama |

---

### POST /run-task/{task_type}

Execute a VLM task on an image.

**Path Parameters:**
- `task_type` — one of: `vlm_caption`, `vlm_actionability`, `vlm_memory_summary`

**Request** (`multipart/form-data`):
| field | type | description |
|-------|------|-------------|
| `image` | file | Image file (JPEG, PNG, WebP) |
| `config` | string (JSON) | Optional override config: `{"model": "llava:13b", "max_tokens": 512}` |

**Response** `200 OK`:
```json
{
  "task_type": "vlm_caption",
  "model_used": "llava:13b",
  "prompt_version": 1,
  "output": {
    "caption": "A person working at a desk with dual monitors showing code",
    "scene_category": "indoor",
    "subjects": ["person", "desk", "monitors", "keyboard"],
    "mood": "focused",
    "has_text": true
  },
  "processing_time_ms": 3450,
  "raw_response": null
}
```

**Error Responses:**
| status | meaning |
|--------|---------|
| `404` | Unknown task_type |
| `429` | Rate limited |
| `503` | Not ready (check reason in body) |
| `504` | Ollama inference timed out |
| `502` | Ollama error |

---

### GET /prompts

List all registered prompts and their versions.

**Response** `200 OK`:
```json
{
  "vlm_caption": {
    "version": 1,
    "has_schema": true,
    "prompt_preview": "Describe this image in 1-2 concise sentences..."
  }
}
```

---

### GET /stats

Get wrapper service statistics.

**Response** `200 OK`:
```json
{
  "tasks_processed": 142,
  "errors": 3,
  "free_vram_mb": 10240,
  "last_task_time": "2024-03-15T10:30:00",
  "manual_override": false
}
```

---

### POST /override/{action}

Enable/disable manual override (kill switch).

**Path Parameters:**
- `action` — `enable` or `disable`

**Response** `200 OK`:
```json
{
  "override": true
}
```

## Image Preprocessing

The wrapper automatically preprocesses images before sending to Ollama:

1. Convert to RGB if needed
2. Resize to max dimension (default: 1344px, configurable)
3. Export as JPEG with configurable quality (default: 85)
4. Base64 encode for Ollama API

## JSON Response Parsing

The wrapper attempts to parse VLM output as JSON using multiple strategies:
1. Direct JSON parse
2. Extract from ` ```json ... ``` ` code blocks
3. Extract from ` ``` ... ``` ` code blocks
4. Find first `{...}` object in text
5. Fallback: `{"raw_response": "...", "parse_error": true}`

If `parse_error` is in the output, the `raw_response` field contains the original text.

## Configuration (Environment Variables)

| variable | default | description |
|----------|---------|-------------|
| `VLM_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL |
| `VLM_DEFAULT_MODEL` | `llava:13b` | Default VLM model |
| `VLM_VRAM_THRESHOLD_MB` | `2000` | Minimum free VRAM |
| `VLM_COOLDOWN_SECONDS` | `5` | Cooldown between tasks |
| `VLM_MANUAL_OVERRIDE` | `false` | Kill switch |
| `VLM_MAX_IMAGE_DIMENSION` | `1344` | Max resize dimension |
| `VLM_JPEG_QUALITY` | `85` | JPEG quality for preprocessing |
| `VLM_BIND_HOST` | `0.0.0.0` | Bind address |
| `VLM_BIND_PORT` | `8100` | Bind port |
