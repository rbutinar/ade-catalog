-- ============================================================================
-- ade-catalog - Enrichment Meta Schema (SQLite / public self-host edition)
-- ============================================================================
-- Generic, view-scoped AI/human descriptions, keyed by (view_id, object_class,
-- object_id) where object_id anchors on the lineage graph node. ATTACHed AS
-- enrichment_meta. No FK on view_id (mirrors lineage_meta.nodes). Hand-authored
-- from the SQL Server DDL (the supabase set applies enrichment via migration).
-- ============================================================================

CREATE TABLE IF NOT EXISTS enrichment_meta.enrichments (
    enrichment_id         INTEGER PRIMARY KEY,

    -- Scope + object identity
    view_id               INTEGER NOT NULL,
    object_class          TEXT NOT NULL,   -- report/measure/table/notebook/column/...
    object_id             TEXT NOT NULL,   -- lineage node_id (as string) — stable anchor
    qualified_name        TEXT,

    -- Content (two audiences)
    business_description  TEXT,
    technical_description TEXT,

    -- Provenance
    source                TEXT NOT NULL DEFAULT 'llm'
                          CHECK (source IN ('llm', 'human')),
    ai_model              TEXT,
    source_hash           TEXT,
    is_human_edited       INTEGER NOT NULL DEFAULT 0,

    generated_at          TEXT,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by            TEXT,

    UNIQUE (view_id, object_class, object_id)
);

CREATE INDEX IF NOT EXISTS enrichment_meta.idx_enrichment_view_class
    ON enrichments (view_id, object_class);
