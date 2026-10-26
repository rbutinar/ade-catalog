"""Catalog browser with flat list view, grouped by type."""

from collections import OrderedDict

from flask import Blueprint, current_app, g, render_template, request

from ade_catalog.core.queries import (
    get_available_platforms,
    search_catalog,
)

bp = Blueprint("catalog", __name__, url_prefix="/catalog")


# Map object_type from search results to detail route and display name
TYPE_CONFIG = {
    "notebook":     {"detail": "notebook",     "label": "Notebooks"},
    "table":        {"detail": "table",        "label": "Databricks Tables"},
    "dataset":      {"detail": "dataset",      "label": "Datasets"},
    "report":       {"detail": "report",       "label": "Reports"},
    "pbi_table":    {"detail": "pbi_table",    "label": "Power BI Tables"},
    "measure":      {"detail": "measure",      "label": "Measures"},
    "page":         {"detail": "page",         "label": "Report Pages"},
    "visual":       {"detail": "visual",       "label": "Visuals"},
    # relationship rows carry the parent dataset_id, column rows the parent table_id
    "relationship": {"detail": "dataset",      "label": "Relationships"},
    "column":       {"detail": "pbi_table",    "label": "Columns"},
    "pipeline":     {"detail": "pipeline",     "label": "Pipelines"},
    "warehouse":    {"detail": "warehouse",    "label": "Warehouses"},
    "lakehouse":    {"detail": "lakehouse",    "label": "Lakehouses"},
    "fab_notebook": {"detail": "fab_notebook", "label": "Fabric Notebooks"},
    "wh_table":     {"detail": "wh_table",     "label": "Warehouse Tables"},
    "lh_table":     {"detail": "lh_table",     "label": "Lakehouse Tables"},
    "workspace":    {"detail": "workspace",    "label": "Workspaces"},
    "data_product": {"detail": "data_product", "label": "Data Products"},
}


def _group_results(results: list[dict]) -> tuple[OrderedDict, int]:
    """Group search results by type, adding display fields."""
    grouped = OrderedDict()
    for r in results:
        obj_type = r.get("object_type", "unknown")
        cfg = TYPE_CONFIG.get(obj_type, {"detail": obj_type, "label": obj_type.replace("_", " ").title()})

        item = {
            "id": r.get("id"),
            "name": r.get("name", ""),
            "platform": r.get("platform", ""),
            "context": r.get("description", ""),  # workspace, schema, dataset, etc.
            "info": r.get("purpose", ""),          # row count, language, activities, etc.
            "detail_type": cfg["detail"],
            # lineage-first rows carry an explicit URL (the node detail page);
            # platform-meta rows fall back to /detail/<type>/<id> in the template.
            "detail_url": r.get("detail_url"),
        }

        label = cfg["label"]
        if label not in grouped:
            grouped[label] = []
        grouped[label].append(item)

    return grouped, len(results)


def _resolve_workspace_ids(config, view_id, filter_workspace, filter_environment):
    """Resolve the search scope to a list of Fabric workspace_ids, or None (= all).

    A specific workspace wins. Otherwise an environment expands to its workspaces:
    a bucket value (prod/test/dev/self-service/personal-copy), or the synthetic
    'exclude-personal' (prod+test+dev — the clean/presentable scope, dropping BOTH
    personal buckets: personal-copy and self-service). Returns [-1] to match nothing
    when an environment is selected but resolves to no workspaces.
    """
    if filter_workspace:
        return [filter_workspace]
    if not filter_environment:
        return None
    envs = get_fabric_environments(config, view_id=view_id)
    if filter_environment == "exclude-personal":
        ids = [wid for e in envs if e["value"] not in ("personal-copy", "self-service")
               for wid in e["workspace_ids"]]
    else:
        ids = next((e["workspace_ids"] for e in envs if e["value"] == filter_environment), [])
    return ids or [-1]


def _search_scoped(config, view_id, query, platforms, types, limit, ws_ids):
    """Run search_catalog across platform/type combos, scoped to ws_ids (or all)."""
    results = []
    scopes = ws_ids if ws_ids is not None else [None]
    for plat in (platforms or [""]):
        for typ in (types or [""]):
            for wid in scopes:
                results.extend(search_catalog(
                    config, query=query, platform=plat, object_type=typ,
                    view_id=view_id, limit=limit, workspace_id=wid))
    return results


@bp.route("/")
def browse():
    config = current_app.config["CATALOG"]
    view_id = g.view_id

    selected_platforms = request.args.getlist("platform")
    selected_types = request.args.getlist("type")
    query = request.args.get("q", "").strip()
    limit = request.args.get("limit", "100", type=str)

    try:
        available_platforms = get_available_platforms(config, view_id=view_id)
    except Exception:
        available_platforms = []

    # Lineage-first orgs (no platform-meta inventory) populate the platform/type
    # filters straight from the graph, so the dropdowns aren't empty.
    available_types = None
    if not available_platforms:
        from ade_catalog.core.queries import get_lineage_catalog_facets
        available_platforms, raw_types = get_lineage_catalog_facets(config, view_id)
        available_types = [
            {"value": t["value"],
             "label": TYPE_CONFIG.get(t["value"], {}).get("label", t["value"].replace("_", " ").title())}
            for t in raw_types
        ]

    # Search for each selected platform/type combo (or all if none selected),
    # scoped to a workspace or an environment (a filter over the estate view).
    filter_workspace = request.args.get("filter_workspace", type=int)
    filter_environment = request.args.get("filter_environment", "").strip()
    all_results = []
    try:
        ws_ids = _resolve_workspace_ids(config, view_id, filter_workspace, filter_environment)
        all_results = _search_scoped(
            config, view_id, query, selected_platforms, selected_types, int(limit), ws_ids)
    except Exception:
        pass

    # Deduplicate by (id, object_type)
    seen = set()
    deduped = []
    for r in all_results:
        key = (r.get("id"), r.get("object_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    results_by_type, total_count = _group_results(deduped)

    # Load filter options for advanced filters
    fabric_workspaces = []
    fabric_environments = []
    dbx_workspaces = []
    pbi_reports = []
    # Each platform's facet load is isolated: a schema missing for one platform
    # (e.g. a lineage-first org with no databricks_meta at all) must not raise
    # and mask the others — a shared try/except here previously did exactly
    # that (databricks failing before powerbi ran meant BOTH dropdowns stayed
    # empty, not just the one with no data).
    platform_codes = [p["platform"] for p in available_platforms]
    if "fabric" in platform_codes:
        try:
            fabric_workspaces = get_fabric_workspaces(config, view_id=view_id)
            fabric_environments = get_fabric_environments(config, view_id=view_id)
        except Exception:
            pass
    if "databricks" in platform_codes:
        try:
            dbx_workspaces = get_databricks_workspaces(config, view_id=view_id)
        except Exception:
            pass
    if "powerbi" in platform_codes:
        try:
            pbi_reports = get_powerbi_reports(config, view_id=view_id)
        except Exception:
            pass

    return render_template(
        "catalog.html",
        available_platforms=available_platforms,
        selected_platforms=selected_platforms,
        selected_types=selected_types,
        results_by_type=results_by_type,
        total_count=total_count,
        query=query,
        available_types=available_types,
        fabric_workspaces=fabric_workspaces,
        fabric_environments=fabric_environments,
        selected_environment=filter_environment,
        dbx_workspaces=dbx_workspaces,
        pbi_reports=pbi_reports,
    )


@bp.route("/search")
def search():
    """HTMX endpoint: returns search results partial."""
    config = current_app.config["CATALOG"]
    view_id = g.view_id

    query = request.args.get("q", "").strip()
    selected_platforms = request.args.getlist("platform")
    selected_types = request.args.getlist("type")
    limit = request.args.get("limit", "100", type=str)
    filter_workspace = request.args.get("filter_workspace", type=int)
    filter_environment = request.args.get("filter_environment", "").strip()

    all_results = []
    try:
        ws_ids = _resolve_workspace_ids(config, view_id, filter_workspace, filter_environment)
        all_results = _search_scoped(
            config, view_id, query, selected_platforms, selected_types, int(limit), ws_ids)
    except Exception:
        pass

    # Deduplicate
    seen = set()
    deduped = []
    for r in all_results:
        key = (r.get("id"), r.get("object_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    results_by_type, total_count = _group_results(deduped)

    return render_template(
        "partials/catalog_results.html",
        results_by_type=results_by_type,
        total_count=total_count,
        query=query,
        selected_platform=", ".join(selected_platforms) if selected_platforms else "",
    )


# ---------------------------------------------------------------------------
# Cascade filter endpoints (HTMX)
# ---------------------------------------------------------------------------

@bp.route("/filters/items")
def filter_items():
    """HTMX: load warehouses/lakehouses for a Fabric workspace."""
    config = current_app.config["CATALOG"]
    workspace_id = request.args.get("filter_workspace", "", type=str)

    warehouses = []
    lakehouses = []
    if workspace_id:
        try:
            from ade_catalog.core.queries import _query
            warehouses = _query(config,
                "SELECT warehouse_id, warehouse_name FROM fabric_infra.warehouses WHERE workspace_id = ?",
                (int(workspace_id),))
            lakehouses = _query(config,
                "SELECT lakehouse_id, lakehouse_name FROM fabric_infra.lakehouses WHERE workspace_id = ?",
                (int(workspace_id),))
        except Exception:
            pass

    return render_template("partials/fabric_cascade.html",
                           warehouses=warehouses, lakehouses=lakehouses)


@bp.route("/filters/workspaces")
def filter_workspaces_cascade():
    """HTMX: reload the Workspace dropdown narrowed to the selected Environment.

    Environment → Workspace is a cascade: picking an environment restricts the
    workspace list to that environment's workspaces. The returned partial also
    fires a search so the results reflect the new environment immediately.
    """
    config = current_app.config["CATALOG"]
    view_id = g.view_id
    env = request.args.get("filter_environment", "").strip()

    workspaces = []
    try:
        workspaces = get_fabric_workspaces(config, view_id=view_id)
        if env:
            ids = set(_resolve_workspace_ids(config, view_id, None, env) or [])
            workspaces = [w for w in workspaces if w["workspace_id"] in ids]
    except Exception:
        pass

    return render_template("partials/fabric_workspace_select.html",
                           fabric_workspaces=workspaces, selected_environment=env)


@bp.route("/filters/schemas")
def filter_schemas_cascade():
    """HTMX: load schemas for a warehouse or lakehouse."""
    config = current_app.config["CATALOG"]
    item_ref = request.args.get("filter_item", "")

    schemas = []
    if item_ref and ":" in item_ref:
        item_type, item_id = item_ref.split(":", 1)
        try:
            container_type = "Warehouse" if item_type == "wh" else "Lakehouse"
            schemas = get_fabric_schemas(config, int(item_id), container_type)
        except Exception:
            pass

    return render_template("partials/fabric_schema_cascade.html", schemas=schemas)


@bp.route("/filters/datasets")
def filter_datasets():
    """HTMX: load datasets for a Power BI report."""
    config = current_app.config["CATALOG"]
    report_id = request.args.get("filter_report", "", type=str)

    datasets = []
    if report_id:
        try:
            datasets = get_datasets_for_report(config, int(report_id))
        except Exception:
            pass

    return render_template("partials/pbi_cascade.html", datasets=datasets)


# ---------------------------------------------------------------------------
# Tree endpoints (kept for backward compatibility with detail pages)
# ---------------------------------------------------------------------------

from ade_catalog.core.queries import (
    get_databricks_workspaces,
    get_datasets_for_report,
    get_fabric_containers,
    get_fabric_environments,
    get_fabric_notebooks_for_workspace,
    get_fabric_pipelines,
    get_fabric_schemas,
    get_fabric_workspaces,
    get_lakehouse_tables,
    get_measures_for_dataset,
    get_notebooks_for_workspace,
    get_pipeline_activities,
    get_powerbi_reports,
    get_tables_for_dataset,
    get_warehouse_procedures,
    get_warehouse_tables,
    get_warehouse_views,
)


@bp.route("/tree/workspace/<int:workspace_id>")
def tree_workspace(workspace_id: int):
    config = current_app.config["CATALOG"]
    notebooks = get_notebooks_for_workspace(config, workspace_id)
    return render_template("partials/tree_notebooks.html", notebooks=notebooks)


@bp.route("/tree/report/<int:report_id>")
def tree_report(report_id: int):
    config = current_app.config["CATALOG"]
    datasets = get_datasets_for_report(config, report_id)
    return render_template("partials/tree_datasets.html", datasets=datasets)


@bp.route("/tree/dataset/<int:dataset_id>")
def tree_dataset(dataset_id: int):
    config = current_app.config["CATALOG"]
    tables = get_tables_for_dataset(config, dataset_id)
    measures = get_measures_for_dataset(config, dataset_id)
    return render_template(
        "partials/tree_dataset_children.html",
        tables=tables, measures=measures, dataset_id=dataset_id,
    )


@bp.route("/tree/fabric-workspace/<int:workspace_id>")
def tree_fabric_workspace(workspace_id: int):
    config = current_app.config["CATALOG"]
    containers = get_fabric_containers(config, workspace_id)
    pipelines = get_fabric_pipelines(config, workspace_id)
    notebooks = get_fabric_notebooks_for_workspace(config, workspace_id)
    return render_template(
        "partials/tree_fabric_workspace.html",
        containers=containers, pipelines=pipelines, notebooks=notebooks,
    )


@bp.route("/tree/fabric-container/<container_type>/<int:container_id>")
def tree_fabric_container(container_type: str, container_id: int):
    config = current_app.config["CATALOG"]
    schemas = get_fabric_schemas(config, container_id, container_type)
    return render_template(
        "partials/tree_fabric_schemas.html",
        schemas=schemas, container_type=container_type, container_id=container_id,
    )


@bp.route("/tree/fabric-schema/<container_type>/<int:schema_id>")
def tree_fabric_schema(container_type: str, schema_id: int):
    config = current_app.config["CATALOG"]
    if container_type == "Warehouse":
        tables = get_warehouse_tables(config, schema_id)
        views = get_warehouse_views(config, schema_id)
        procedures = get_warehouse_procedures(config, schema_id)
    else:
        tables = get_lakehouse_tables(config, schema_id)
        views = []
        procedures = []
    return render_template(
        "partials/tree_fabric_schema_children.html",
        tables=tables, views=views, procedures=procedures, container_type=container_type,
    )


@bp.route("/tree/fabric-pipeline/<int:pipeline_id>")
def tree_fabric_pipeline(pipeline_id: int):
    config = current_app.config["CATALOG"]
    activities = get_pipeline_activities(config, pipeline_id)
    return render_template("partials/tree_fabric_activities.html", activities=activities)
