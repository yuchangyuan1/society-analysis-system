-- ============================================================
-- society_db — Postgres schema
-- Run once: psql -d society_db -f db/schema.sql
-- ============================================================

-- ---------- accounts --------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT,
    verified      BOOLEAN DEFAULT FALSE,
    followers     INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- posts -----------------------------------------------------
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    account_id    TEXT REFERENCES accounts(id),
    text          TEXT NOT NULL,
    lang          TEXT DEFAULT 'en',
    retweet_count INTEGER DEFAULT 0,
    like_count    INTEGER DEFAULT 0,
    reply_count   INTEGER DEFAULT 0,
    has_image     BOOLEAN DEFAULT FALSE,
    posted_at     TIMESTAMPTZ,
    ingested_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- images ----------------------------------------------------
CREATE TABLE IF NOT EXISTS images (
    id            TEXT PRIMARY KEY,
    post_id       TEXT REFERENCES posts(id),
    url           TEXT,
    local_path    TEXT,
    ocr_text      TEXT,
    image_caption TEXT,
    image_type    TEXT,   -- screenshot | chart | meme | photo
    embedding_id  TEXT,
    processed_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- claims ----------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    id                TEXT PRIMARY KEY,
    normalized_text   TEXT NOT NULL,
    first_seen_post   TEXT REFERENCES posts(id),
    propagation_count INTEGER DEFAULT 1,
    risk_level        TEXT,   -- LOW | MEDIUM | HIGH | CRITICAL
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- post_claims (many-to-many) --------------------------------
CREATE TABLE IF NOT EXISTS post_claims (
    post_id  TEXT REFERENCES posts(id),
    claim_id TEXT REFERENCES claims(id),
    PRIMARY KEY (post_id, claim_id)
);

-- ---------- articles / fact-checks ------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id           TEXT PRIMARY KEY,
    url          TEXT,
    title        TEXT,
    body_text    TEXT,
    source       TEXT,
    published_at TIMESTAMPTZ,
    ingested_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- claim_evidence --------------------------------------------
CREATE TABLE IF NOT EXISTS claim_evidence (
    id          SERIAL PRIMARY KEY,
    claim_id    TEXT REFERENCES claims(id),
    article_id  TEXT REFERENCES articles(id),
    stance      TEXT NOT NULL CHECK (stance IN ('supports','contradicts','neutral')),
    snippet     TEXT
);

-- ---------- reports ---------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
    id                TEXT PRIMARY KEY,
    intent_type       TEXT NOT NULL,
    query_text        TEXT,
    risk_level        TEXT,
    requires_review   BOOLEAN DEFAULT FALSE,
    propagation_json  JSONB,
    counter_message   TEXT,
    visual_card_path  TEXT,
    report_md         TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- run_logs --------------------------------------------------
CREATE TABLE IF NOT EXISTS run_logs (
    id          SERIAL PRIMARY KEY,
    report_id   TEXT REFERENCES reports(id),
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok','degraded','error','blocked')),
    detail      TEXT,
    logged_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- indexes ---------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_posts_account       ON posts(account_id);
CREATE INDEX IF NOT EXISTS idx_post_claims_claim   ON post_claims(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_run_logs_report     ON run_logs(report_id);
