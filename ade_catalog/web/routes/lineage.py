"""Lineage viewer — pick an item, choose direction + depth, see the graph.

Mirrors the v1 Streamlit "Object Details > Lineage" UX (linear: select an
object, view its connections at a chosen depth) but server-rendered in Flask,
reusing the MCP get_lineage tool and the shared Mermaid renderer.
"""

import json

from flask import Blueprint, current_app, g, jsonify, render_template, request

from ade_catalog.core.db_backend import get_active_backend
from ade_catalog.mcp_server.tools import execute_tool

bp = Blueprint("lineage", __name__, url_prefix="/lineage")

_DIRECTIONS = ("both", "downstream", "upstream")


def _node_dict(nid, name, ntype, platform):
    return {"id": nid, "label": name, "type": ntype, "platform": platform}


@bp.route("/explore")
def explore():
    """Interactive graph explorer (Cytoscape): class-count nodes, expand on demand."""
    return render_template("lineage_explore.html", query=(request.args.get("q") or "").strip(), mode="explore")


@bp.route("/api/attribute-schema")
def api_attribute_schema():
    """Per-node-type attribute descriptors (label + semantic type) — the single
    source of truth for how the UI renders a node's properties."""
    from ade_catalog.core.attribute_schema import get_attribute_schema
    return jsonify(get_attribute_schema())


@bp.route("/api/find")
def api_find():
    """Return the candidate nodes matching a name (exact matches first), so the
    UI can disambiguate when several objects share/contain the search term."""
    config = current_app.config["CATALOG"]
    q = (request.args.get("q") or "").strip()
    view_id = getattr(g, "view_id", None)
    if not q:
        return jsonify({"candidates": []})
    backend = get_active_backend(config)
    rows = backend.query(
        "SELECT node_id, display_name, node_type, platform FROM lineage_meta.nodes "
        "WHERE display_name ILIKE %s AND (view_id = %s OR %s IS NULL) "
        "ORDER BY CASE WHEN display_name = %s THEN 0 ELSE 1 END, display_name LIMIT 20",
        (f"%{q}%", view_id, view_id, q),
    )
    return jsonify({"candidates": [
        _node_dict(r["node_id"], r["display_name"], r["node_type"], r["platform"]) for r in rows
    ]})


@bp.route("/api/node/<int:node_id>")
def api_node(node_id: int):
    """Return a single node's attributes + properties (for the details panel)."""
    config = current_app.config["CATALOG"]
    backend = get_active_backend(config)
    rows = backend.query(
        "SELECT node_id, display_name, node_type, platform, qualified_name, external_id, properties "
        "FROM lineage_meta.nodes WHERE node_id = %s",
        (node_id,),
    )
    if not rows:
        return jsonify({"error": "not found"})
    r = rows[0]
    props = r.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (ValueError, TypeError):
            props = {}
    return jsonify({
        "id": r["node_id"], "label": r["display_name"], "type": r["node_type"],
        "platform": r["platform"], "qualified_name": r.get("qualified_name"),
        "external_id": r.get("external_id"),
        # Route through the node detail page: it redirects to the rich platform-meta
        # page when the org has that inventory, else renders the lineage-native view.
        "detail_url": f"/detail/node/{r['node_id']}",
        "properties": props if isinstance(props, dict) else {},
    })


# node_type (+ platform) -> the detail route that renders its full card.
_DETAIL_ROUTE = {
    "report": "report", "dataset": "dataset", "measure": "measure",
    "semantic_table": "pbi_table", "page": "page", "visual": "visual",
}


def _detail_url(node_type: str, platform: str, external_id) -> str:
    """Deep-link to the object's full detail page, when we know its source id."""
    if not external_id:
        return None
    if node_type == "notebook":
        route = "fab_notebook" if platform == "fabric" else "notebook"
    else:
        route = _DETAIL_ROUTE.get(node_type)
    if not route:
        return None
    try:
        return f"/detail/{route}/{int(external_id)}"
    except (TypeError, ValueError):
        return None


@bp.route("/api/class-neighbors", methods=["POST"])
def api_class_neighbors():
    """Aggregate navigation: given the member node ids of a class-in-context,
    return the CONNECTED CLASSES (neighbour types) with counts + members, so the
    user can keep navigating class-by-class without materializing instances."""
    config = current_app.config["CATALOG"]
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    try:
        ids = [int(i) for i in ids][:2000]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return jsonify({"groups": []})
    backend = get_active_backend(config)
    ph = ",".join(["%s"] * len(ids))
    rows = backend.query(
        f"""
        SELECT e.source_node_id AS sid, e.target_node_id AS tid, e.relationship_type AS rel,
               ns.display_name s_name, ns.node_type s_type, ns.platform s_plat,
               nt.display_name t_name, nt.node_type t_type, nt.platform t_plat
        FROM lineage_meta.edges e
        JOIN lineage_meta.nodes ns ON e.source_node_id = ns.node_id
        JOIN lineage_meta.nodes nt ON e.target_node_id = nt.node_id
        WHERE e.source_node_id IN ({ph}) OR e.target_node_id IN ({ph})
        """,
        tuple(ids) + tuple(ids),
    )
    idset = set(ids)
    groups: dict = {}
    seen: dict = {}
    for r in rows:
        if r["sid"] in idset and r["tid"] not in idset:
            direction = "downstream"
            nb = {"id": r["tid"], "label": r["t_name"], "type": r["t_type"], "platform": r["t_plat"], "rel": r["rel"]}
        elif r["tid"] in idset and r["sid"] not in idset:
            direction = "upstream"
            nb = {"id": r["sid"], "label": r["s_name"], "type": r["s_type"], "platform": r["s_plat"], "rel": r["rel"]}
        else:
            continue  # intra-class edge
        key = (direction, nb["type"] or "unknown")
        s = seen.setdefault(key, set())
        if nb["id"] in s:
            continue
        s.add(nb["id"])
        groups.setdefault(key, []).append(nb)
    return jsonify({"groups": [
        {"direction": d, "type": t, "count": len(m), "members": m}
        for (d, t), m in groups.items()
    ]})


@bp.route("/api/reachable", methods=["POST"])
def api_reachable():
    """Multi-hop reachability: from seed node ids, traverse edges in a direction
    up to max_depth and return the reachable CLASSES (types) with their min hop
    distance + members. Lets the UI offer 'jump to an indirectly-connected type'
    (skipping intermediates) as an on-demand query."""
    config = current_app.config["CATALOG"]
    body = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in body.get("ids", [])][:200]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return jsonify({"groups": []})
    direction = body.get("direction", "both")
    try:
        max_depth = max(2, min(6, int(body.get("max_depth", 4))))
    except (TypeError, ValueError):
        max_depth = 4
    # used_only: follow only data-flow edges (feeds/produces), so the result is
    # what a KPI actually CONSUMES — not what's merely reachable via model
    # relationships (relates_to) or containment (belongs_to).
    used_only = bool(body.get("used_only"))
    rel_filter = "AND e.relationship_type IN ('feeds', 'produces')" if used_only else ""
    backend = get_active_backend(config)
    ph = ",".join(["%s"] * len(ids))

    def traverse(cur_col, next_col):
        sql = f"""
        WITH RECURSIVE reach(nid, depth) AS (
            SELECT node_id, 0 FROM lineage_meta.nodes WHERE node_id IN ({ph})
            UNION ALL
            SELECT e.{next_col}, r.depth + 1
            FROM reach r
            JOIN lineage_meta.edges e ON e.{cur_col} = r.nid
            WHERE r.depth < %s {rel_filter}
        )
        SELECT r.nid AS nid, MIN(r.depth) AS depth,
               n.node_type AS node_type, n.display_name AS display_name, n.platform AS platform
        FROM reach r
        JOIN lineage_meta.nodes n ON n.node_id = r.nid
        GROUP BY r.nid, n.node_type, n.display_name, n.platform
        """
        try:
            return backend.query(sql, tuple(ids) + (max_depth,))
        except Exception:
            return []

    dirs = []
    if direction in ("both", "downstream"):
        dirs.append(("downstream", "source_node_id", "target_node_id"))
    if direction in ("both", "upstream"):
        dirs.append(("upstream", "target_node_id", "source_node_id"))

    idset = set(ids)
    groups: dict = {}
    for dname, cur_col, next_col in dirs:
        for r in traverse(cur_col, next_col):
            if r["nid"] in idset:
                continue  # skip the seeds themselves (depth 0)
            key = (dname, r["node_type"] or "unknown")
            g = groups.setdefault(key, {"direction": dname, "type": key[1], "min_depth": r["depth"], "members": {}})
            g["min_depth"] = min(g["min_depth"], r["depth"])
            g["members"][r["nid"]] = {
                "id": r["nid"], "label": r["display_name"], "type": r["node_type"], "platform": r["platform"],
            }
    return jsonify({"groups": [
        {"direction": g["direction"], "type": g["type"], "min_depth": g["min_depth"],
         "count": len(g["members"]), "members": list(g["members"].values())[:500]}
        for g in groups.values()
    ]})


# A transform-bearing node carries its logic under one of these property keys;
# the value is the transformation text shown at that step of the trail.
_TRANSFORM_KEYS = ("dax_expression", "code", "sql", "view_definition", "transformation")
_TRANSFORM_LABEL = {
    "dax_expression": "DAX", "code": "Code", "sql": "SQL",
    "view_definition": "SQL", "transformation": "Transformation",
}


@bp.route("/api/transform-trail/<int:node_id>")
def api_transform_trail(node_id: int):
    """The transformation logic along the upstream lineage path of a node — how the
    data is transformed at each step (DAX / code / SQL), ordered from nearest to
    the sources. Follows only data-flow edges (feeds/produces)."""
    config = current_app.config["CATALOG"]
    backend = get_active_backend(config)
    try:
        rows = backend.query(
            """
            WITH RECURSIVE reach(nid, depth) AS (
                SELECT %s, 0
                UNION ALL
                SELECT e.source_node_id, r.depth + 1
                FROM reach r
                JOIN lineage_meta.edges e ON e.target_node_id = r.nid
                WHERE r.depth < 8 AND e.relationship_type IN ('feeds', 'produces')
            )
            SELECT r.nid AS nid, MIN(r.depth) AS depth,
                   n.display_name AS display_name, n.node_type AS node_type,
                   n.platform AS platform, n.properties AS properties
            FROM reach r
            JOIN lineage_meta.nodes n ON n.node_id = r.nid
            GROUP BY r.nid, n.display_name, n.node_type, n.platform, n.properties
            """,
            (node_id,),
        )
    except Exception as exc:
        return jsonify({"steps": [], "error": str(exc)})

    steps = []
    for r in rows:
        # keep the seed too: if it is itself a transform (a measure/notebook), its
        # own logic is step 0 — "this KPI's formula, then what feeds it".
        props = r.get("properties")
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (ValueError, TypeError):
                props = {}
        if not isinstance(props, dict):
            continue
        key = next((k for k in _TRANSFORM_KEYS if props.get(k)), None)
        if not key:
            continue  # a pass-through data asset, not a transform step
        steps.append({
            "depth": r["depth"], "label": r["display_name"], "type": r["node_type"],
            "platform": r["platform"], "transform_type": _TRANSFORM_LABEL.get(key, "Transformation"),
            "logic": str(props[key]),
        })
    steps.sort(key=lambda s: s["depth"])
    return jsonify({"steps": steps})


@bp.route("/api/neighbors/<int:node_id>")
def api_neighbors(node_id: int):
    """Return a node's immediate neighbours grouped by (direction, type) with counts."""
    config = current_app.config["CATALOG"]
    backend = get_active_backend(config)
    rows = backend.query(
        """
        SELECT e.source_node_id AS sid, e.target_node_id AS tid,
               e.relationship_type AS rel,
               ns.display_name AS s_name, ns.node_type AS s_type, ns.platform AS s_plat,
               nt.display_name AS t_name, nt.node_type AS t_type, nt.platform AS t_plat
        FROM lineage_meta.edges e
        JOIN lineage_meta.nodes ns ON e.source_node_id = ns.node_id
        JOIN lineage_meta.nodes nt ON e.target_node_id = nt.node_id
        WHERE e.source_node_id = %s OR e.target_node_id = %s
        """,
        (node_id, node_id),
    )
    groups: dict = {}
    for r in rows:
        if r["sid"] == node_id:
            direction = "downstream"
            nb = _node_dict(r["tid"], r["t_name"], r["t_type"], r["t_plat"])
        else:
            direction = "upstream"
            nb = _node_dict(r["sid"], r["s_name"], r["s_type"], r["s_plat"])
        nb["rel"] = r["rel"]
        key = (direction, nb["type"] or "unknown")
        groups.setdefault(key, []).append(nb)

    out = [
        {"direction": d, "type": t, "count": len(members), "members": members}
        for (d, t), members in groups.items()
    ]
    return jsonify({"groups": out})


@bp.route("/")
def view():
    config = current_app.config["CATALOG"]
    query = (request.args.get("q") or "").strip()
    direction = request.args.get("direction", "both")
    if direction not in _DIRECTIONS:
        direction = "both"
    try:
        depth = max(1, min(10, int(request.args.get("depth", 3))))
    except (TypeError, ValueError):
        depth = 3

    result = None
    error = None
    if query:
        try:
            result = execute_tool(
                "get_lineage",
                {"node_name": query, "direction": direction, "max_depth": depth},
                config,
                getattr(g, "view_id", None),
            )
            if result.get("error"):
                error = result["error"]
                result = None
        except Exception as exc:  # surface DB/dialect issues rather than 500
            error = str(exc)

    return render_template(
        "lineage.html",
        query=query,
        direction=direction,
        depth=depth,
        directions=_DIRECTIONS,
        result=result,
        error=error,
        mode="classic",
    )
