-- ============================================================
-- society_db v2 schema - redesign-2026-05 Phase 2
-- Run once: psql -d society_db -f db/schema_v2.sql
--
-- Coexists with the v1 schema. v2 introduces:
--   - posts_v2: fixed core columns + JSONB extra (Schema-aware Agent
--               proposes which fields go into extra; never ALTER TABLE)
--   - simhash bigint for post-level dedup (Hamming distance <= 3)
--   - tsvector + pg_trgm for full-text fallback search
--   - schema_meta: schema fingerprint + per-column descriptions
--                  (mirrored to Chroma 2; see scripts/rebuild_chroma2_schema.py)
--   - topics_v2: post-level cluster summaries
--   - entities_v2: extracted PERSON/ORG/LOC/EVENT entities
--   - reflection_log: audit trail for Critic verdicts (Phase 5 reads)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- posts_v2 -------------------------------------------------------
CREATE TABLE IF NOT EXISTS posts_v2 (
    post_id           TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    author            TEXT NOT NULL,        -- human-readable author handle
    text              TEXT NOT NULL,        -- compressed / merged text
    posted_at         TIMESTAMPTZ,
    subreddit         TEXT,                 -- nullable for non-Reddit sources
    source            TEXT NOT NULL DEFAULT 'reddit',  -- reddit | telegram | x | fixture
    topic_id          TEXT,                 -- assigned by post-level clusterer
    dominant_emotion  TEXT,                 -- fear | anger | hope | disgust | neutral
    emotion_score     REAL DEFAULT 0.0,
    -- Engagement metrics: stable across platforms, indexable, queryable.
    like_count        INTEGER NOT NULL DEFAULT 0,
    reply_count       INTEGER NOT NULL DEFAULT 0,
    retweet_count     INTEGER NOT NULL DEFAULT 0,
    simhash           BIGINT,               -- 64-bit simhash for dedup
    text_tsv          tsvector,             -- generated below
    extra             JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Generated tsvector column maintained by trigger to avoid PG version
-- compatibility issues with GENERATED ALWAYS AS columns.
CREATE OR REPLACE FUNCTION posts_v2_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.text_tsv := to_tsvector('english', COALESCE(NEW.text, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS posts_v2_tsv_update ON posts_v2;
CREATE TRIGGER posts_v2_tsv_update
    BEFORE INSERT OR UPDATE OF text ON posts_v2
    FOR EACH ROW EXECUTE FUNCTION posts_v2_tsv_trigger();

CREATE INDEX IF NOT EXISTS idx_posts_v2_topic       ON posts_v2(topic_id);
CREATE INDEX IF NOT EXISTS idx_posts_v2_author      ON posts_v2(author);
CREATE INDEX IF NOT EXISTS idx_posts_v2_posted_at   ON posts_v2(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_v2_simhash     ON posts_v2(simhash);
CREATE INDEX IF NOT EXISTS idx_posts_v2_emotion     ON posts_v2(dominant_emotion);
CREATE INDEX IF NOT EXISTS idx_posts_v2_like_count   ON posts_v2(like_count DESC);
CREATE INDEX IF NOT EXISTS idx_posts_v2_extra_gin   ON posts_v2 USING GIN (extra jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_posts_v2_text_tsv    ON posts_v2 USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_posts_v2_text_trgm   ON posts_v2 USING GIN (text gin_trgm_ops);

-- ---------- topics_v2 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics_v2 (
    topic_id          TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    post_count        INTEGER NOT NULL DEFAULT 0,
    dominant_emotion  TEXT,
    centroid_text     TEXT,         -- representative post text
    extra             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- entities_v2 ----------------------------------------------------
CREATE TABLE IF NOT EXISTS entities_v2 (
    entity_id         TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    entity_type       TEXT NOT NULL CHECK (entity_type IN
                          ('PERSON','ORG','LOC','EVENT','OTHER')),
    mention_count     INTEGER NOT NULL DEFAULT 1,
    extra             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_v2_name_type
    ON entities_v2 (LOWER(name), entity_type);

CREATE TABLE IF NOT EXISTS post_entities_v2 (
    post_id           TEXT NOT NULL REFERENCES posts_v2(post_id) ON DELETE CASCADE,
    entity_id         TEXT NOT NULL REFERENCES entities_v2(entity_id) ON DELETE CASCADE,
    char_start        INTEGER,
    char_end          INTEGER,
    confidence        REAL DEFAULT 0.5,
    PRIMARY KEY (post_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_post_entities_v2_entity
    ON post_entities_v2(entity_id);

-- ---------- schema_meta ----------------------------------------------------
-- Per-column descriptions and fingerprints. Mirrored to Chroma 2 by
-- agents/schema_agent.py (see Phase 2 5b double-write contract).
CREATE TABLE IF NOT EXISTS schema_meta (
    id                SERIAL PRIMARY KEY,
    table_name        TEXT NOT NULL,
    column_name       TEXT NOT NULL,
    column_type       TEXT NOT NULL,
    description       TEXT NOT NULL,
    sample_values     JSONB NOT NULL DEFAULT '[]'::jsonb,
    fingerprint       TEXT NOT NULL,     -- sha256(table.column.type)
    in_extra          BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (table_name, column_name)
);
CREATE INDEX IF NOT EXISTS idx_schema_meta_table ON schema_meta(table_name);

-- ---------- reflection_log -------------------------------------------------
-- Phase 5 audit table for Critic verdicts. Errors also routed to
-- Chroma 2 / Chroma 3 by services/reflection_store.py.
CREATE TABLE IF NOT EXISTS reflection_log (
    id                BIGSERIAL PRIMARY KEY,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id        TEXT,
    user_message      TEXT,
    error_kind        TEXT,
    failed_branch     TEXT,
    causal_record_ids TEXT[],
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_reflection_kind ON reflection_log(error_kind);
CREATE INDEX IF NOT EXISTS idx_reflection_time ON reflection_log(occurred_at DESC);
