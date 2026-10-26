"""Enrichment store — generic AI/human descriptions keyed by
(view_id, object_class, object_id) where object_id is the lineage node_id.

See ADR 2026-07-01 (Phase 3) and schemas/sqlserver/08_enrichment_meta.sql. The
store is backend-agnostic (SQL Server + Supabase via core.db_backend); SQL uses
Postgres-style %s placeholders, which the SQL Server backend translates.

Provenance rules:
- The ``enrich`` LLM pass writes source='llm', is_human_edited=0 + a source_hash.
- A human edit (web) writes source='human', is_human_edited=1.
- Re-running the pass must NOT overwrite a human-edited row (the pass checks
  is_human_edited before calling save_llm_enrichment; save_human_edit preserves
  the original generated_at/source_hash).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

_FIELDS = (
    "enrichment_id, view_id, object_class, object_id, qualified_name, "
    "business_description, technical_description, source, ai_model, source_hash, "
    "is_human_edited, generated_at, updated_at, updated_by"
)


def compute_source_hash(payload) -> str:
    """Stable hash of the metadata that produced an enrichment (regen guard)."""
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_enrichment(backend, view_id, object_class, object_id):
    """Fetch one enrichment row (dict) or None."""
    rows = backend.query(
        f"SELECT {_FIELDS} FROM enrichment_meta.enrichments "
        "WHERE view_id = %s AND object_class = %s AND object_id = %s",
        (view_id, str(object_class), str(object_id)),
    )
    return rows[0] if rows else None


def fetch(config, view_id, object_class, object_id):
    """Web-path read: resolve the request/org backend and never raise (the card
    is optional — a missing enrichment_meta schema must not break a detail page)."""
    if view_id is None or object_id is None:
        return None
    from ade_catalog.core.db_backend import get_active_backend
    try:
        return get_enrichment(get_active_backend(config), view_id, object_class, object_id)
    except Exception:
        return None


def _upsert(backend, view_id, object_class, object_id, qualified_name, *,
            business, technical, source, ai_model, source_hash,
            is_human_edited, generated_at, updated_by):
    now = _utcnow()
    existing = get_enrichment(backend, view_id, object_class, object_id)
    if existing:
        backend.execute(
            "UPDATE enrichment_meta.enrichments SET "
            "qualified_name = %s, business_description = %s, technical_description = %s, "
            "source = %s, ai_model = %s, source_hash = %s, is_human_edited = %s, "
            "generated_at = %s, updated_at = %s, updated_by = %s "
            "WHERE view_id = %s AND object_class = %s AND object_id = %s",
            (qualified_name, business, technical, source, ai_model, source_hash,
             is_human_edited, generated_at, now, updated_by,
             view_id, str(object_class), str(object_id)),
        )
    else:
        backend.execute(
            "INSERT INTO enrichment_meta.enrichments "
            "(view_id, object_class, object_id, qualified_name, business_description, "
            "technical_description, source, ai_model, source_hash, is_human_edited, "
            "generated_at, updated_at, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (view_id, str(object_class), str(object_id), qualified_name, business, technical,
             source, ai_model, source_hash, is_human_edited, generated_at, now, updated_by),
        )
    return get_enrichment(backend, view_id, object_class, object_id)


def save_llm_enrichment(backend, view_id, object_class, object_id, qualified_name, *,
                        business, technical, model, source_hash):
    """Write an LLM-generated enrichment (provenance = llm)."""
    return _upsert(
        backend, view_id, object_class, object_id, qualified_name,
        business=business, technical=technical, source="llm", ai_model=model,
        source_hash=source_hash, is_human_edited=False, generated_at=_utcnow(),
        updated_by="enrich",
    )


def save_human_edit(backend, view_id, object_class, object_id, qualified_name, *,
                    business, technical, updated_by="user"):
    """Write a human edit (provenance = human, protected from re-scan overwrite).
    Preserves the original generated_at / ai_model / source_hash for reference."""
    existing = get_enrichment(backend, view_id, object_class, object_id) or {}
    return _upsert(
        backend, view_id, object_class, object_id, qualified_name,
        business=business, technical=technical, source="human",
        ai_model=existing.get("ai_model"), source_hash=existing.get("source_hash"),
        is_human_edited=True, generated_at=existing.get("generated_at"),
        updated_by=updated_by,
    )
