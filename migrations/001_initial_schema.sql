-- Photo Intelligence System — Initial Schema
-- PostgreSQL 15+

-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- for future text search

-- ============================================================
-- ENUM Types
-- ============================================================
CREATE TYPE media_kind AS ENUM ('photo', 'screenshot', 'unknown');
CREATE TYPE task_state AS ENUM ('discovered', 'pending', 'leased', 'completed', 'failed', 'dead_letter');
CREATE TYPE digest_type AS ENUM ('daily', 'resurface');

-- ============================================================
-- media_item
-- ============================================================
CREATE TABLE media_item (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_hash    VARCHAR(64) NOT NULL,
    file_path       TEXT NOT NULL,
    source          VARCHAR(128) NOT NULL DEFAULT 'filesystem',
    captured_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mime_type       VARCHAR(64),
    width           INTEGER,
    height          INTEGER,
    file_size       BIGINT,
    media_kind      media_kind NOT NULL DEFAULT 'unknown',
    metadata_json   JSONB DEFAULT '{}'::jsonb,

    CONSTRAINT uq_media_content_hash UNIQUE (content_hash)
);

CREATE INDEX idx_media_item_source ON media_item (source);
CREATE INDEX idx_media_item_captured_at ON media_item (captured_at);
CREATE INDEX idx_media_item_created_at ON media_item (created_at);
CREATE INDEX idx_media_item_media_kind ON media_item (media_kind);

-- ============================================================
-- media_exif
-- ============================================================
CREATE TABLE media_exif (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    media_item_id   UUID NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
    exif_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_media_exif_item UNIQUE (media_item_id)
);

-- ============================================================
-- media_ocr
-- ============================================================
CREATE TABLE media_ocr (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    media_item_id           UUID NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
    engine                  VARCHAR(64) NOT NULL,
    engine_version          VARCHAR(32) NOT NULL,
    full_text               TEXT,
    structured_blocks_json  JSONB DEFAULT '[]'::jsonb,
    confidence              REAL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_media_ocr_engine UNIQUE (media_item_id, engine, engine_version)
);

CREATE INDEX idx_media_ocr_fulltext ON media_ocr USING gin (to_tsvector('english', full_text));

-- ============================================================
-- task_definition
-- ============================================================
CREATE TABLE task_definition (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_type       VARCHAR(64) NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    config_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    prompt_template TEXT,
    prompt_version  INTEGER NOT NULL DEFAULT 1,
    output_schema   JSONB,
    prerequisites   JSONB DEFAULT '[]'::jsonb,  -- list of task_type strings
    applies_to      JSONB DEFAULT '["photo", "screenshot"]'::jsonb,  -- media_kind filter
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    priority        INTEGER NOT NULL DEFAULT 100,  -- lower = higher priority
    max_retries     INTEGER NOT NULL DEFAULT 3,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_task_def_type_version UNIQUE (task_type, version)
);

-- ============================================================
-- task_instance
-- ============================================================
CREATE TABLE task_instance (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    media_item_id   UUID NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
    task_type       VARCHAR(64) NOT NULL,
    task_version    INTEGER NOT NULL,
    state           task_state NOT NULL DEFAULT 'discovered',
    priority        INTEGER NOT NULL DEFAULT 100,
    available_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until     TIMESTAMPTZ,
    leased_by       VARCHAR(128),
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    input_hash      VARCHAR(64) NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,

    CONSTRAINT uq_task_idempotency UNIQUE (media_item_id, task_type, task_version, input_hash)
);

CREATE INDEX idx_task_instance_queue ON task_instance (state, priority, available_at)
    WHERE state IN ('pending', 'discovered');
CREATE INDEX idx_task_instance_media ON task_instance (media_item_id);
CREATE INDEX idx_task_instance_type ON task_instance (task_type);
CREATE INDEX idx_task_instance_leased ON task_instance (lease_until)
    WHERE state = 'leased';

-- ============================================================
-- task_output
-- ============================================================
CREATE TABLE task_output (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_instance_id    UUID NOT NULL REFERENCES task_instance(id) ON DELETE CASCADE,
    output_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary_text        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_task_output_instance UNIQUE (task_instance_id)
);

-- ============================================================
-- dead_letter_task
-- ============================================================
CREATE TABLE dead_letter_task (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_instance_id    UUID NOT NULL REFERENCES task_instance(id) ON DELETE CASCADE,
    error_type          VARCHAR(128),
    error_message       TEXT,
    payload_json        JSONB DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_dlq_instance UNIQUE (task_instance_id)
);

-- ============================================================
-- digest_run
-- ============================================================
CREATE TABLE digest_run (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    digest_type             digest_type NOT NULL,
    target_date             DATE NOT NULL,
    config_snapshot_json    JSONB DEFAULT '{}'::jsonb,
    status                  VARCHAR(32) NOT NULL DEFAULT 'pending',
    total_items             INTEGER DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ,

    CONSTRAINT uq_digest_run_type_date UNIQUE (digest_type, target_date)
);

-- ============================================================
-- digest_item
-- ============================================================
CREATE TABLE digest_item (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    digest_run_id   UUID NOT NULL REFERENCES digest_run(id) ON DELETE CASCADE,
    media_item_id   UUID NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
    section         VARCHAR(64) NOT NULL,
    rank_score      REAL DEFAULT 0.0,
    summary_text    TEXT,
    metadata_json   JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_digest_item_run ON digest_item (digest_run_id);
CREATE INDEX idx_digest_item_section ON digest_item (section);

-- ============================================================
-- Processing metrics / observability
-- ============================================================
CREATE TABLE processing_metric (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_instance_id UUID REFERENCES task_instance(id) ON DELETE SET NULL,
    worker_id       VARCHAR(128),
    task_type       VARCHAR(64),
    duration_ms     INTEGER,
    success         BOOLEAN,
    metadata_json   JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_processing_metric_task_type ON processing_metric (task_type, created_at);

-- ============================================================
-- Trigger: auto-update updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_media_item_updated_at
    BEFORE UPDATE ON media_item
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_task_instance_updated_at
    BEFORE UPDATE ON task_instance
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_task_definition_updated_at
    BEFORE UPDATE ON task_definition
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- Seed: Default task definitions
-- ============================================================
INSERT INTO task_definition (task_type, version, config_json, priority, max_retries, timeout_seconds, prerequisites, applies_to) VALUES
('extract_exif',       1, '{"extract_gps": true, "extract_camera": true}'::jsonb,                    10, 2, 60,   '[]'::jsonb, '["photo"]'::jsonb),
('generate_thumbnail', 1, '{"sizes": [256, 512], "format": "webp", "quality": 80}'::jsonb,           20, 2, 120,  '[]'::jsonb, '["photo", "screenshot"]'::jsonb),
('ocr_full',           1, '{"engine": "tesseract", "lang": "eng+hun", "dpi": 300}'::jsonb,           30, 3, 180,  '[]'::jsonb, '["screenshot"]'::jsonb),
('ocr_entities',       1, '{"detect": ["dates", "urls", "prices", "addresses", "emails"]}'::jsonb,   40, 3, 120,  '["ocr_full"]'::jsonb, '["screenshot"]'::jsonb),
('vlm_caption',        1, '{"model": "llava:13b", "max_tokens": 256}'::jsonb,                        50, 3, 300,  '["generate_thumbnail"]'::jsonb, '["photo", "screenshot"]'::jsonb),
('vlm_actionability',  1, '{"model": "llava:13b", "max_tokens": 512}'::jsonb,                        60, 3, 300,  '["ocr_full"]'::jsonb, '["screenshot"]'::jsonb),
('vlm_memory_summary', 1, '{"model": "llava:13b", "max_tokens": 512}'::jsonb,                        70, 3, 300,  '["vlm_caption"]'::jsonb, '["photo", "screenshot"]'::jsonb);

-- Add prompt templates
UPDATE task_definition SET prompt_template = 'Describe this image in 1-2 sentences. Include: scene type, main subjects, notable details. Respond in JSON: {"caption": "...", "scene_category": "...", "subjects": [...], "mood": "..."}'
WHERE task_type = 'vlm_caption';

UPDATE task_definition SET prompt_template = 'Analyze this screenshot for actionable information. Is there anything the user should act on (deadline, reminder, price, booking, appointment)? Respond in JSON: {"is_actionable": bool, "action_items": [...], "urgency": "none|low|medium|high", "category": "...", "reasoning": "..."}'
WHERE task_type = 'vlm_actionability';

UPDATE task_definition SET prompt_template = 'Summarize what is worth remembering from this image for the user''s personal knowledge base. Consider: people present, places shown, events captured, information displayed. Respond in JSON: {"memory_relevance": "none|low|medium|high", "summary": "...", "people_hints": [...], "place_hints": [...], "time_hints": [...], "tags": [...]}'
WHERE task_type = 'vlm_memory_summary';
