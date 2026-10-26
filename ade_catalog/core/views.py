"""
Catalog views management.

Views are navigable perspectives on the catalog data. Every piece of
imported metadata belongs to exactly one view (via view_id FK).
Views are scoped to organizations for multi-tenancy.
"""

import logging
from typing import Optional

from ade_catalog.core.config import CatalogConfig
from ade_catalog.core.db_backend import get_backend

logger = logging.getLogger(__name__)


def get_all_views(config: CatalogConfig, org_id: str = None) -> list[dict]:
    """Return all active views, optionally filtered by org. Default first."""
    backend = get_backend(config)
    if org_id:
        return backend.query("""
            SELECT view_id, view_name, description, view_type,
                   is_default, created_at, created_by
            FROM catalog_meta.catalog_views
            WHERE is_active = TRUE AND org_id = %s
            ORDER BY is_default DESC, view_name
        """, (org_id,))
    # Super admin or no org filter — show all
    return backend.query("""
        SELECT view_id, view_name, description, view_type,
               is_default, created_at, created_by
        FROM catalog_meta.catalog_views
        WHERE is_active = TRUE
        ORDER BY is_default DESC, view_name
    """)


def get_default_view_id(config: CatalogConfig, org_id: str = None) -> Optional[int]:
    """Return the default view_id for an org. Returns None if no views exist.

    Uses the request-scoped active backend (``g.org_backend``) when one is set
    (e.g. a per-request local-org override, or a SaaS org with its own SQL
    Server), falling back to the default backend otherwise — so this resolves
    the default view from the SAME database the caller is actually about to
    query, not always the process's default one.
    """
    from ade_catalog.core.db_backend import get_active_backend
    backend = get_active_backend(config)
    if org_id:
        rows = backend.query("""
            SELECT view_id FROM catalog_meta.catalog_views
            WHERE is_default = TRUE AND is_active = TRUE AND org_id = %s
            LIMIT 1
        """, (org_id,))
        if rows:
            return rows[0]["view_id"]
        # No default for this org — return first view
        rows = backend.query("""
            SELECT view_id FROM catalog_meta.catalog_views
            WHERE is_active = TRUE AND org_id = %s
            ORDER BY view_id LIMIT 1
        """, (org_id,))
        return rows[0]["view_id"] if rows else None

    # No org filter — return any default
    rows = backend.query("""
        SELECT view_id FROM catalog_meta.catalog_views
        WHERE is_default = TRUE AND is_active = TRUE
        LIMIT 1
    """)
    return rows[0]["view_id"] if rows else None


def get_or_create_view(
    config: CatalogConfig,
    view_name: str,
    org_id: str = None,
    view_type: str = "manual",
    description: str = "",
) -> int:
    """Get existing view by name (within org) or create a new one. Returns view_id."""
    backend = get_backend(config)

    if org_id:
        rows = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s AND org_id = %s",
            (view_name, org_id)
        )
    else:
        rows = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s",
            (view_name,)
        )

    if rows:
        return rows[0]["view_id"]

    result = backend.execute_returning("""
        INSERT INTO catalog_meta.catalog_views
            (view_name, description, view_type, is_default, org_id)
        VALUES (%s, %s, %s, FALSE, %s)
        RETURNING view_id
    """, (view_name, description, view_type, org_id))
    return result[0]["view_id"]
