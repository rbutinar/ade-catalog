-- ============================================================================
-- ade-catalog - Catalog Meta Schema (SQLite / public self-host edition)
-- ============================================================================
-- Purpose: Core catalog infrastructure — navigable views.
-- The SqliteBackend ATTACHes this file AS catalog_meta, so `catalog_meta.x`
-- schema-qualified queries (written for Postgres / SQL Server) run unchanged.
--
-- Public-edition simplifications vs the Supabase/SQL Server schema:
--   * No cross-schema FK to auth_meta.organizations — the public edition is
--     local single-org (no SaaS control plane). org_id stays as a nullable
--     column for shape parity with the publish target.
--   * BOOLEAN -> INTEGER (SQLite has no bool type; TRUE/FALSE map to 1/0).
--   * TIMESTAMPTZ DEFAULT NOW() -> TEXT DEFAULT CURRENT_TIMESTAMP.
-- ============================================================================

CREATE TABLE IF NOT EXISTS catalog_meta.catalog_views (
    view_id      INTEGER PRIMARY KEY,
    view_name    TEXT NOT NULL UNIQUE,
    description  TEXT,
    view_type    TEXT NOT NULL DEFAULT 'manual'
                 CHECK (view_type IN ('manual', 'scan', 'snapshot')),

    org_id       TEXT,

    is_default   INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,

    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by   TEXT
);

-- Seed a default view so freshly built catalogs have a home.
INSERT INTO catalog_meta.catalog_views (view_name, description, view_type, is_default)
VALUES ('Default', 'Default catalog view', 'manual', 1)
ON CONFLICT (view_name) DO NOTHING;
