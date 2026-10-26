"""
MCP (Model Context Protocol) HTTP endpoint.

Exposes ade-catalog tools via MCP Streamable HTTP transport.
Any MCP-compatible client (Claude Desktop, Agent SDK, etc.) can connect.

Endpoint: POST /mcp
Auth: Bearer token (API key per org) or Supabase JWT
"""

import json
import logging
import uuid

from flask import Blueprint, current_app, g, jsonify, request

bp = Blueprint("mcp", __name__, url_prefix="/mcp")

logger = logging.getLogger(__name__)

# MCP protocol version
MCP_VERSION = "2024-11-05"


def _authenticate():
    """Validate the request and return config + view_id.

    Supports:
    - Bearer token from Supabase JWT
    - X-API-Key header (for simple API key auth)
    """
    config = current_app.config["CATALOG"]

    # Local-first single-tenant deployments run without the SaaS auth layer.
    # No browser session here (headless MCP client), so there's no cookie to
    # carry an org choice the way the web UI's org-switcher dropdown does.
    # Let a headless caller pick a LOCAL org explicitly per-request instead —
    # via ?org=<id> or the X-Local-Org header — so querying e.g. Contoso via MCP
    # doesn't require restarting the whole server against a different DB.
    # Falls back to the server's own default DB when no org is given.
    if getattr(config, "local_mode", False):
        from ade_catalog.core.views import get_default_view_id

        org_param = request.args.get("org") or request.headers.get("X-Local-Org")
        if org_param and getattr(config, "organizations", None):
            org = next(
                (o for o in config.organizations
                 if str(o.get("id")).lower() == org_param.lower()
                 or str(o.get("name", "")).lower() == org_param.lower()),
                None,
            )
            if not org:
                return None, None, f"Unknown local org '{org_param}'"
            from ade_catalog.core.db_backend import get_local_org_backend
            try:
                g.org_backend = get_local_org_backend(org, config)
            except Exception as e:
                return None, None, f"Could not connect to local org '{org_param}': {e}"

        try:
            view_id = get_default_view_id(config)
        except Exception:
            view_id = None
        return config, view_id, None

    # SaaS: authenticate via X-API-Key (long-lived, org-bound) or a Supabase
    # Bearer JWT, resolve the caller's org -> view, and bind the DB-layer RLS
    # identity for this request (TICK-018 / TICK-015 co-gate).
    from ade_catalog.core.views import get_default_view_id
    from ade_catalog.mcp_server.auth import authenticate_mcp

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
    else:
        bearer = auth_header.strip()  # tolerate clients that send the raw token
    api_key = request.headers.get("X-API-Key", "")
    requested_org = request.args.get("org") or request.headers.get("X-Org")

    user_id, org_id, is_super, error = authenticate_mcp(
        config, bearer_token=bearer, api_key=api_key, requested_org=requested_org)
    if error:
        return None, None, error

    # Bind the RLS identity BEFORE any query so the per-request connection is
    # created under SET ROLE authenticated. Super-admin stays on postgres/bypass.
    g.db_auth_uid = None if is_super else user_id

    # Resolve the org's default view for query-level scoping (defense in depth
    # alongside RLS). Runs on the now-scoped connection when a uid was bound.
    view_id = None
    if org_id:
        try:
            view_id = get_default_view_id(config, org_id=org_id)
        except Exception:
            view_id = None
    return config, view_id, None


def _make_jsonrpc_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _make_jsonrpc_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


@bp.route("", methods=["POST"])
@bp.route("/", methods=["POST"])
def handle_mcp():
    """Handle MCP JSON-RPC requests."""
    config, view_id, auth_error = _authenticate()
    if auth_error:
        return jsonify(_make_jsonrpc_error(None, -32000, auth_error)), 401

    try:
        body = request.get_json()
    except Exception:
        return jsonify(_make_jsonrpc_error(None, -32700, "Parse error")), 400

    if not body:
        return jsonify(_make_jsonrpc_error(None, -32600, "Invalid request")), 400

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # Handle MCP methods
    if method == "initialize":
        return jsonify(_make_jsonrpc_response(req_id, {
            "protocolVersion": MCP_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "ade-catalog",
                "version": "2.0.0",
            },
        }))

    elif method == "notifications/initialized":
        # Client acknowledgment, no response needed
        return "", 204

    elif method == "tools/list":
        from ade_catalog.mcp_server.tools import TOOLS
        return jsonify(_make_jsonrpc_response(req_id, {
            "tools": TOOLS,
        }))

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        from ade_catalog.mcp_server.tools import execute_tool
        try:
            result = execute_tool(tool_name, arguments, config, view_id)
            return jsonify(_make_jsonrpc_response(req_id, {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2, default=str),
                    }
                ],
            }))
        except Exception as e:
            logger.exception("Tool execution failed: %s", tool_name)
            return jsonify(_make_jsonrpc_response(req_id, {
                "content": [
                    {"type": "text", "text": f"Error: {e}"},
                ],
                "isError": True,
            }))

    elif method == "ping":
        return jsonify(_make_jsonrpc_response(req_id, {}))

    else:
        return jsonify(_make_jsonrpc_error(req_id, -32601, f"Method not found: {method}"))


# ---------------------------------------------------------------------------
# Discovery endpoint (GET) — for clients to find the MCP server
# ---------------------------------------------------------------------------

@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
def mcp_info():
    """Return MCP server info for discovery."""
    return jsonify({
        "name": "ade-catalog",
        "version": "2.0.0",
        "protocol": "mcp",
        "protocolVersion": MCP_VERSION,
        "transport": "streamable-http",
        "endpoint": "/mcp",
        "tools": [t["name"] for t in _get_tools()],
    })


def _get_tools():
    from ade_catalog.mcp_server.tools import TOOLS
    return TOOLS
