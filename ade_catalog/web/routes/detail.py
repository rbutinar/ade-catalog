"""Object detail pages."""

from flask import Blueprint, abort, current_app, g, redirect, render_template, request

from ade_catalog.mcp_server.tools import execute_tool
from ade_catalog.core.attribute_schema import get_attribute_schema
from ade_catalog.core.queries import (
    get_available_platforms,
    get_node_detail,
    get_node_neighbors,
    get_dataset_detail,
    get_dataset_measures,
    get_dataset_relationships,
    get_dataset_tables,
    get_measure_detail,
    get_notebook_detail,
    get_notebook_tables,
    get_page_detail,
    get_page_visuals,
    get_pbi_table_detail,
    get_pipeline_detail,
    get_pipeline_activities,
    get_report_datasets,
    get_report_detail,
    get_report_pages,
    get_table_columns,
    get_visual_detail,
    get_warehouse_table_columns,
)

bp = Blueprint("detail", __name__, url_prefix="/detail")


def _lineage(config, name, depth: int = 2, node_id: int = None):
    """Fetch object-level lineage for embedding on a detail page.

    Reuses the get_lineage MCP tool + shared renderer (same graph as /lineage),
    so a detail page can show the object's own upstream/downstream. Prefer
    passing node_id when the caller already holds it (e.g. the lineage-first
    node route) — display names are not unique (the same measure/table name can
    exist in more than one dataset or environment), so a name-only lookup can
    silently resolve to a DIFFERENT node than the one this page is about.
    Returns None when there's no lineage node, or the name is ambiguous and no
    node_id was given (the template then offers the full viewer instead).
    """
    if not node_id and not name:
        return None
    try:
        args = {"node_id": node_id} if node_id else {"node_name": name}
        res = execute_tool(
            "get_lineage",
            # max_nodes=0: the embedded section renders the full graph as it
            # always has — the 200-node default cap is for MCP/assistant callers.
            {**args, "direction": "both", "max_depth": depth, "max_nodes": 0},
            config,
            getattr(g, "view_id", None),
        )
        if res.get("error"):
            return None
        # Compact per-class summary (type -> count) for the embedded section,
        # so a big graph shows as a few colour chips instead of a flat wall.
        from collections import Counter
        counts = Counter((n.get("type") or "unknown") for n in res.get("nodes", []))
        res["class_counts"] = sorted(
            ({"type": t, "count": c} for t, c in counts.items()),
            key=lambda x: -x["count"],
        )
        return res
    except Exception:
        return None


def _enrichment(config, name):
    """Resolve the object's lineage node by name and fetch its description
    enrichment (for the summary card). Returns None when the object has no graph
    node — enrichment is anchored on the node (view_id + node_id)."""
    if not name:
        return None
    view_id = getattr(g, "view_id", None)
    if view_id is None:
        return None
    from ade_catalog.core.queries import _query
    try:
        rows = _query(
            config,
            "SELECT node_id, node_type FROM lineage_meta.nodes "
            "WHERE view_id = %s AND display_name = %s ORDER BY node_id",
            (view_id, name),
        )
    except Exception:
        return None
    if not rows:
        return None
    node_id, node_type = rows[0]["node_id"], rows[0]["node_type"]
    from ade_catalog.core.enrichment import fetch
    return {
        "view_id": view_id,
        "object_class": node_type,
        "object_id": node_id,
        "qualified_name": name,
        "enrichment": fetch(config, view_id, node_type, node_id),
    }


def _enrichment_for_node(config, node):
    """Enrichment ctx for a graph node we already hold (exact, no name lookup)."""
    view_id = getattr(g, "view_id", None)
    from ade_catalog.core.enrichment import fetch
    return {
        "view_id": view_id,
        "object_class": node["node_type"],
        "object_id": node["node_id"],
        "qualified_name": node["display_name"],
        "enrichment": fetch(config, view_id, node["node_type"], node["node_id"]),
    }


@bp.route("/node/<int:node_id>")
def node(node_id: int):
    """Lineage-native detail page for a graph node.

    Serves lineage-first orgs (data only in lineage_meta, e.g. a UC-lineage scan)
    whose objects have no per-platform detail page. When the org DOES have the
    platform inventory, redirect to the richer platform-meta page instead, so
    Acme keeps its full detail while Contoso gets a proper page from the graph.
    """
    config = current_app.config["CATALOG"]
    n = get_node_detail(config, node_id)
    if not n:
        abort(404)
    # Data products get a dedicated curation page (members + editor).
    if n["node_type"] == "data_product":
        from ade_catalog.core import data_product as _dp
        from ade_catalog.core.db_backend import get_active_backend
        d = _dp.get(get_active_backend(config), getattr(g, "view_id", None), node_id)
        if d:
            return render_template("detail/data_product.html", dp=d)
    # Prefer the rich platform-meta page when this DB actually has that inventory.
    from ade_catalog.web.routes.lineage import _detail_url
    pm_url = _detail_url(n["node_type"], n["platform"], n.get("external_id"))
    if pm_url:
        plats = {p["platform"] for p in get_available_platforms(config, getattr(g, "view_id", None))}
        if n["platform"] in plats:
            return redirect(pm_url)

    schema = get_attribute_schema().get(n["node_type"], [])
    props = n["properties"]
    typed = [{**a, "value": props.get(a["key"])} for a in schema
             if props.get(a["key"]) not in (None, "")]
    shown = {a["key"] for a in schema}
    extras = [(k, v) for k, v in props.items() if k not in shown and v not in (None, "")]
    upstream, downstream = get_node_neighbors(config, node_id)
    return render_template(
        "detail/node.html",
        obj=n, typed=typed, extras=extras,
        upstream=upstream, downstream=downstream,
        lineage=_lineage(config, n["display_name"], node_id=node_id),
        lineage_query=n["display_name"],
        enrichment_ctx=_enrichment_for_node(config, n),
    )


@bp.route("/notebook/<int:notebook_id>")
def notebook(notebook_id: int):
    config = current_app.config["CATALOG"]
    nb = get_notebook_detail(config, notebook_id)
    if not nb:
        abort(404)
    tables = get_notebook_tables(config, notebook_id)
    reads = [t for t in tables if t["access_type"] == "read"]
    writes = [t for t in tables if t["access_type"] != "read"]

    return render_template(
        "detail/notebook.html",
        obj=nb,
        reads=reads,
        writes=writes,
        lineage=_lineage(config, nb["notebook_name"]),
        lineage_query=nb["notebook_name"],
        enrichment_ctx=_enrichment(config, nb["notebook_name"]),
    )


@bp.route("/dataset/<int:dataset_id>")
def dataset(dataset_id: int):
    config = current_app.config["CATALOG"]
    ds = get_dataset_detail(config, dataset_id)
    if not ds:
        abort(404)
    tables = get_dataset_tables(config, dataset_id)
    measures = get_dataset_measures(config, dataset_id)
    relationships = get_dataset_relationships(config, dataset_id)

    return render_template(
        "detail/dataset.html",
        obj=ds,
        tables=tables,
        measures=measures,
        relationships=relationships,
        lineage=_lineage(config, ds["dataset_name"]),
        lineage_query=ds["dataset_name"],
        enrichment_ctx=_enrichment(config, ds["dataset_name"]),
    )


@bp.route("/report/<int:report_id>")
def report(report_id: int):
    config = current_app.config["CATALOG"]
    r = get_report_detail(config, report_id)
    if not r:
        abort(404)
    return render_template(
        "detail/report.html",
        obj=r,
        pages=get_report_pages(config, report_id),
        datasets=get_report_datasets(config, report_id),
        lineage=_lineage(config, r["report_name"]),
        lineage_query=r["report_name"],
        enrichment_ctx=_enrichment(config, r["report_name"]),
    )


@bp.route("/page/<int:page_id>")
def page(page_id: int):
    config = current_app.config["CATALOG"]
    p = get_page_detail(config, page_id)
    if not p:
        abort(404)
    return render_template(
        "detail/page.html",
        obj=p,
        visuals=get_page_visuals(config, page_id),
    )


@bp.route("/visual/<int:visual_id>")
def visual(visual_id: int):
    import json as _json

    config = current_app.config["CATALOG"]
    v = get_visual_detail(config, visual_id)
    if not v:
        abort(404)

    def _parse(raw):
        if not raw:
            return []
        try:
            val = _json.loads(raw)
            return val if isinstance(val, list) else [val]
        except (ValueError, TypeError):
            return [raw]

    vname = v.get("visual_name") or v.get("visual_type") or "(unnamed)"
    lineage_name = f"{v.get('page_name')}/{vname}" if v.get("page_name") else vname
    return render_template(
        "detail/visual.html",
        obj=v,
        tables=_parse(v.get("tables_used")),
        measures=_parse(v.get("measures_used")),
        columns=_parse(v.get("columns_used")),
        lineage=_lineage(config, lineage_name),
        lineage_query=lineage_name,
        enrichment_ctx=_enrichment(config, lineage_name),
    )


@bp.route("/measure/<int:measure_id>")
def measure(measure_id: int):
    config = current_app.config["CATALOG"]
    m = get_measure_detail(config, measure_id)
    if not m:
        abort(404)

    return render_template(
        "detail/measure.html",
        obj=m,
        lineage=_lineage(config, m["measure_name"]),
        lineage_query=m["measure_name"],
        enrichment_ctx=_enrichment(config, m["measure_name"]),
    )


@bp.route("/pbi_table/<int:table_id>")
def pbi_table(table_id: int):
    config = current_app.config["CATALOG"]
    tbl = get_pbi_table_detail(config, table_id)
    if not tbl:
        abort(404)
    columns = get_table_columns(config, table_id)

    return render_template(
        "detail/pbi_table.html",
        obj=tbl,
        columns=columns,
        lineage=_lineage(config, tbl["table_name"]),
        lineage_query=tbl["table_name"],
        enrichment_ctx=_enrichment(config, tbl["table_name"]),
    )


@bp.route("/pipeline/<int:pipeline_id>")
def pipeline(pipeline_id: int):
    config = current_app.config["CATALOG"]
    try:
        p = get_pipeline_detail(config, pipeline_id)
    except Exception:
        p = None
    if not p:
        abort(404)
    try:
        activities = get_pipeline_activities(config, pipeline_id)
    except Exception:
        activities = []

    return render_template(
        "detail/pipeline.html",
        obj=p,
        activities=activities,
        lineage=_lineage(config, p["pipeline_name"]),
        lineage_query=p["pipeline_name"],
        enrichment_ctx=_enrichment(config, p["pipeline_name"]),
    )


@bp.route("/warehouse/<int:warehouse_id>")
def warehouse(warehouse_id: int):
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query, get_fabric_schemas, get_warehouse_tables, get_warehouse_views, get_warehouse_procedures

    rows = _query(config, """
        SELECT wh.warehouse_id, wh.warehouse_name, wh.sql_endpoint,
               wh.description, w.workspace_name
        FROM fabric_infra.warehouses wh
        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
        WHERE wh.warehouse_id = ?
    """, (warehouse_id,))
    if not rows:
        abort(404)

    obj = rows[0]

    try:
        schemas = get_fabric_schemas(config, warehouse_id, "Warehouse")
    except Exception:
        schemas = []

    selected_schema_id = request.args.get("schema_id", type=int)
    tables = []
    views = []
    procedures = []

    schema_id = selected_schema_id or (schemas[0].get("schema_id") if schemas else None)
    if schema_id:
        try:
            tables = get_warehouse_tables(config, schema_id)
            views = get_warehouse_views(config, schema_id)
            procedures = get_warehouse_procedures(config, schema_id)
        except Exception:
            pass

    return render_template("detail/warehouse.html",
                           obj=obj, schemas=schemas, tables=tables,
                           views=views, procedures=procedures,
                           selected_schema_id=schema_id,
                           lineage=_lineage(config, obj["warehouse_name"]),
                           lineage_query=obj["warehouse_name"],
                           enrichment_ctx=_enrichment(config, obj["warehouse_name"]))


@bp.route("/lakehouse/<int:lakehouse_id>")
def lakehouse(lakehouse_id: int):
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query, get_fabric_schemas, get_lakehouse_tables

    rows = _query(config, """
        SELECT lh.lakehouse_id, lh.lakehouse_name, lh.sql_endpoint,
               lh.onelake_path, lh.description, w.workspace_name
        FROM fabric_infra.lakehouses lh
        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
        WHERE lh.lakehouse_id = ?
    """, (lakehouse_id,))
    if not rows:
        abort(404)

    obj = rows[0]

    # Load schemas
    try:
        schemas = get_fabric_schemas(config, lakehouse_id, "Lakehouse")
    except Exception:
        schemas = []

    # Load tables for selected or first schema
    selected_schema_id = request.args.get("schema_id", type=int)
    tables = []
    if selected_schema_id:
        try:
            tables = get_lakehouse_tables(config, selected_schema_id)
        except Exception:
            pass
    elif schemas:
        selected_schema_id = schemas[0].get("schema_id")
        try:
            tables = get_lakehouse_tables(config, selected_schema_id)
        except Exception:
            pass

    return render_template("detail/lakehouse.html",
                           obj=obj, schemas=schemas, tables=tables,
                           selected_schema_id=selected_schema_id,
                           lineage=_lineage(config, obj["lakehouse_name"]),
                           lineage_query=obj["lakehouse_name"],
                           enrichment_ctx=_enrichment(config, obj["lakehouse_name"]))


@bp.route("/wh_table/<int:table_id>")
def wh_table(table_id: int):
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query
    rows = _query(config, """
        SELECT t.table_id, t.table_name, t.table_type, t.column_count, t.row_count,
               s.schema_name, wh.warehouse_name, w.workspace_name
        FROM warehouse_meta.tables t
        JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id
        JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id
        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
        WHERE t.table_id = ?
    """, (table_id,))
    if not rows:
        abort(404)
    columns = get_warehouse_table_columns(config, table_id)
    return render_template("detail/wh_table.html", obj=rows[0], columns=columns,
                           lineage=_lineage(config, rows[0]["table_name"]),
                           lineage_query=rows[0]["table_name"],
                           enrichment_ctx=_enrichment(config, rows[0]["table_name"]))


@bp.route("/lh_table/<int:table_id>")
def lh_table(table_id: int):
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query
    rows = _query(config, """
        SELECT t.table_id, t.table_name, t.row_count, t.size_mb, t.location,
               s.schema_name, lh.lakehouse_name, w.workspace_name
        FROM lakehouse_meta.tables t
        JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id
        JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id
        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
        WHERE t.table_id = ?
    """, (table_id,))
    if not rows:
        abort(404)
    return render_template("detail/lh_table.html", obj=rows[0],
                           lineage=_lineage(config, rows[0]["table_name"]),
                           lineage_query=rows[0]["table_name"],
                           enrichment_ctx=_enrichment(config, rows[0]["table_name"]))


@bp.route("/fab_notebook/<int:notebook_id>")
def fab_notebook(notebook_id: int):
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query
    rows = _query(config, """
        SELECT nb.notebook_id, nb.notebook_name, nb.description,
               nb.language as notebook_language, w.workspace_name
        FROM fabric_infra.notebooks nb
        JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
        WHERE nb.notebook_id = ?
    """, (notebook_id,))
    if not rows:
        abort(404)
    return render_template("detail/fab_notebook.html", obj=rows[0],
                           lineage=_lineage(config, rows[0]["notebook_name"]),
                           lineage_query=rows[0]["notebook_name"],
                           enrichment_ctx=_enrichment(config, rows[0]["notebook_name"]))


@bp.route("/workspace/<int:workspace_id>")
def workspace(workspace_id: int):
    """Fabric workspace landing page: summary + its contents (lakehouses,
    warehouses, pipelines, notebooks) + embedded lineage."""
    config = current_app.config["CATALOG"]
    from ade_catalog.core.queries import _query
    rows = _query(config, """
        SELECT workspace_id, workspace_name, fabric_workspace_id, capacity_region
        FROM fabric_infra.workspaces WHERE workspace_id = %s
    """, (workspace_id,))
    if not rows:
        abort(404)
    obj = rows[0]
    lakehouses = _query(config, "SELECT lakehouse_id, lakehouse_name FROM fabric_infra.lakehouses WHERE workspace_id = %s ORDER BY lakehouse_name", (workspace_id,))
    warehouses = _query(config, "SELECT warehouse_id, warehouse_name FROM fabric_infra.warehouses WHERE workspace_id = %s ORDER BY warehouse_name", (workspace_id,))
    pipelines = _query(config, "SELECT pipeline_id, pipeline_name, activity_count FROM fabric_infra.pipelines WHERE workspace_id = %s ORDER BY pipeline_name", (workspace_id,))
    notebooks = _query(config, "SELECT notebook_id, notebook_name FROM fabric_infra.notebooks WHERE workspace_id = %s ORDER BY notebook_name", (workspace_id,))
    return render_template("detail/workspace.html", obj=obj,
                           lakehouses=lakehouses, warehouses=warehouses,
                           pipelines=pipelines, notebooks=notebooks,
                           lineage=_lineage(config, obj["workspace_name"]),
                           lineage_query=obj["workspace_name"],
                           enrichment_ctx=_enrichment(config, obj["workspace_name"]))
