# Photo Intelligence — MVP Implementation Plan

## Phase Overview

| Phase | Focus | Duration | Dependencies |
|-------|-------|----------|-------------|
| **Phase 0** | Infra setup | 1-2 days | Docker, Postgres |
| **Phase 1** | Ingest + CPU tasks | 3-5 days | Phase 0 |
| **Phase 2** | VLM pipeline | 3-5 days | Phase 1 + GPU node |
| **Phase 3** | Digest + observability | 2-3 days | Phase 2 |
| **Phase 4** | Hardening + backfill | 2-3 days | Phase 3 |

**Total estimated: 2-3 weeks**

---

## Phase 0: Infrastructure Setup

### Tasks
1. **Postgres instance**
   - `docker compose up postgres`
   - Run `migrations/001_initial_schema.sql`
   - Verify tables + seed data

2. **Project skeleton**
   - Install dependencies: `pip install -e .`
   - Verify imports work
   - Run test suite: `pytest tests/`

3. **GPU node setup**
   - Install Ollama on 4070 Ti machine
   - Pull model: `ollama pull llava:13b`
   - Test inference: `curl http://gpu-node:11434/api/generate ...`
   - Deploy VLM Wrapper: `docker compose -f docker-compose-gpu.yml up -d`

### Verification
- [ ] `psql` connects to Postgres, tables exist
- [ ] `pytest` passes all unit tests
- [ ] `curl http://gpu-node:8100/ready-for-vlm` returns `{"ready": true}`

---

## Phase 1: Ingest + CPU Tasks

### Tasks
1. **Batch scanner**
   - Point to a test directory with ~100 images
   - Run: `photo-intel scan --dirs /path/to/test-images`
   - Verify media_items in DB

2. **Task planning**
   - Run: `photo-intel ingest --dirs /path/to/test-images`
   - Verify task_instances created per media_item
   - Check prerequisite handling (discovered vs pending states)

3. **CPU Worker: extract_exif**
   - Start worker: `photo-intel worker --type cpu`
   - Verify EXIF data extracted and stored in media_exif
   - Check task_output records

4. **CPU Worker: generate_thumbnail**
   - Verify thumbnails created in output directory
   - Check multiple sizes (256, 512)

5. **CPU Worker: ocr_full**
   - Test with screenshot images
   - Verify full_text stored in media_ocr
   - Check confidence scores

6. **CPU Worker: ocr_entities**
   - Verify entity extraction after OCR completes
   - Check prerequisite chain: ocr_full → ocr_entities

### Verification
- [ ] 100 images scanned, deduplicated, classified
- [ ] Each image has appropriate task_instances
- [ ] EXIF, thumbnails, OCR complete for applicable items
- [ ] Prerequisite chain works (ocr_entities waits for ocr_full)
- [ ] Queue stats show correct state transitions

---

## Phase 2: VLM Pipeline

### Tasks
1. **VLM Wrapper deployment**
   - Deploy on GPU node
   - Test readiness endpoint
   - Test with sample image via curl

2. **VLM Worker: vlm_caption**
   - Start VLM worker: `photo-intel worker --type vlm`
   - Verify captions generated for photos
   - Check JSON output parsing

3. **VLM Worker: vlm_actionability**
   - Test with screenshot images
   - Verify actionable item detection

4. **VLM Worker: vlm_memory_summary**
   - Test with various image types
   - Verify memory relevance scoring

5. **GPU unavailability handling**
   - Stop Ollama, verify graceful reschedule
   - Enable manual override, verify tasks wait
   - Resume, verify tasks complete

### Verification
- [ ] VLM Wrapper responds correctly to all endpoints
- [ ] Captions generated with valid JSON
- [ ] Actionability detection identifies real action items
- [ ] GPU down → tasks reschedule (not fail permanently)
- [ ] Processing metrics recorded

---

## Phase 3: Digest + Observability

### Tasks
1. **Daily digest generator**
   - Run: `photo-intel digest --date 2024-03-15`
   - Verify sections: highlights, actionable, screenshots, photos
   - Check scoring and ranking

2. **API endpoints**
   - Test all endpoints via curl/httpx
   - `/stats`, `/media`, `/media/{id}`, `/dlq`, `/metrics/processing`
   - Verify DLQ retry mechanism

3. **Structured logging**
   - Verify structlog output format
   - Check JSON log mode for production

4. **Processing metrics**
   - Verify duration tracking per task
   - Check `/metrics/processing` endpoint

### Verification
- [ ] Daily digest generates with correct sections
- [ ] API returns accurate stats
- [ ] DLQ retry resets task to pending
- [ ] Logs are structured and queryable

---

## Phase 4: Hardening + Backfill

### Tasks
1. **Large-scale backfill test**
   - Run against 10k+ image archive
   - Monitor memory usage, DB connections, disk space
   - Verify resumability (kill + restart mid-batch)

2. **Crash recovery**
   - Kill worker mid-task, verify lease expiry + reclaim
   - Kill API mid-request, verify no data corruption
   - Simulate DB disconnect, verify reconnection

3. **Idempotency verification**
   - Re-run ingest on same directory, verify 0 new registrations
   - Re-run scan + plan, verify no duplicate tasks
   - Re-run completed tasks, verify no duplicate outputs

4. **Performance tuning**
   - Tune batch sizes for ingest (100-1000)
   - Tune OCR concurrency (2-4)
   - Tune VLM batch size (5-10)
   - Tune Postgres connection pool

### Verification
- [ ] 10k+ images processed without OOM or deadlocks
- [ ] Crash recovery works: no stuck leases after restart
- [ ] Full idempotency: same input → same state
- [ ] Acceptable throughput: 1000 images/hour (CPU), 50 images/hour (VLM)

---

## Future Phases (post-MVP)

### Phase 5: Search + Embeddings
- Add pgvector extension
- Generate image embeddings (CLIP)
- Semantic search endpoint

### Phase 6: UI Dashboard
- Next.js or SvelteKit dashboard
- Image gallery with task outputs
- Digest viewer
- DLQ inspector

### Phase 7: Advanced Features
- Face grouping (clustering)
- Topic clustering
- Multi-model routing (different models for different tasks)
- RabbitMQ migration
- Tailscale networking

---

## Quick Start (tl;dr)

```bash
# 1. Clone + setup
cp .env.example .env
# Edit .env with your paths and GPU node IP

# 2. Start infrastructure
docker compose up -d postgres
psql -h localhost -U photo_intel -d photo_intel -f migrations/001_initial_schema.sql

# 3. GPU node (separate machine)
docker compose -f docker-compose-gpu.yml up -d

# 4. Test ingest
photo-intel scan --dirs /path/to/photos

# 5. Start workers
photo-intel worker --type cpu &
photo-intel worker --type vlm &
photo-intel worker --type maintenance &

# 6. Start API
photo-intel api

# 7. Check status
curl http://localhost:8000/stats
curl http://localhost:8000/media?limit=10
```
