-- ============================================================================
-- EMR Helper — database schema
--
-- This file is applied automatically the first time the `db` container starts
-- on an empty data directory (it's mounted into /docker-entrypoint-initdb.d
-- via docker-compose.yml). To re-apply after edits, drop the pgdata volume:
--
--     docker compose down
--     docker volume rm emr_helper_pgdata
--     docker compose up -d db
--
-- All statements are idempotent (`IF NOT EXISTS`) so re-running by hand is
-- safe, but the entrypoint-initdb hook only fires on a fresh data dir.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
-- pgvector: vector(N) column type + cosine/L2/inner-product distance operators
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm: trigram similarity for fuzzy matching of exact UI labels
-- (e.g. matching "+New" or "Save to Queue" inside a user query).
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ---------------------------------------------------------------------------
-- documents — one row per ingested manual
-- ---------------------------------------------------------------------------
-- Re-ingesting the same (title, manual_version) is idempotent via the unique
-- constraint: ingestion path looks up existing doc, marks its chunks
-- active=false, then inserts new ones. Old chunks are kept (not deleted)
-- so historical conversations referencing them still resolve.
CREATE TABLE IF NOT EXISTS documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title           text NOT NULL,
    manual_version  text NOT NULL,
    source_path     text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (title, manual_version)
);


-- ---------------------------------------------------------------------------
-- chunks — one row per searchable unit
-- ---------------------------------------------------------------------------
-- This is THE critical table. Every retrieval query hits it.
--
-- Chunk kinds:
--   procedure      — one per H3 heading (the main content)
--   index_entry    — one per "How do I...?" quick-index bullet (vocabulary anchors)
--   glossary       — one per glossary term (definitions)
--   section_intro  — prose paragraphs directly under H1/H2 (conceptual overviews)
--
-- The `embedding` column is bge-m3 output (1024-dim). The `tsv` column is a
-- generated tsvector over the searchable text fields — Postgres maintains it
-- automatically on insert/update. The `ui_labels` column is plain text used
-- only by the trigram index for fuzzy UI-element matching.
--
-- Versioning: `active=false` marks superseded chunks from previous ingest
-- runs. Active retrieval queries filter on `active=true`.
CREATE TABLE IF NOT EXISTS chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id          uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    kind            text NOT NULL
                    CHECK (kind IN ('procedure', 'index_entry', 'glossary', 'section_intro')),

    -- Human-readable breadcrumb, e.g.
    --   "Section 1: Patient registration > Queued Patients tab > Close a clinic session"
    section_path    text NOT NULL,

    -- Display title of the chunk. For procedures: the H3 text. For glossary:
    -- the term. For index entries: the procedure name the entry points to.
    title           text NOT NULL,

    -- The italic "When to use:" line under each procedure heading.
    -- NULL for non-procedure chunks.
    when_to_use     text,

    -- Body content: steps list (procedures), definition (glossary),
    -- question text (index_entry), intro paragraph (section_intro).
    content         text NOT NULL,

    -- Concatenated `Figure:` captions for screenshots attached to this chunk.
    -- Separator between captions is " || " so the tokenizer sees clear breaks.
    -- This is the load-bearing text representation of the chunk's screenshots
    -- (per project policy: no image embeddings, captions are the bridge).
    image_captions  text NOT NULL DEFAULT '',

    -- [{"path": "data/images/.../fig_3.png", "caption": "...", "order": 3}, ...]
    -- Returned by retrieval and rendered inline in the chat response.
    images          jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- Space-separated exact UI labels extracted from bold runs and image
    -- captions. Powers the trigram index for queries like "+New button" or
    -- "Save to Queue". Kept distinct from `content` so trigram noise doesn't
    -- bleed into BM25 ranking.
    ui_labels       text NOT NULL DEFAULT '',

    -- Concatenated Caution callouts (each ending with newline). Surfaced
    -- prominently in answer generation when present.
    cautions        text NOT NULL DEFAULT '',

    -- Concatenated Note callouts (informational, less urgent than cautions).
    notes           text NOT NULL DEFAULT '',

    -- Optional page-range hints (NULL if unknown — .docx may not have pages).
    page_start      int,
    page_end        int,

    -- Denormalised copy of documents.manual_version for fast version-scoped
    -- filtering without a JOIN on every retrieval.
    manual_version  text NOT NULL,

    -- false = superseded by a newer ingest of the same (title, manual_version).
    active          boolean NOT NULL DEFAULT true,

    -- bge-m3 embedding. Dimension must match EMBEDDING_DIM in app/config.py
    -- and the EMBEDDING_MODEL .env value. Changing the model means dropping
    -- this column type and re-ingesting everything.
    embedding       vector(1024),

    -- Postgres-maintained full-text search vector with FIELD WEIGHTING.
    -- Without weights, a procedure that mentions "register" three times in
    -- its body content can outrank a procedure literally titled "Register
    -- a patient". setweight() assigns labels A/B/C/D to slices of the
    -- tsvector; ts_rank_cd then uses default weights {0.1, 0.2, 0.4, 1.0}
    -- for {D, C, B, A} respectively. Title gets A (1.0×), when_to_use
    -- gets B (0.4×), content gets C (0.2×), image_captions gets D (0.1×).
    tsv             tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')),          'A') ||
                        setweight(to_tsvector('english', coalesce(when_to_use, '')),    'B') ||
                        setweight(to_tsvector('english', coalesce(content, '')),        'C') ||
                        setweight(to_tsvector('english', coalesce(image_captions, '')), 'D')
                    ) STORED,

    created_at      timestamptz NOT NULL DEFAULT now()
);

-- HNSW vector index for fast cosine-similarity search.
-- HNSW is preferred over ivfflat for this corpus size (~2000 chunks): better
-- recall and no requirement to build with representative data.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- GIN index on the full-text search vector for BM25-style ranking via ts_rank.
CREATE INDEX IF NOT EXISTS chunks_tsv_gin
    ON chunks USING gin (tsv);

-- GIN trigram index for fuzzy matching on exact UI labels.
-- Powers queries like "what does +New do" or "where is the Save to Queue button".
CREATE INDEX IF NOT EXISTS chunks_ui_labels_trgm
    ON chunks USING gin (ui_labels gin_trgm_ops);

-- Hot-path filter: every retrieval query restricts to active + version.
CREATE INDEX IF NOT EXISTS chunks_version_active
    ON chunks (manual_version, active);


-- ---------------------------------------------------------------------------
-- conversations — one row per chat session
-- ---------------------------------------------------------------------------
-- user_id is the value of the X-User header set by Caddy after basic auth
-- (or by an OIDC reverse proxy later). For local-only deployments without
-- auth, this column may be a constant like "anonymous".
CREATE TABLE IF NOT EXISTS conversations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          text NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    last_message_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_user
    ON conversations (user_id, started_at DESC);


-- ---------------------------------------------------------------------------
-- messages — one row per user turn or assistant turn
-- ---------------------------------------------------------------------------
-- retrieved_chunk_ids: list of chunk UUIDs (as text in JSON) that the
-- generator's prompt was built from. Used by the audit and feedback paths
-- to reconstruct exactly what context produced an answer.
CREATE TABLE IF NOT EXISTS messages (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id       uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                  text NOT NULL CHECK (role IN ('user', 'assistant')),
    content               text NOT NULL,
    retrieved_chunk_ids   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation
    ON messages (conversation_id, created_at);


-- ---------------------------------------------------------------------------
-- feedback — thumbs up / thumbs down per assistant message
-- ---------------------------------------------------------------------------
-- One row per rating action; a user could rate the same message twice
-- (e.g. correct themselves). Latest row wins in any UI aggregation.
CREATE TABLE IF NOT EXISTS feedback (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    rating      text NOT NULL CHECK (rating IN ('up', 'down')),
    comment     text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feedback_message
    ON feedback (message_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- audit_log — every query, every ingest run, every failure
-- ---------------------------------------------------------------------------
-- user_id stored as the FIRST 16 HEX CHARS of sha256(raw_user_id). Raw user
-- identifiers must never reach this table.
-- payload jsonb shape varies by event_type:
--   query   → {query, retrieved_chunk_ids, latency_ms, model, ...}
--   ingest  → {doc_id, source_path, n_chunks, duration_ms, ...}
--   error   → {where, message, stacktrace?, ...}
CREATE TABLE IF NOT EXISTS audit_log (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     text NOT NULL,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_event_type
    ON audit_log (event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS audit_log_user
    ON audit_log (user_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- One-shot sanity check on first apply: log that the schema was created.
-- ---------------------------------------------------------------------------
INSERT INTO audit_log (user_id, event_type, payload)
VALUES ('system', 'schema_applied', jsonb_build_object(
    'schema_version', 'phase-2-initial',
    'applied_at', now()
));
