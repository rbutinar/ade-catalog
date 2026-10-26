"""Authentication for the hosted MCP endpoint (TICK-018).

Two credential kinds, one org-scoping rule shared with the web:
- **X-API-Key**: a long-lived key (adatta a client MCP persistenti come Claude
  Desktop). Only its sha256 is stored, in ``auth_meta.mcp_api_keys``, mapped to a
  ``(user_id, org_id)``. This is the primary path for delivery colleagues.
- **Bearer JWT**: a Supabase access token (reuses the web identity). Validated
  against ``SUPABASE_JWT_SECRET``; ``sub`` is the user, super-admin from
  ``user_metadata.role``.

``authenticate_mcp`` returns ``(user_id, org_id, is_super, error)``. The caller
(``web/routes/mcp.py``) sets ``g.db_auth_uid`` from it so the per-request
connection binds RLS, and resolves the org's view_id for query-level scoping.

Lookups use the backend's *ephemeral* connection: they must run as the base
postgres role (read ``mcp_api_keys`` under its deny-all RLS) and must NOT create
the request's reused connection before its RLS identity is known.
"""

import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)

_API_KEY_PREFIX = "adecat_"   # human-recognisable, namespaced
_PREFIX_STORE_LEN = 12        # leading chars kept in the clear for display


def hash_api_key(plaintext: str) -> str:
    """sha256 hex of an API key (what we store / look up by)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Mint a new API key. Returns ``(plaintext, key_prefix, key_hash)``.

    The plaintext is shown to the operator ONCE; only prefix + hash are stored.
    """
    plaintext = _API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:_PREFIX_STORE_LEN], hash_api_key(plaintext)


def _resolve_org_from_membership(backend, user_id: str, requested_org: str | None):
    """Pick the org for a JWT caller: the requested org if the user is an active
    member, else their first active org. Returns (org_id | None)."""
    rows = backend.query_ephemeral(
        "SELECT org_id FROM auth_meta.user_org_memberships "
        "WHERE user_id = %s AND is_active = TRUE ORDER BY created_at",
        (user_id,),
    )
    org_ids = [str(r["org_id"]) for r in rows]
    if not org_ids:
        return None
    if requested_org and requested_org in org_ids:
        return requested_org
    return org_ids[0]


def authenticate_mcp(config, *, bearer_token: str = "", api_key: str = "",
                     requested_org: str | None = None):
    """Validate an MCP caller. Returns ``(user_id, org_id, is_super, error)``.

    On success ``error`` is None. On failure the identity fields are None and
    ``error`` is a human-readable string (the caller returns 401).
    """
    from ade_catalog.core.db_backend import get_backend
    backend = get_backend(config)

    # ---- API key (primary: long-lived, org-bound) ----
    if api_key:
        try:
            rows = backend.query_ephemeral(
                "SELECT user_id, org_id FROM auth_meta.mcp_api_keys "
                "WHERE key_hash = %s AND is_active = TRUE",
                (hash_api_key(api_key),),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP api-key lookup failed: %s", e)
            return None, None, False, "authentication backend error"
        if not rows:
            return None, None, False, "invalid API key"
        row = rows[0]
        try:
            backend.execute_ephemeral(
                "UPDATE auth_meta.mcp_api_keys SET last_used_at = now() WHERE key_hash = %s",
                (hash_api_key(api_key),),
            )
        except Exception:  # best-effort; never block auth on the touch
            pass
        # API keys are org-bound by construction; never super.
        return str(row["user_id"]), str(row["org_id"]), False, None

    # ---- Bearer JWT (Supabase access token) ----
    if bearer_token:
        secret = getattr(config.supabase, "jwt_secret", "") or ""
        if not secret:
            return None, None, False, "server not configured for JWT auth (SUPABASE_JWT_SECRET missing)"
        try:
            import jwt
            payload = jwt.decode(
                bearer_token, secret, algorithms=["HS256"], audience="authenticated"
            )
        except Exception as e:  # noqa: BLE001 - PyJWT raises subclasses of Exception
            return None, None, False, f"invalid token: {e}"
        user_id = payload.get("sub")
        if not user_id:
            return None, None, False, "token missing subject"
        meta = payload.get("user_metadata") or {}
        is_super = meta.get("role") == "super_admin"
        if is_super:
            # Super sees everything (mirrors the web '__all__'); no single org.
            return str(user_id), None, True, None
        try:
            org_id = _resolve_org_from_membership(backend, user_id, requested_org)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP membership lookup failed: %s", e)
            return None, None, False, "authentication backend error"
        if not org_id:
            return None, None, False, "no active org membership for this user"
        return str(user_id), org_id, False, None

    return None, None, False, "missing authentication (provide X-API-Key or Bearer token)"
