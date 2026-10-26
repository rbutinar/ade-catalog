"""stdio MCP transport for the public / local self-host edition (TICK-022 ACT-008).

v1 parity: put `python -m ade_catalog.mcp_server.stdio` in a `.mcp.json` and Claude
Desktop "just works" — no server to keep running, no port, no auth, offline.

Reuses the SAME `TOOLS` + `execute_tool` as the HTTP transport (web/routes/mcp.py):
one tool implementation, the invariant surface, cannot drift. Local no-auth — the
scope is the default catalog view.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout. Diagnostics go to
stderr ONLY (stdout is the protocol channel; a stray print there corrupts it).
"""

import json
import sys

MCP_VERSION = "2024-11-05"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def handle(body: dict, config, view_id):
    """Return a JSON-RPC response dict, or None for a notification (no reply)."""
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {}) or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ade-catalog", "version": "0.4.0-preview"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        from ade_catalog.mcp_server.tools import TOOLS
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        from ade_catalog.mcp_server.tools import execute_tool
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        try:
            result = execute_tool(name, arguments, config, view_id)
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, indent=2, default=str)}],
            }}
        except Exception as e:  # a tool error is a result, not a transport error
            _log(f"tool {name!r} failed: {e}")
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }}
    # Unknown method: error for a request, silence for a notification.
    if req_id is not None:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def main() -> int:
    from ade_catalog.core.config import load_config
    from ade_catalog.core.views import get_default_view_id

    config = load_config()
    try:
        view_id = get_default_view_id(config)
    except Exception:
        view_id = None
    _log(f"ade-catalog MCP (stdio) ready — backend={getattr(config, 'db_backend', '?')}, "
         f"view={view_id}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            _log("skipping non-JSON line")
            continue
        resp = handle(body, config, view_id)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
