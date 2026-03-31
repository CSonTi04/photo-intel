# Photo Intelligence & Digest System — Architecture

## Overview

A self-hosted, extensible photo processing pipeline that ingests images,
extracts structured information via OCR and Vision LLMs, and produces daily digests.

## Architecture Diagram

```mermaid
graph TB
    subgraph "1 — Ingest Layer"
        FS[Filesystem Watcher / Batch Scanner]
        FS -->|new file| REG[Media Registrar]
        REG -->|content_hash + metadata| DB[(PostgreSQL)]
    end

    subgraph "2 — Task Planning Layer"
        TP[Task Planner]
        TD[(task_definition table)]
        TP -->|reads| TD
        TP -->|creates task_instance| DB
    end

    subgraph "3 — Queue / Orchestration Layer"
        Q[Postgres-backed Queue]
        Q -->|lease task| W1
        Q -->|lease task| W2
        Q -->|lease task| W3
        DLQ[Dead Letter Queue]
        Q -->|max retries exceeded| DLQ
    end

    subgraph "4 — Execution Layer"
        W1[Worker: CPU Tasks<br/>exif, thumbnail, ocr]
        W2[Worker: VLM Tasks<br/>caption, actionability]
        W3[Worker: Digest Tasks<br/>daily, resurface]
    end

    subgraph "5 — VLM Wrapper Service (GPU Node)"
        GW[VLM Gateway API]
        GW -->|readiness check| RC[Readiness Controller]
        GW -->|inference| OL[Ollama / vLLM]
        GW -->|prompt mgmt| PM[Prompt Manager]
        RC -->|VRAM check| GPU[GPU: 4070 Ti]
    end

    subgraph "6 — Presentation Layer"
        DG[Digest Generator]
        DG -->|reads| DB
        DG -->|writes| DR[(digest_run / digest_item)]
        API[FastAPI Admin/API]
        API -->|reads| DB
    end

    REG --> TP
    W1 -->|writes results| DB
    W2 -->|HTTP POST| GW
    W2 -->|writes results| DB
    W3 -->|reads task_outputs| DB
    W3 -->|writes digest| DB
```

## Component Flow (Sequence)

```mermaid
sequenceDiagram
    participant FS as Filesystem
    participant ING as Ingest Worker
    participant DB as PostgreSQL
    participant TP as Task Planner
    participant Q as Queue
    participant CW as CPU Worker
    participant VW as VLM Worker
    participant GW as VLM Wrapper (GPU)
    participant DG as Digest Generator

    FS->>ING: New image detected
    ING->>DB: INSERT media_item (hash, metadata)
    ING->>TP: Trigger planning
    TP->>DB: Read task_definitions (enabled)
    TP->>DB: INSERT task_instances (pending)

    loop CPU Tasks (exif, thumbnail, ocr)
        Q->>CW: Lease task
        CW->>DB: Read media_item
        CW->>DB: Write task_output
        CW->>Q: Mark completed / enqueue follow-ups
    end

    loop VLM Tasks (caption, actionability, memory)
        Q->>VW: Lease task
        VW->>GW: GET /ready-for-vlm
        alt GPU Ready
            VW->>GW: POST /run-task/{task_type}
            GW-->>VW: JSON result
            VW->>DB: Write task_output
        else GPU Not Ready
            VW->>Q: Reschedule (available_at += backoff)
        end
    end

    DG->>DB: Query completed tasks for date range
    DG->>DB: Write digest_run + digest_items
```

## Task State Machine

```mermaid
stateDiagram-v2
    [*] --> discovered: Task Planner creates
    discovered --> pending: Prerequisites met
    pending --> leased: Worker claims
    leased --> completed: Success
    leased --> failed: Error (retryable)
    failed --> pending: Retry (attempts < max)
    failed --> dead_letter: Max retries exceeded
    dead_letter --> pending: Manual retry
    completed --> [*]
```

## Layer Responsibilities

### 1. Ingest Layer
- Watches configured directories (inotify / polling)
- Computes SHA-256 content hash for deduplication
- Extracts basic file metadata (size, MIME type, timestamps)
- Classifies media_kind (photo vs screenshot) via heuristics
- Registers `media_item` in database
- Triggers Task Planner for new items
- **Does NOT perform heavy processing**

### 2. Task Planning Layer
- Reads `task_definition` table for enabled tasks
- Evaluates prerequisite conditions per task type
- Creates `task_instance` records with proper priority
- Computes `input_hash` for idempotency
- Handles task versioning (re-plans if version changes)

### 3. Queue / Orchestration Layer
- Postgres-based queue with `SELECT ... FOR UPDATE SKIP LOCKED`
- Supports: leasing, retries with exponential backoff, scheduling
- Dead Letter Queue for permanently failed tasks
- Future: RabbitMQ adapter with dead-letter exchange

### 4. Execution Layer
- Workers are typed (cpu, vlm, digest)
- Each worker: lease → execute → write output → ack/nack
- Tasks read inputs from DB, write outputs to DB
- Tasks may enqueue follow-up tasks (never call directly)
- GPU unavailability = graceful reschedule, not failure

### 5. VLM Wrapper Service
- Runs on GPU node (4070 Ti)
- HTTP API wrapping Ollama/vLLM
- Readiness gating (VRAM, cooldown, manual override)
- Prompt management per task type
- Image preprocessing (resize, quality)
- Output validation against JSON schema

### 6. Presentation Layer
- Daily digest: summarizes today's processed images
- Resurface digest: surfaces older interesting content
- Future: search UI, admin dashboard, DLQ inspector

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Queue backend | Postgres (initial) | No extra infra, SKIP LOCKED is robust |
| Task identity | (media_id, type, version, input_hash) | Ensures idempotency |
| VLM access | HTTP wrapper, not direct Ollama | Decoupling, readiness gating, caching |
| Worker types | Separate processes per type | Independent scaling, fault isolation |
| Config | Static (env/YAML) + Dynamic (DB) | Flexibility without restarts |
| Image processing | Per-task configurable | Screenshots vs photos need different handling |

## Networking

- All services communicate over LAN
- No public exposure required
- Future: Tailscale for cross-network access
- VLM Wrapper binds to LAN IP, authenticated via shared secret
