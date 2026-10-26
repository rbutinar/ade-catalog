"""
MCP tools for ade-catalog.

Each tool is a function that takes validated parameters and returns results.
These are used by both the MCP server endpoint and can be called directly.
"""

import json
import logging
from typing import Optional

from ade_catalog.core.config import CatalogConfig
from ade_catalog.core.db_backend import get_active_backend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_catalog",
        "description": "Search across all catalog objects (tables, notebooks, pipelines, measures, datasets, warehouses, lakehouses). Returns matching objects with name, type, platform, and description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (e.g., 'sales', 'customer'). Empty string returns all objects.",
                },
                "platform": {
                    "type": "string",
                    "enum": [
                        "", "databricks", "powerbi", "fabric", "postgresql",
                        "cloudera", "hive", "tableau", "talend", "oracle",
                        "hdfs", "mysql", "cross_platform",
                    ],
                    "description": "Filter by platform. Empty for all platforms.",
                },
                "object_type": {
                    "type": "string",
                    "enum": [
                        "", "notebook", "table", "dataset", "pbi_table", "measure",
                        "report", "page", "visual", "relationship", "column",
                        "pipeline", "warehouse", "lakehouse", "fab_notebook",
                        "lh_table", "wh_table", "workspace", "job", "spark_job",
                        "view", "materialized_view", "schema", "database",
                        "workbook", "datasource", "worksheet", "calculated_field",
                        "source_table", "semantic_table", "data_product",
                    ],
                    "description": "Filter by object type. Empty for all types.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum TOTAL results (default 20). Results are fair-truncated round-robin across object types; the response carries total_found and truncated so you can tell when more matches exist.",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "get_object_detail",
        "description": "Get full details of a catalog object by type and ID. Returns the object plus its pertinent contour: for a report → its pages, each page's visuals, and the measures each visual uses (the report→page→visual→measure composition, so you don't hand-walk get_lineage); for a page/visual → the same one level down; for measure/table/etc. → metadata + bindings. On lineage-first orgs (data only in the graph) any node type — including hive/tableau/spark/talend/postgres nodes with no dedicated inventory — returns the node plus its immediate upstream/downstream neighbors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_type": {
                    "type": "string",
                    "enum": [
                        "notebook", "table", "dataset", "pbi_table", "measure",
                        "report", "page", "visual", "column", "pipeline",
                        "warehouse", "lakehouse", "fab_notebook", "lh_table",
                        "wh_table", "workspace", "job", "spark_job", "view",
                        "materialized_view", "schema", "database", "workbook",
                        "datasource", "worksheet", "calculated_field",
                        "source_table", "semantic_table", "data_product",
                    ],
                    "description": "Type of the object (as returned in a search_catalog / get_lineage result's type field).",
                },
                "object_id": {
                    "type": "integer",
                    "description": "ID of the object: the platform-meta id on inventory orgs, or the lineage node_id (from search_catalog / get_lineage) on lineage-first orgs. The tool tries inventory first, then falls back to the graph.",
                },
            },
            "required": ["object_type", "object_id"],
        },
    },
    {
        "name": "list_platforms",
        "description": "List all platforms that have data in the catalog, with object counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_dashboard_stats",
        "description": "Get overview statistics: counts of workspaces, notebooks, datasets, measures, pipelines, warehouses, lakehouses.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_lineage",
        "description": "Get lineage (upstream/downstream dependencies) for a node. Returns connected nodes and relationships. Prefer node_id (e.g. from a prior search_catalog result) when you have it — display names are not unique (the same measure/table name can exist in more than one dataset or environment), so a name-only lookup can silently resolve to the wrong node, or return a 'candidates' list instead of lineage if the name is ambiguous.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "integer",
                    "description": "Exact node id to trace lineage for (unambiguous — use this when you have it, e.g. from search_catalog's 'id' field).",
                },
                "node_name": {
                    "type": "string",
                    "description": "Display name of the node to trace lineage for. Only used when node_id is not given. If the name matches more than one node, the response returns a 'candidates' list instead of lineage — re-call with node_id set to the one you mean.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["upstream", "downstream", "both"],
                    "description": "Lineage direction (default: both).",
                    "default": "both",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum traversal depth (default: 3).",
                    "default": 3,
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "Cap on returned nodes (default 200, nearest-first by depth; 0 = unlimited). When the cap is hit the response carries truncated=true.",
                    "default": 200,
                },
                "relationship_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only traverse these relationship types (e.g. [\"feeds\", \"produces\"]). Applied during traversal, not post-hoc.",
                },
                "exclude_relationship_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Do not traverse these relationship types (e.g. [\"belongs_to\"] to skip containment noise such as a table's sibling columns).",
                },
                "node_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only traverse to nodes of these types (e.g. [\"measure\", \"table\"]). Applied during traversal, not post-hoc.",
                },
                "exclude_node_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Do not traverse to nodes of these types (e.g. [\"column\"] to drop containment noise — on a hub node this can cut the payload by ~80%). Applied during traversal, not post-hoc.",
                },
                "include_mermaid": {
                    "type": "boolean",
                    "description": "Include the mermaid diagram field (default true). Set false to slim the payload.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "list_views",
        "description": "List the catalog views (perspectives) defined in this organization's catalog, with id, name, default and active flags.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_graph_stats",
        "description": "Aggregate statistics of the lineage graph: node/edge totals, nodes by platform and by type, edges by relationship type, cross-platform edge pairs. Works on every org — including lineage-first ones where get_dashboard_stats inventory counts are empty. Scoped to the active view when one is set.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, arguments: dict, config: CatalogConfig, view_id: int = None) -> dict:
    """Execute a tool by name with given arguments. Returns result dict."""
    backend = get_active_backend(config)

    if tool_name == "search_catalog":
        # Delegate to the web search (single, resilient, SQL-Server + Supabase
        # compatible implementation) rather than the Postgres-only _search_catalog.
        from ade_catalog.core.queries import search_catalog as _web_search
        limit = arguments.get("limit", 20)
        results = _web_search(
            config,
            query=arguments.get("query", ""),
            platform=arguments.get("platform", ""),
            object_type=arguments.get("object_type", ""),
            view_id=view_id,
            limit=limit,
        )
        # queries.search_catalog applies `limit` PER OBJECT TYPE (the web browse
        # relies on that); the tool contract is a TOTAL cap — fair-truncate
        # round-robin across types so one populous type can't crowd out the rest.
        total_found = len(results)
        if limit and limit > 0 and total_found > limit:
            results = _fair_truncate(results, limit)
        return {
            "results": results,
            "count": len(results),
            "total_found": total_found,
            "truncated": len(results) < total_found,
        }
    elif tool_name == "get_object_detail":
        return _get_object_detail(backend, config, arguments)
    elif tool_name == "list_platforms":
        return _list_platforms(backend, view_id)
    elif tool_name == "get_dashboard_stats":
        return _get_dashboard_stats(backend, view_id)
    elif tool_name == "get_lineage":
        return _get_lineage(backend, arguments)
    elif tool_name == "list_views":
        return _list_views(backend)
    elif tool_name == "get_graph_stats":
        return _get_graph_stats(backend, view_id)
    else:
        return {"error": f"Unknown tool: {tool_name}"}


def _fair_truncate(results: list, limit: int) -> list:
    """Round-robin across object types so the total cap keeps type diversity —
    otherwise the first type block would consume the whole budget."""
    buckets: dict[str, list] = {}
    order: list[str] = []
    for r in results:
        t = r.get("object_type") or r.get("type") or ""
        if t not in buckets:
            buckets[t] = []
            order.append(t)
        buckets[t].append(r)
    picked: list = []
    idx = 0
    while len(picked) < limit:
        progressed = False
        for t in order:
            bucket = buckets[t]
            if idx < len(bucket):
                picked.append(bucket[idx])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        idx += 1
    return picked


def _search_catalog(backend, args: dict, view_id: int = None) -> dict:
    query = args.get("query", "")
    platform = args.get("platform", "")
    object_type = args.get("object_type", "")
    limit = args.get("limit", 20)
    search_term = f"%{query}%" if query else "%"

    results = []

    # Databricks notebooks
    if not platform or platform == "databricks":
        if not object_type or object_type == "notebook":
            sql = """SELECT notebook_id as id, notebook_name as name, 'notebook' as type,
                            'databricks' as platform, purpose, layer,
                            CAST(notebook_path AS TEXT) as description
                     FROM databricks_meta.notebooks
                     WHERE notebook_name LIKE %s OR CAST(notebook_path AS TEXT) LIKE %s
                     ORDER BY notebook_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    # Fabric pipelines
    if not platform or platform == "fabric":
        if not object_type or object_type == "pipeline":
            sql = """SELECT p.pipeline_id as id, p.pipeline_name as name, 'pipeline' as type,
                            'fabric' as platform, p.activity_count::text as purpose,
                            NULL as layer, w.workspace_name as description
                     FROM fabric_infra.pipelines p
                     JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                     WHERE p.pipeline_name LIKE %s OR w.workspace_name LIKE %s
                     ORDER BY p.pipeline_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    # Fabric warehouses
    if not platform or platform == "fabric":
        if not object_type or object_type == "warehouse":
            sql = """SELECT wh.warehouse_id as id, wh.warehouse_name as name, 'warehouse' as type,
                            'fabric' as platform, 'Warehouse' as purpose,
                            NULL as layer, w.workspace_name as description
                     FROM fabric_infra.warehouses wh
                     JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                     WHERE wh.warehouse_name LIKE %s OR w.workspace_name LIKE %s
                     ORDER BY wh.warehouse_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    # Fabric lakehouses
    if not platform or platform == "fabric":
        if not object_type or object_type == "lakehouse":
            sql = """SELECT lh.lakehouse_id as id, lh.lakehouse_name as name, 'lakehouse' as type,
                            'fabric' as platform, 'Lakehouse' as purpose,
                            NULL as layer, w.workspace_name as description
                     FROM fabric_infra.lakehouses lh
                     JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                     WHERE lh.lakehouse_name LIKE %s OR w.workspace_name LIKE %s
                     ORDER BY lh.lakehouse_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    # Power BI datasets
    if not platform or platform == "powerbi":
        if not object_type or object_type == "dataset":
            sql = """SELECT d.dataset_id as id, d.dataset_name as name, 'dataset' as type,
                            'powerbi' as platform, d.connection_mode as purpose,
                            NULL as layer, r.report_name as description
                     FROM powerbi_meta.datasets d
                     JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                     WHERE d.dataset_name LIKE %s OR r.report_name LIKE %s
                     ORDER BY d.dataset_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    # Power BI measures
    if not platform or platform == "powerbi":
        if not object_type or object_type == "measure":
            sql = """SELECT m.measure_id as id, m.measure_name as name, 'measure' as type,
                            'powerbi' as platform, m.measure_type as purpose,
                            NULL as layer, d.dataset_name as description
                     FROM powerbi_meta.measures m
                     JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
                     WHERE m.measure_name LIKE %s OR m.dax_expression LIKE %s
                     ORDER BY m.measure_name LIMIT %s"""
            results.extend(backend.query(sql, (search_term, search_term, limit)))

    return {"results": results, "count": len(results)}


def _get_object_detail(backend, config: CatalogConfig, args: dict) -> dict:
    obj_type = args["object_type"]
    obj_id = args["object_id"]

    # measure/pbi_table delegate to the same queries.py helpers the web detail
    # pages use (one source of truth for the SQL), rather than duplicating it.
    if obj_type in ("measure", "pbi_table"):
        from ade_catalog.core.queries import get_measure_detail, get_pbi_table_detail
        getter = get_measure_detail if obj_type == "measure" else get_pbi_table_detail
        result = getter(config, obj_id)
        if not result:
            return {"error": f"{obj_type} with id {obj_id} not found"}
        return {"object": _serialize(result)}

    # report/page/visual: return the typed contour (composition down one/all
    # levels) so a caller gets "which metrics per visual per page" in ONE call
    # instead of hand-walking a large get_lineage graph (report ←belongs_to←
    # page ←belongs_to← visual ←feeds← measure). Inventory first, graph fallback.
    if obj_type == "report":
        return _report_detail(backend, config, obj_id)
    if obj_type == "page":
        return _page_detail(backend, config, obj_id)
    if obj_type == "visual":
        return _visual_detail(backend, config, obj_id)

    queries = {
        "notebook": """SELECT n.*, w.workspace_name FROM databricks_meta.notebooks n
                       JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
                       WHERE n.notebook_id = %s""",
        "dataset": """SELECT d.*, r.report_name FROM powerbi_meta.datasets d
                      JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                      WHERE d.dataset_id = %s""",
        "pipeline": """SELECT p.*, w.workspace_name FROM fabric_infra.pipelines p
                       JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                       WHERE p.pipeline_id = %s""",
        "warehouse": """SELECT wh.*, w.workspace_name FROM fabric_infra.warehouses wh
                        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                        WHERE wh.warehouse_id = %s""",
        "lakehouse": """SELECT lh.*, w.workspace_name FROM fabric_infra.lakehouses lh
                        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                        WHERE lh.lakehouse_id = %s""",
        # Mirror the exact SQL used by web/routes/detail.py's wh_table/lh_table/
        # fab_notebook pages, so the tool and the UI never disagree on what a
        # "detail" of these types looks like.
        "wh_table": """SELECT t.table_id, t.table_name, t.table_type, t.column_count, t.row_count,
                              s.schema_name, wh.warehouse_name, w.workspace_name
                       FROM warehouse_meta.tables t
                       JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id
                       JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id
                       JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                       WHERE t.table_id = %s""",
        "lh_table": """SELECT t.table_id, t.table_name, t.row_count, t.size_mb, t.location,
                              s.schema_name, lh.lakehouse_name, w.workspace_name
                       FROM lakehouse_meta.tables t
                       JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id
                       JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id
                       JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                       WHERE t.table_id = %s""",
        "fab_notebook": """SELECT nb.notebook_id, nb.notebook_name, nb.description,
                                  nb.language as notebook_language, w.workspace_name
                           FROM fabric_infra.notebooks nb
                           JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
                           WHERE nb.notebook_id = %s""",
    }

    sql = queries.get(obj_type)
    if sql:
        try:
            rows = backend.query(sql, (obj_id,))
        except Exception:
            rows = []  # platform-meta schema absent (lineage-first org) -> graph fallback
        if rows:
            return {"object": _serialize(rows[0])}

    # No inventory row (unknown type, or a lineage-first org where the schema is
    # absent / the id is a graph node_id): return the generic node contour from
    # the graph. Closes "no detail for lineage-first types" (hive/tableau/spark/
    # talend/postgres) with one universal path.
    return _node_detail_from_lineage(backend, obj_id, declared_type=obj_type)


def _serialize(row: dict) -> dict:
    """Convert non-JSON-serializable values (dates, etc.) in-place, return the row."""
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
    return row


# ---------------------------------------------------------------------------
# get_object_detail helpers: typed contour (report/page/visual) + a universal
# lineage-first node contour (closes "no detail for lineage-first types").
# ---------------------------------------------------------------------------

_NODE_COLS = ("SELECT node_id, display_name, node_type, platform, qualified_name, properties "
              "FROM lineage_meta.nodes WHERE node_id = %s")


def _lineage_node_public(row: dict) -> dict:
    """Shape a lineage_meta.nodes row for the tool response (parse properties JSON)."""
    props = row.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (ValueError, TypeError):
            props = None
    return {
        "node_id": row["node_id"],
        "name": row.get("display_name"),
        "type": row.get("node_type"),
        "platform": row.get("platform"),
        "qualified_name": row.get("qualified_name"),
        "properties": props or {},
    }


def _lineage_children(backend, parent_id: int, child_type: str) -> list:
    """Nodes of child_type contained in parent_id. Containment is
    child --belongs_to--> parent, so the children are the edge SOURCES."""
    return backend.query(
        "SELECT n.node_id, n.display_name FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes n ON n.node_id = e.source_node_id "
        "WHERE e.target_node_id = %s AND e.relationship_type = 'belongs_to' AND n.node_type = %s "
        "ORDER BY n.display_name",
        (parent_id, child_type))


def _lineage_feeders(backend, target_id: int, source_type: str) -> list:
    """Nodes of source_type that FEED target_id (source --feeds--> target)."""
    return backend.query(
        "SELECT n.node_id, n.display_name FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes n ON n.node_id = e.source_node_id "
        "WHERE e.target_node_id = %s AND e.relationship_type = 'feeds' AND n.node_type = %s "
        "ORDER BY n.display_name",
        (target_id, source_type))


def _report_detail(backend, config: CatalogConfig, obj_id: int) -> dict:
    from ade_catalog.core import queries as q
    try:
        rep = q.get_report_detail(config, obj_id)
    except Exception:
        rep = None
    if rep:
        try:
            pages = [_serialize(p) for p in q.get_report_pages(config, obj_id)]
        except Exception:
            pages = []
        # pages carry visual_count; drill into a page (get_object_detail page) for
        # its per-visual measures — keeps the report payload bounded (a Acme
        # report can carry thousands of visuals).
        return {"object": _serialize(rep), "pages": pages, "source": "powerbi_meta"}
    return _report_detail_from_lineage(backend, obj_id)


def _report_detail_from_lineage(backend, node_id: int) -> dict:
    rows = backend.query(_NODE_COLS, (node_id,))
    if not rows:
        return {"error": f"report with id {node_id} not found"}
    pages_out, measures = [], {}
    for p in _lineage_children(backend, node_id, "page"):
        visuals = _lineage_children(backend, p["node_id"], "visual")
        for v in visuals:
            for m in _lineage_feeders(backend, v["node_id"], "measure"):
                measures[m["node_id"]] = m["display_name"]
        # visual_count only at the report level; drill into the page for the
        # per-visual measures — same bounded shape as the platform-meta path.
        pages_out.append({"node_id": p["node_id"], "name": p["display_name"],
                          "visual_count": len(visuals)})
    return {
        "object": _lineage_node_public(rows[0]),
        "pages": pages_out,
        # headline "which metrics does this report use" — distinct across all visuals
        "measures_used": [{"node_id": nid, "name": nm} for nid, nm in measures.items()],
        "source": "lineage_graph",
    }


def _page_detail(backend, config: CatalogConfig, obj_id: int) -> dict:
    from ade_catalog.core import queries as q
    try:
        pg = q.get_page_detail(config, obj_id)
    except Exception:
        pg = None
    if pg:
        try:
            visuals = [_serialize(v) for v in q.get_page_visuals(config, obj_id)]
        except Exception:
            visuals = []
        return {"object": _serialize(pg), "visuals": visuals, "source": "powerbi_meta"}
    return _page_detail_from_lineage(backend, obj_id)


def _page_detail_from_lineage(backend, node_id: int) -> dict:
    rows = backend.query(_NODE_COLS, (node_id,))
    if not rows:
        return {"error": f"page with id {node_id} not found"}
    visuals_out = []
    for v in _lineage_children(backend, node_id, "visual"):
        meas = _lineage_feeders(backend, v["node_id"], "measure")
        visuals_out.append({"node_id": v["node_id"], "name": v["display_name"],
                            "measures": [{"node_id": m["node_id"], "name": m["display_name"]} for m in meas]})
    return {"object": _lineage_node_public(rows[0]), "visuals": visuals_out, "source": "lineage_graph"}


def _visual_detail(backend, config: CatalogConfig, obj_id: int) -> dict:
    from ade_catalog.core import queries as q
    try:
        vis = q.get_visual_detail(config, obj_id)
    except Exception:
        vis = None
    if vis:
        return {"object": _serialize(vis), "source": "powerbi_meta"}
    return _visual_detail_from_lineage(backend, obj_id)


def _visual_detail_from_lineage(backend, node_id: int) -> dict:
    rows = backend.query(_NODE_COLS, (node_id,))
    if not rows:
        return {"error": f"visual with id {node_id} not found"}
    meas = _lineage_feeders(backend, node_id, "measure")
    tables = _lineage_feeders(backend, node_id, "table") + _lineage_feeders(backend, node_id, "semantic_table")
    return {
        "object": _lineage_node_public(rows[0]),
        "measures": [{"node_id": m["node_id"], "name": m["display_name"]} for m in meas],
        "tables": [{"node_id": t["node_id"], "name": t["display_name"]} for t in tables],
        "source": "lineage_graph",
    }


def _node_detail_from_lineage(backend, node_id: int, declared_type: str = None) -> dict:
    """Universal fallback: any graph node + its immediate neighbors, grouped by
    direction. Serves lineage-first types with no dedicated inventory detail
    (hive/tableau/spark/talend/postgres) and any inventory type on a
    lineage-first org where the platform-meta row is absent."""
    rows = backend.query(_NODE_COLS, (node_id,))
    if not rows:
        return {"error": f"{declared_type or 'object'} with id {node_id} not found"}
    cap = 50
    up = backend.query(
        "SELECT e.relationship_type, n.node_id, n.display_name, n.node_type, n.platform "
        "FROM lineage_meta.edges e JOIN lineage_meta.nodes n ON n.node_id = e.source_node_id "
        "WHERE e.target_node_id = %s ORDER BY e.relationship_type, n.display_name", (node_id,))
    down = backend.query(
        "SELECT e.relationship_type, n.node_id, n.display_name, n.node_type, n.platform "
        "FROM lineage_meta.edges e JOIN lineage_meta.nodes n ON n.node_id = e.target_node_id "
        "WHERE e.source_node_id = %s ORDER BY e.relationship_type, n.display_name", (node_id,))

    def _fmt(rs):
        return [{"relationship": r["relationship_type"], "node_id": r["node_id"],
                 "name": r["display_name"], "type": r["node_type"], "platform": r["platform"]}
                for r in rs[:cap]]

    return {
        "object": _lineage_node_public(rows[0]),
        "source": "lineage_graph",
        "upstream": _fmt(up),
        "downstream": _fmt(down),
        "upstream_total": len(up),
        "downstream_total": len(down),
        "truncated": len(up) > cap or len(down) > cap,
    }


def _list_platforms(backend, view_id: int = None) -> dict:
    platforms = []

    checks = [
        ("databricks", "SELECT COUNT(*) as count FROM databricks_meta.workspaces"),
        ("powerbi", "SELECT COUNT(*) as count FROM powerbi_meta.reports"),
        ("fabric", "SELECT COUNT(*) as count FROM fabric_infra.workspaces"),
    ]

    for name, sql in checks:
        try:
            rows = backend.query(sql)
            count = rows[0]["count"] if rows else 0
            if count > 0:
                platforms.append({"platform": name, "count": count, "source": "inventory"})
        except Exception:
            pass

    # Lineage-graph merge: platforms that exist only as graph nodes (postgresql,
    # hive, tableau, talend, … — or ALL platforms on a lineage-first org with no
    # platform-meta schemas at all, e.g. Contoso/Fabrikam). Count = graph nodes.
    try:
        where, params = ("WHERE view_id = %s", (view_id,)) if view_id is not None else ("", ())
        rows = backend.query(
            f"SELECT platform, COUNT(*) as count FROM lineage_meta.nodes {where} "
            "GROUP BY platform ORDER BY count DESC",
            tuple(params),
        )
        known = {p["platform"] for p in platforms}
        for r in rows:
            if r["platform"] and r["platform"] not in known:
                platforms.append({"platform": r["platform"], "count": r["count"], "source": "lineage_graph"})
    except Exception:
        pass  # no lineage schema deployed — the inventory-only answer stands

    return {"platforms": platforms}


def _get_dashboard_stats(backend, view_id: int = None) -> dict:
    stats = {}
    queries = {
        "workspaces": "SELECT COUNT(*) as count FROM fabric_infra.workspaces",
        "pipelines": "SELECT COUNT(*) as count FROM fabric_infra.pipelines",
        "warehouses": "SELECT COUNT(*) as count FROM fabric_infra.warehouses",
        "lakehouses": "SELECT COUNT(*) as count FROM fabric_infra.lakehouses",
        "fab_notebooks": "SELECT COUNT(*) as count FROM fabric_infra.notebooks",
        "dbx_notebooks": "SELECT COUNT(*) as count FROM databricks_meta.notebooks",
        "pbi_datasets": "SELECT COUNT(*) as count FROM powerbi_meta.datasets",
        "pbi_measures": "SELECT COUNT(*) as count FROM powerbi_meta.measures",
    }

    for key, sql in queries.items():
        try:
            rows = backend.query(sql)
            stats[key] = rows[0]["count"] if rows else 0
        except Exception:
            stats[key] = 0

    result = {"stats": stats}

    # Additive lineage-graph section, so lineage-first orgs (no platform-meta
    # schemas — e.g. Contoso, Fabrikam) don't answer all-zero despite thousands of nodes.
    try:
        where, params = ("WHERE view_id = %s", (view_id,)) if view_id is not None else ("", ())
        jwhere = "WHERE s.view_id = %s" if view_id is not None else ""
        n = backend.query(
            f"SELECT COUNT(*) as count FROM lineage_meta.nodes {where}", tuple(params))[0]["count"]
        if n:
            e = backend.query(
                "SELECT COUNT(*) as count FROM lineage_meta.edges e "
                "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id " + jwhere,
                tuple(params))[0]["count"]
            by_platform = backend.query(
                f"SELECT platform, COUNT(*) as count FROM lineage_meta.nodes {where} "
                "GROUP BY platform ORDER BY count DESC",
                tuple(params))
            result["graph"] = {
                "nodes": n,
                "edges": e,
                "nodes_by_platform": {r["platform"]: r["count"] for r in by_platform},
            }
    except Exception:
        pass  # no lineage schema — inventory stats only

    return result


def _get_lineage(backend, args: dict) -> dict:
    node_id_arg = args.get("node_id")
    node_name = args.get("node_name")
    direction = args.get("direction", "both")
    max_depth = args.get("max_depth", 3)
    max_nodes = args.get("max_nodes", 200) or 0  # 0/None = unlimited
    include_mermaid = args.get("include_mermaid", True)
    rel_in = [r for r in (args.get("relationship_types") or []) if r]
    rel_out = [r for r in (args.get("exclude_relationship_types") or []) if r]
    node_in = [t for t in (args.get("node_types") or []) if t]
    node_out = [t for t in (args.get("exclude_node_types") or []) if t]

    def _rel_filter(prefix: str = ""):
        # Applied INSIDE the recursive CTE (seed + recursive member) so an
        # excluded relationship is not traversed THROUGH, not merely hidden.
        clause, params = "", []
        if rel_in:
            clause += f" AND {prefix}relationship_type IN ({', '.join(['%s'] * len(rel_in))})"
            params.extend(rel_in)
        if rel_out:
            clause += f" AND {prefix}relationship_type NOT IN ({', '.join(['%s'] * len(rel_out))})"
            params.extend(rel_out)
        return clause, params

    def _node_filter():
        # Same discipline as _rel_filter but on the NEWLY-REACHED node's type
        # (joined as `fn` — target for downstream, source for upstream): an
        # excluded node type is not traversed THROUGH. On a hub, excluding
        # `column` drops the belongs_to sibling explosion (~80% of the payload).
        clause, params = "", []
        if node_in:
            clause += f" AND fn.node_type IN ({', '.join(['%s'] * len(node_in))})"
            params.extend(node_in)
        if node_out:
            clause += f" AND fn.node_type NOT IN ({', '.join(['%s'] * len(node_out))})"
            params.extend(node_out)
        return clause, params

    seed_clause, seed_params = _rel_filter()
    rec_clause, rec_params = _rel_filter("e.")
    node_clause, node_params = _node_filter()

    if node_id_arg is not None:
        # Unambiguous — exactly what search_catalog/get_object_detail return.
        nodes = backend.query(
            "SELECT node_id, display_name, node_type, platform FROM lineage_meta.nodes WHERE node_id = %s",
            (node_id_arg,)
        )
        if not nodes:
            return {"error": f"No node found with id {node_id_arg}", "nodes": [], "edges": []}
    elif node_name:
        # Display names are NOT unique (e.g. the same measure name can exist in a
        # prod and a qa dataset) — exact match first, then a partial match for
        # interactive/free-text search, but EITHER tier can legitimately return
        # more than one node. Silently picking the first one used to produce a
        # confident-looking but wrong answer; surface the ambiguity instead so
        # the caller can re-call with node_id.
        nodes = backend.query(
            "SELECT node_id, display_name, node_type, platform, qualified_name "
            "FROM lineage_meta.nodes WHERE display_name = %s LIMIT 5",
            (node_name,)
        )
        if not nodes:
            nodes = backend.query(
                "SELECT node_id, display_name, node_type, platform, qualified_name "
                "FROM lineage_meta.nodes WHERE display_name ILIKE %s LIMIT 5",
                (f"%{node_name}%",)
            )
        if not nodes:
            return {"error": f"No node found matching '{node_name}'", "nodes": [], "edges": []}
        if len(nodes) > 1:
            return {
                "error": f"'{node_name}' matches {len(nodes)} nodes — ambiguous. Re-call get_lineage with node_id set to the one you mean.",
                "candidates": [
                    {"node_id": n["node_id"], "name": n["display_name"], "type": n["node_type"],
                     "platform": n["platform"], "qualified_name": n.get("qualified_name")}
                    for n in nodes
                ],
                "nodes": [], "edges": [],
            }
    else:
        return {"error": "Provide node_id or node_name", "nodes": [], "edges": []}

    node_id = nodes[0]["node_id"]

    # Get lineage edges using recursive CTE
    if direction in ("downstream", "both"):
        downstream = backend.query(f"""
            WITH RECURSIVE lineage AS (
                SELECT e.source_node_id, e.target_node_id, e.relationship_type,
                       e.extraction_method, e.confidence, 1 as depth
                FROM lineage_meta.edges e
                JOIN lineage_meta.nodes fn ON fn.node_id = e.target_node_id
                WHERE e.source_node_id = %s{seed_clause}{node_clause}
                UNION ALL
                SELECT e.source_node_id, e.target_node_id, e.relationship_type,
                       e.extraction_method, e.confidence, l.depth + 1
                FROM lineage l
                JOIN lineage_meta.edges e ON l.target_node_id = e.source_node_id
                JOIN lineage_meta.nodes fn ON fn.node_id = e.target_node_id
                WHERE l.depth < %s{rec_clause}{node_clause}
            )
            SELECT DISTINCT l.*, ns.display_name as source_name, ns.node_type as source_type, ns.platform as source_platform,
                   nt.display_name as target_name, nt.node_type as target_type, nt.platform as target_platform
            FROM lineage l
            JOIN lineage_meta.nodes ns ON l.source_node_id = ns.node_id
            JOIN lineage_meta.nodes nt ON l.target_node_id = nt.node_id
        """, (node_id, *seed_params, *node_params, max_depth, *rec_params, *node_params))
    else:
        downstream = []

    if direction in ("upstream", "both"):
        upstream = backend.query(f"""
            WITH RECURSIVE lineage AS (
                SELECT e.source_node_id, e.target_node_id, e.relationship_type,
                       e.extraction_method, e.confidence, 1 as depth
                FROM lineage_meta.edges e
                JOIN lineage_meta.nodes fn ON fn.node_id = e.source_node_id
                WHERE e.target_node_id = %s{seed_clause}{node_clause}
                UNION ALL
                SELECT e.source_node_id, e.target_node_id, e.relationship_type,
                       e.extraction_method, e.confidence, l.depth + 1
                FROM lineage l
                JOIN lineage_meta.edges e ON l.source_node_id = e.target_node_id
                JOIN lineage_meta.nodes fn ON fn.node_id = e.source_node_id
                WHERE l.depth < %s{rec_clause}{node_clause}
            )
            SELECT DISTINCT l.*, ns.display_name as source_name, ns.node_type as source_type, ns.platform as source_platform,
                   nt.display_name as target_name, nt.node_type as target_type, nt.platform as target_platform
            FROM lineage l
            JOIN lineage_meta.nodes ns ON l.source_node_id = ns.node_id
            JOIN lineage_meta.nodes nt ON l.target_node_id = nt.node_id
        """, (node_id, *seed_params, *node_params, max_depth, *rec_params, *node_params))
    else:
        upstream = []

    all_edges = downstream + upstream
    all_edges.sort(key=lambda e: e["depth"])

    # Node cap, nearest-first: keep edges in depth order until adding one would
    # push the node count past max_nodes. Edges between already-kept nodes still
    # come in (they add no nodes). An unbounded hub (a dataset at depth 2 pulls
    # every column via belongs_to) once returned 1291 nodes / ~0.5MB — unusable
    # for an MCP client.
    truncated = False
    if max_nodes and max_nodes > 0:
        kept = []
        kept_ids = {node_id}
        for e in all_edges:
            new_ids = {e["source_node_id"], e["target_node_id"]} - kept_ids
            if len(kept_ids) + len(new_ids) > max_nodes:
                truncated = True
                continue
            kept_ids |= new_ids
            kept.append(e)
        all_edges = kept

    # Collect unique nodes
    seen_nodes = {}
    for e in all_edges:
        for prefix in ("source", "target"):
            nid = e[f"{prefix}_node_id"]
            if nid not in seen_nodes:
                seen_nodes[nid] = {
                    "node_id": nid,
                    "name": e[f"{prefix}_name"],
                    "type": e[f"{prefix}_type"],
                    "platform": e[f"{prefix}_platform"],
                }

    result = {
        "root_node": nodes[0],
        "nodes": list(seen_nodes.values()),
        # source_id/target_id let a caller reconstruct the graph unambiguously —
        # the display-name fields are for readability only and MUST NOT be used
        # to look nodes back up (two different nodes can share a name; see the
        # "nodes" list / node_id for the real identity).
        "edges": [{"source_id": e["source_node_id"], "target_id": e["target_node_id"],
                    "source": e["source_name"], "target": e["target_name"],
                    "relationship": e["relationship_type"], "depth": e["depth"],
                    # Provenance so a consumer can tell OBSERVED (api/code_parsing·high)
                    # from DERIVED (name_match·medium, ai_suggested·medium) edges.
                    "extraction_method": e.get("extraction_method"),
                    "confidence": e.get("confidence")}
                   for e in all_edges],
        "total_nodes": len(seen_nodes),
        "total_edges": len(all_edges),
        "truncated": truncated,
    }

    if include_mermaid:
        # Mermaid diagram for live rendering (Claude conversation, Flask viewer)
        from ade_catalog.lineage.render import to_mermaid
        nodes_by_id = {
            nid: {"name": n["name"], "type": n["type"], "platform": n["platform"]}
            for nid, n in seen_nodes.items()
        }
        mermaid_edges = [
            (e["source_node_id"], e["target_node_id"], e["relationship_type"])
            for e in all_edges
        ]
        result["mermaid"] = to_mermaid(nodes_by_id, mermaid_edges)

    return result


def _list_views(backend) -> dict:
    """Views registry (catalog_meta.catalog_views) — the perspectives defined in
    this organization's catalog. Each view carries its scan completeness manifest
    (TICK-021): a partial graph is partial BY CHOICE (depth tier + perimeter +
    completeness cause), not incomplete/broken — so a consumer must not read
    e.g. "0 lineage edges" as a finding when the view is inventory-only."""
    from ade_catalog.core import scan_manifest

    base = "view_id, view_name, description, view_type, is_default, is_active"
    manifest_cols = ", ".join(scan_manifest.FIELDS)
    try:
        rows = backend.query(
            f"SELECT {base}, {manifest_cols} "
            "FROM catalog_meta.catalog_views ORDER BY view_id"
        )
    except Exception:
        # DB predates the manifest columns (pre-TICK-021) — fall back to base.
        try:
            rows = backend.query(
                f"SELECT {base} FROM catalog_meta.catalog_views ORDER BY view_id"
            )
        except Exception as exc:
            return {"error": f"views registry not available: {exc}", "views": []}
    views = []
    for r in rows:
        v = _serialize(r)
        if v.get("completeness") is not None:
            v["completeness_label"] = scan_manifest.describe(v)
        views.append(v)
    return {"views": views, "count": len(views)}


def _get_graph_stats(backend, view_id: int = None) -> dict:
    """Aggregates over lineage_meta — works on lineage-first orgs too. Edges carry
    no view_id: scope them through the join on the source node."""
    where, params = ("WHERE view_id = %s", (view_id,)) if view_id is not None else ("", ())
    jwhere = "WHERE s.view_id = %s" if view_id is not None else ""
    xwhere = "AND s.view_id = %s" if view_id is not None else ""
    try:
        nodes_total = backend.query(
            f"SELECT COUNT(*) as count FROM lineage_meta.nodes {where}", tuple(params))[0]["count"]
    except Exception as exc:
        return {"error": f"lineage graph not available: {exc}"}
    edges_total = backend.query(
        "SELECT COUNT(*) as count FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id " + jwhere,
        tuple(params))[0]["count"]
    nodes_by_platform = backend.query(
        f"SELECT platform, COUNT(*) as count FROM lineage_meta.nodes {where} "
        "GROUP BY platform ORDER BY count DESC", tuple(params))
    nodes_by_type = backend.query(
        f"SELECT node_type, COUNT(*) as count FROM lineage_meta.nodes {where} "
        "GROUP BY node_type ORDER BY count DESC", tuple(params))
    edges_by_relationship = backend.query(
        "SELECT e.relationship_type as relationship_type, COUNT(*) as count "
        "FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id " + jwhere +
        " GROUP BY e.relationship_type ORDER BY count DESC", tuple(params))
    # Provenance dimensions: lets a consumer see the observed-vs-derived split
    # (confidence) and how edges were derived (extraction_method) without SQL.
    edges_by_confidence = backend.query(
        "SELECT e.confidence as confidence, COUNT(*) as count "
        "FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id " + jwhere +
        " GROUP BY e.confidence ORDER BY count DESC", tuple(params))
    edges_by_method = backend.query(
        "SELECT e.extraction_method as extraction_method, COUNT(*) as count "
        "FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id " + jwhere +
        " GROUP BY e.extraction_method ORDER BY count DESC", tuple(params))
    cross_platform_pairs = backend.query(
        "SELECT s.platform as source_platform, t.platform as target_platform, COUNT(*) as count "
        "FROM lineage_meta.edges e "
        "JOIN lineage_meta.nodes s ON s.node_id = e.source_node_id "
        "JOIN lineage_meta.nodes t ON t.node_id = e.target_node_id "
        "WHERE s.platform <> t.platform " + xwhere +
        " GROUP BY s.platform, t.platform ORDER BY count DESC", tuple(params))
    return {
        "view_id": view_id,
        "nodes_total": nodes_total,
        "edges_total": edges_total,
        "nodes_by_platform": nodes_by_platform,
        "nodes_by_type": nodes_by_type[:25],
        "edges_by_relationship": edges_by_relationship,
        "edges_by_confidence": edges_by_confidence,
        "edges_by_method": edges_by_method,
        "cross_platform_pairs": cross_platform_pairs,
    }
