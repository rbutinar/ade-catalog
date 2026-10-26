"""
Authentication layer for ade-catalog.

Uses Supabase Auth for user management, login, and JWT verification.
Only active when db_backend == "supabase".

Usage:
    from ade_catalog.core.auth import get_supabase_client, login_required

    # In Flask routes:
    @login_required
    def protected_route():
        user_id = g.user_id
        org_id = g.org_id
"""

import logging
from functools import wraps
from typing import Optional

from flask import g, redirect, request, session, url_for

logger = logging.getLogger(__name__)

_supabase_client = None


def get_supabase_client(config):
    """Get or create a Supabase client instance."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        _supabase_client = create_client(
            config.supabase.url,
            config.supabase.service_key or config.supabase.anon_key,
        )
    return _supabase_client


def login_required(f):
    """Decorator to protect routes. Redirects to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def get_user_orgs(config, user_id: str) -> list[dict]:
    """Get organizations the user belongs to."""
    from ade_catalog.core.db_backend import get_backend
    backend = get_backend(config)
    return backend.query("""
        SELECT o.org_id, o.org_name, o.org_slug, m.role
        FROM auth_meta.organizations o
        JOIN auth_meta.user_org_memberships m ON o.org_id = m.org_id
        WHERE m.user_id = %s AND m.is_active = TRUE AND o.is_active = TRUE
        ORDER BY o.org_name
    """, (user_id,))


def get_or_create_personal_org(config, user_id: str, email: str) -> dict:
    """Ensure a user has at least one organization (personal)."""
    orgs = get_user_orgs(config, user_id)
    if orgs:
        return orgs[0]

    from ade_catalog.core.db_backend import get_backend
    backend = get_backend(config)

    # Create personal org
    slug = email.split("@")[0].lower().replace(".", "-")
    rows = backend.execute_returning("""
        INSERT INTO auth_meta.organizations (org_name, org_slug, plan)
        VALUES (%s, %s, 'free')
        RETURNING org_id, org_name, org_slug
    """, (f"{email}'s Catalog", slug))

    if rows:
        org = rows[0]
        # Add user as owner
        backend.execute("""
            INSERT INTO auth_meta.user_org_memberships (user_id, org_id, role)
            VALUES (%s, %s, 'owner')
        """, (user_id, org["org_id"]))

        # Create default view for the org
        backend.execute_returning("""
            INSERT INTO catalog_meta.catalog_views (org_id, view_name, description, view_type, is_default)
            VALUES (%s, 'Default', 'Default catalog view', 'manual', TRUE)
            RETURNING view_id
        """, (org["org_id"],))

        return {"org_id": org["org_id"], "org_name": org["org_name"],
                "org_slug": org["org_slug"], "role": "owner"}

    return {}
