"""
SQL query layer for the ade-catalog web UI.

All database queries used by Flask routes are defined here.
Returns raw rows (list of dicts) for template rendering.

Every listing/search query accepts a view_id parameter to scope
results to the active catalog view.
"""

import json
import logging
from typing import Optional

from ade_catalog.core.config import CatalogConfig
from ade_catalog.core.database import get_connection

logger = logging.getLogger(__name__)


def _rows_to_dicts(cursor) -> list[dict]:
    """Convert cursor results to list of dicts."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_db_config(config: CatalogConfig):
    """Get the database config, preferring org-specific override if available."""
    try:
        from flask import g
        if hasattr(g, 'org_db_config') and g.org_db_config:
            return g.org_db_config
    except RuntimeError:
        pass  # Outside Flask request context
    return config.database


def _use_backend(config: CatalogConfig):
    """Check if we should use the db_backend abstraction instead of direct pyodbc.

    The raw-pyodbc branch exists only for the default SQL Server backend (its SQL
    is hand-written T-SQL). Every other backend — Supabase (PostgreSQL) and SQLite
    (public self-host edition) — goes through the db_backend abstraction, whose
    query() applies the dialect adaptation those Postgres-style statements need.
    """
    if config.db_backend in ("supabase", "sqlite"):
        return True
    try:
        from flask import g
        if hasattr(g, 'org_backend') and g.org_backend:
            return True
    except RuntimeError:
        pass
    return False


def _query(config: CatalogConfig, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return results as list of dicts.

    Always goes through the backend abstraction (session org-switch backend if
    set, else the default) rather than a raw pyodbc connection — the backend's
    query() applies dialect adaptation (Postgres-style %s/LIMIT -> T-SQL ?/TOP),
    which most functions in this module don't need (they're written directly in
    T-SQL and pass through unchanged) but a few newer ones (e.g.
    _search_catalog_from_lineage, shared with the Supabase path) rely on. The
    previous raw-connection fallback executed those unadapted, which SQL Server
    rejects as invalid syntax — silently swallowed by callers' broad
    `except Exception: return []`, so lineage-first orgs looked merely empty
    instead of erroring.
    """
    from ade_catalog.core.db_backend import get_active_backend
    return get_active_backend(config).query(sql, params)


# ---------------------------------------------------------------------------
# Platform availability
# ---------------------------------------------------------------------------

def get_available_platforms(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Return platforms that have data in the given view."""
    platforms = []
    checks = [
        ("databricks", "Databricks", "databricks_meta.workspaces"),
        ("powerbi", "Power BI", "powerbi_meta.reports"),
        ("fabric", "Fabric", "fabric_infra.workspaces"),
    ]
    for code, label, table in checks:
        try:
            if view_id:
                rows = _query(config, f"SELECT COUNT(*) as cnt FROM {table} WHERE view_id = ?", (view_id,))
            else:
                rows = _query(config, f"SELECT COUNT(*) as cnt FROM {table}")
            count = rows[0]["cnt"] if rows else 0
            if count > 0:
                platforms.append({"platform": code, "label": label, "count": count})
        except Exception:
            pass
    return platforms


# ---------------------------------------------------------------------------
# Dashboard queries
# ---------------------------------------------------------------------------

def _count(config: CatalogConfig, sql: str, params: tuple = ()) -> int:
    """Execute a COUNT query and return the count."""
    rows = _query(config, sql, params)
    if rows:
        # Handle both dict key formats
        val = rows[0]
        return val.get("cnt", val.get("count", list(val.values())[0] if val else 0))
    return 0


def get_dashboard_stats(config: CatalogConfig, view_id: int = None) -> dict:
    """Get aggregate statistics for the dashboard.

    Each count is independent and tolerant of a missing platform schema: a DB
    provisioned for only some platforms (e.g. a Fabric-only view without
    databricks_meta) still returns stats for the platforms it has, rather than
    failing the whole dashboard.
    """
    def safe(sql, params=None):
        try:
            return _count(config, sql, params)
        except Exception:
            return 0

    stats = {}
    if view_id:
        stats["workspaces"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.workspaces WHERE view_id = ?", (view_id,))
        stats["notebooks"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.notebooks n JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id WHERE w.view_id = ?", (view_id,))
        stats["jobs"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.jobs j JOIN databricks_meta.workspaces w ON j.workspace_id = w.workspace_id WHERE w.view_id = ?", (view_id,))
        stats["table_access"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.notebook_table_access nta JOIN databricks_meta.notebooks n ON nta.notebook_id = n.notebook_id JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id WHERE w.view_id = ?", (view_id,))
        stats["reports"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.reports WHERE view_id = ?", (view_id,))
        stats["datasets"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.datasets d JOIN powerbi_meta.reports r ON d.report_id = r.report_id WHERE r.view_id = ?", (view_id,))
        stats["pbi_tables"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.tables t JOIN powerbi_meta.datasets d ON t.dataset_id = d.dataset_id JOIN powerbi_meta.reports r ON d.report_id = r.report_id WHERE r.view_id = ?", (view_id,))
        stats["measures"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.measures m JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id JOIN powerbi_meta.reports r ON d.report_id = r.report_id WHERE r.view_id = ?", (view_id,))
        stats["relationships"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.relationships rel JOIN powerbi_meta.datasets d ON rel.dataset_id = d.dataset_id JOIN powerbi_meta.reports r ON d.report_id = r.report_id WHERE r.view_id = ?", (view_id,))
    else:
        stats["workspaces"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.workspaces")
        stats["notebooks"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.notebooks")
        stats["jobs"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.jobs")
        stats["table_access"] = safe("SELECT COUNT(*) as cnt FROM databricks_meta.notebook_table_access")
        stats["reports"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.reports")
        stats["datasets"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.datasets")
        stats["pbi_tables"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.tables")
        stats["measures"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.measures")
        stats["relationships"] = safe("SELECT COUNT(*) as cnt FROM powerbi_meta.relationships")
    return stats


def get_notebooks_by_layer(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get notebook count per layer."""
    if view_id:
        return _query(config, """
            SELECT n.layer, COUNT(*) as cnt
            FROM databricks_meta.notebooks n
            JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            GROUP BY n.layer ORDER BY n.layer
        """, (view_id,))
    return _query(config, """
        SELECT layer, COUNT(*) as cnt
        FROM databricks_meta.notebooks
        GROUP BY layer ORDER BY layer
    """)


def get_measures_by_dataset(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get measure count per dataset."""
    if view_id:
        return _query(config, """
            SELECT d.dataset_name, COUNT(m.measure_id) as cnt
            FROM powerbi_meta.measures m
            JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
            JOIN powerbi_meta.reports r ON d.report_id = r.report_id
            WHERE r.view_id = ?
            GROUP BY d.dataset_name ORDER BY cnt DESC
        """, (view_id,))
    return _query(config, """
        SELECT d.dataset_name, COUNT(m.measure_id) as cnt
        FROM powerbi_meta.measures m
        JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
        GROUP BY d.dataset_name ORDER BY cnt DESC
    """)


# ---------------------------------------------------------------------------
# Hierarchical filter helpers (for cascading dropdowns)
# ---------------------------------------------------------------------------

def get_databricks_workspaces(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get workspace list for filter dropdown.

    A lineage-first org (e.g. a UC-lineage scan) may not have databricks_meta
    deployed at all — catch that so a missing schema doesn't raise (which would
    otherwise abort the whole platform-filter block in routes/catalog.py and
    mask the other platforms' dropdowns too). There's no lineage-graph
    equivalent of a "workspace" for such orgs (Databricks nodes there have no
    workspace container), so an empty list is the correct answer, not a gap.
    """
    try:
        if view_id:
            return _query(config, """
                SELECT workspace_id, workspace_name, workspace_url
                FROM databricks_meta.workspaces
                WHERE view_id = ?
                ORDER BY workspace_name
            """, (view_id,))
        return _query(config, """
            SELECT workspace_id, workspace_name, workspace_url
            FROM databricks_meta.workspaces
            ORDER BY workspace_name
        """)
    except Exception:
        return []


def get_powerbi_reports(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get report list for filter dropdown.

    Falls back to the lineage graph for orgs with no powerbi_meta inventory
    (e.g. a UC-lineage scan) — same rationale as _search_catalog_from_lineage,
    which this reuses so the fallback lives in one place.
    """
    try:
        if view_id:
            rows = _query(config, """
                SELECT r.report_id, r.report_name, r.report_type,
                       COUNT(DISTINCT d.dataset_id) as dataset_count
                FROM powerbi_meta.reports r
                LEFT JOIN powerbi_meta.datasets d ON r.report_id = d.report_id
                WHERE r.view_id = ?
                GROUP BY r.report_id, r.report_name, r.report_type
                ORDER BY r.report_name
            """, (view_id,))
        else:
            rows = _query(config, """
                SELECT r.report_id, r.report_name, r.report_type,
                       COUNT(DISTINCT d.dataset_id) as dataset_count
                FROM powerbi_meta.reports r
                LEFT JOIN powerbi_meta.datasets d ON r.report_id = d.report_id
                GROUP BY r.report_id, r.report_name, r.report_type
                ORDER BY r.report_name
            """)
    except Exception:
        rows = []
    if rows or not view_id:
        return rows
    lineage_rows = _search_catalog_from_lineage(config, "", "", "report", view_id, limit=500)
    result = []
    for r in lineage_rows:
        cnt = _query(config,
            "SELECT COUNT(DISTINCT source_node_id) AS c FROM lineage_meta.edges "
            "WHERE target_node_id = %s AND relationship_type = 'feeds'", (r["id"],))
        result.append({"report_id": r["id"], "report_name": r["name"], "report_type": None,
                       "dataset_count": cnt[0]["c"] if cnt else 0})
    return result


def get_datasets_for_report(config: CatalogConfig, report_id: int) -> list[dict]:
    """Get datasets belonging to a specific report."""
    return _query(config, """
        SELECT d.dataset_id, d.dataset_name, d.connection_mode,
               d.table_count, d.measure_count, d.relationship_count
        FROM powerbi_meta.datasets d
        WHERE d.report_id = ?
        ORDER BY d.dataset_name
    """, (report_id,))


def get_notebooks_for_workspace(
    config: CatalogConfig, workspace_id: int,
) -> list[dict]:
    """Get notebooks belonging to a specific workspace."""
    return _query(config, """
        SELECT n.notebook_id, n.notebook_name, n.notebook_path,
               n.notebook_language, n.purpose, n.layer, n.last_modified_at
        FROM databricks_meta.notebooks n
        WHERE n.workspace_id = ?
        ORDER BY n.layer, n.notebook_name
    """, (workspace_id,))


def get_tables_for_dataset(config: CatalogConfig, dataset_id: int) -> list[dict]:
    """Get tables for a specific dataset (for tree navigation)."""
    return _query(config, """
        SELECT t.table_id, t.table_name, t.source_type,
               t.column_count, t.row_count, t.is_hidden, t.is_calculated_table
        FROM powerbi_meta.tables t
        WHERE t.dataset_id = ?
        ORDER BY t.table_name
    """, (dataset_id,))


def get_measures_for_dataset(config: CatalogConfig, dataset_id: int) -> list[dict]:
    """Get measures for a specific dataset (for tree navigation)."""
    return _query(config, """
        SELECT m.measure_id, m.measure_name, m.measure_type,
               m.complexity_score, m.table_name
        FROM powerbi_meta.measures m
        WHERE m.dataset_id = ?
        ORDER BY m.table_name, m.measure_name
    """, (dataset_id,))


# ---------------------------------------------------------------------------
# Catalog search
# ---------------------------------------------------------------------------

def _search_catalog_from_lineage(config, query="", platform="", object_type="", view_id=None, limit=50):
    """List catalog objects straight from the lineage graph.

    Fallback for lineage-first orgs (e.g. a UC-lineage scan) whose data lives only
    in ``lineage_meta`` and never reached the per-platform inventory tables the
    normal search reads. Each row links to the lineage-native node detail page so
    the Catalog is navigable uniformly across orgs.
    """
    if not view_id:
        return []
    search_term = f"%{query}%" if query else "%"
    where = ["view_id = %s", "display_name LIKE %s"]
    params = [view_id, search_term]
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if object_type:
        where.append("node_type = %s")
        params.append(object_type)
    params.append(limit)
    # Cap PER node_type (not globally) so every class surfaces — mirrors the
    # platform-meta path, which runs one capped query per type. ROW_NUMBER() is
    # valid on both SQL Server and PostgreSQL.
    sql = (
        "SELECT id, name, object_type, platform, purpose, description FROM ("
        "  SELECT node_id AS id, display_name AS name, node_type AS object_type, "
        "         platform, NULL AS purpose, COALESCE(qualified_name, external_id) AS description, "
        "         ROW_NUMBER() OVER (PARTITION BY node_type ORDER BY display_name) AS rn "
        "  FROM lineage_meta.nodes WHERE " + " AND ".join(where) +
        ") t WHERE rn <= %s ORDER BY object_type, name"
    )
    try:
        rows = _query(config, sql, tuple(params))
    except Exception:
        return []
    for r in rows:
        r["detail_url"] = f"/detail/node/{r['id']}"
    return rows


def get_node_detail(config: CatalogConfig, node_id: int) -> Optional[dict]:
    """A single lineage node with parsed properties (for the node detail page)."""
    rows = _query(
        config,
        "SELECT node_id, display_name, node_type, platform, qualified_name, external_id, properties "
        "FROM lineage_meta.nodes WHERE node_id = %s",
        (node_id,),
    )
    if not rows:
        return None
    r = rows[0]
    props = r.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (ValueError, TypeError):
            props = {}
    r["properties"] = props if isinstance(props, dict) else {}
    return r


def get_node_neighbors(config: CatalogConfig, node_id: int) -> tuple[list[dict], list[dict]]:
    """Direct graph neighbours of a node, split into (upstream, downstream)."""
    rows = _query(
        config,
        """
        SELECT e.source_node_id AS sid, e.target_node_id AS tid, e.relationship_type AS rel,
               ns.display_name AS s_name, ns.node_type AS s_type, ns.platform AS s_plat,
               nt.display_name AS t_name, nt.node_type AS t_type, nt.platform AS t_plat
        FROM lineage_meta.edges e
        JOIN lineage_meta.nodes ns ON e.source_node_id = ns.node_id
        JOIN lineage_meta.nodes nt ON e.target_node_id = nt.node_id
        WHERE e.source_node_id = %s OR e.target_node_id = %s
        """,
        (node_id, node_id),
    )
    upstream, downstream, seen_up, seen_down = [], [], set(), set()
    for r in rows:
        if r["sid"] == node_id and r["tid"] not in seen_down:
            seen_down.add(r["tid"])
            downstream.append({"id": r["tid"], "name": r["t_name"], "type": r["t_type"],
                               "platform": r["t_plat"], "rel": r["rel"]})
        elif r["tid"] == node_id and r["sid"] not in seen_up:
            seen_up.add(r["sid"])
            upstream.append({"id": r["sid"], "name": r["s_name"], "type": r["s_type"],
                             "platform": r["s_plat"], "rel": r["rel"]})
    return upstream, downstream


_PLATFORM_LABELS = {"databricks": "Databricks", "powerbi": "Power BI", "fabric": "Fabric"}


def get_lineage_catalog_facets(config: CatalogConfig, view_id: int) -> tuple[list[dict], list[dict]]:
    """Distinct platforms and node types present in the graph for a view.

    Populates the Catalog filter dropdowns for lineage-first orgs, which have no
    platform-meta inventory for ``get_available_platforms`` to read.
    """
    if not view_id:
        return [], []
    try:
        prows = _query(
            config,
            "SELECT platform, COUNT(*) AS cnt FROM lineage_meta.nodes "
            "WHERE view_id = %s AND platform IS NOT NULL GROUP BY platform ORDER BY platform",
            (view_id,),
        )
        trows = _query(
            config,
            "SELECT node_type, COUNT(*) AS cnt FROM lineage_meta.nodes "
            "WHERE view_id = %s AND node_type IS NOT NULL GROUP BY node_type ORDER BY node_type",
            (view_id,),
        )
    except Exception:
        return [], []
    platforms = [
        {"platform": r["platform"], "label": _PLATFORM_LABELS.get(r["platform"], (r["platform"] or "").title()), "count": r["cnt"]}
        for r in prows
    ]
    types = [{"value": r["node_type"], "count": r["cnt"]} for r in trows]
    return platforms, types


def _search_catalog_backend(config, query, platform, object_type, view_id, limit, workspace_id=None):
    """Search catalog using the database backend abstraction (Supabase / org)."""
    from ade_catalog.core.db_backend import get_active_backend
    backend = get_active_backend(config)
    results = []
    search_term = f"%{query}%" if query else "%"
    # Workspace filter: when set it's Fabric-only (skip PBI/Databricks) and each
    # Fabric block gets `AND w.workspace_id = %s` appended (via {ws} / *wp).
    if workspace_id:
        platform = "fabric"
    ws = " AND w.workspace_id = %s" if workspace_id else ""
    wp = (workspace_id,) if workspace_id else ()

    def _search(sql, params):
        try:
            return backend.query(sql, params)
        except Exception:
            return []

    # Databricks notebooks
    if not platform or platform == "databricks":
        if not object_type or object_type == "notebook":
            if view_id:
                results.extend(_search("""
                    SELECT n.notebook_id as id, n.notebook_name as name, 'notebook' as object_type,
                           'databricks' as platform, n.purpose, n.layer, n.notebook_path as description
                    FROM databricks_meta.notebooks n
                    JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (n.notebook_name LIKE %s OR n.notebook_path LIKE %s)
                    ORDER BY n.notebook_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))
            else:
                results.extend(_search("""
                    SELECT notebook_id as id, notebook_name as name, 'notebook' as object_type,
                           'databricks' as platform, purpose, layer, notebook_path as description
                    FROM databricks_meta.notebooks
                    WHERE notebook_name LIKE %s OR notebook_path LIKE %s
                    ORDER BY notebook_name LIMIT %s
                """, (search_term, search_term, limit)))

    # Databricks table access (unique tables)
    if not platform or platform == "databricks":
        if not object_type or object_type == "table":
            if view_id:
                results.extend(_search("""
                    SELECT MIN(nta.access_id) as id, nta.table_name as name, 'table' as object_type,
                           'databricks' as platform, nta.table_type as purpose, NULL as layer,
                           nta.full_table_name as description
                    FROM databricks_meta.notebook_table_access nta
                    JOIN databricks_meta.notebooks n ON nta.notebook_id = n.notebook_id
                    JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (nta.table_name LIKE %s OR nta.full_table_name LIKE %s)
                    GROUP BY nta.table_name, nta.table_type, nta.full_table_name
                    ORDER BY nta.table_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI datasets
    if not platform or platform == "powerbi":
        if not object_type or object_type == "dataset":
            if view_id:
                results.extend(_search("""
                    SELECT d.dataset_id as id, d.dataset_name as name, 'dataset' as object_type,
                           'powerbi' as platform, d.connection_mode as purpose, NULL as layer, r.report_name as description
                    FROM powerbi_meta.datasets d JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    WHERE r.view_id = %s AND (d.dataset_name LIKE %s OR r.report_name LIKE %s)
                    ORDER BY d.dataset_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI tables
    if not platform or platform == "powerbi":
        if not object_type or object_type == "pbi_table":
            if view_id:
                results.extend(_search("""
                    SELECT t.table_id as id, t.table_name as name, 'pbi_table' as object_type,
                           'powerbi' as platform, t.source_type as purpose, NULL as layer, d.dataset_name as description
                    FROM powerbi_meta.tables t
                    JOIN powerbi_meta.datasets d ON t.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    WHERE r.view_id = %s AND (t.table_name LIKE %s OR d.dataset_name LIKE %s)
                    ORDER BY t.table_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI measures
    if not platform or platform == "powerbi":
        if not object_type or object_type == "measure":
            if view_id:
                results.extend(_search("""
                    SELECT m.measure_id as id, m.measure_name as name, 'measure' as object_type,
                           'powerbi' as platform, m.measure_type as purpose, NULL as layer, d.dataset_name as description
                    FROM powerbi_meta.measures m JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    WHERE r.view_id = %s AND (m.measure_name LIKE %s OR m.dax_expression LIKE %s)
                    ORDER BY m.measure_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI reports
    if not platform or platform == "powerbi":
        if not object_type or object_type == "report":
            if view_id:
                results.extend(_search("""
                    SELECT r.report_id as id, r.report_name as name, 'report' as object_type,
                           'powerbi' as platform, COALESCE(r.report_type,'report') as purpose, NULL as layer,
                           CONCAT(COALESCE(r.page_count,0), ' pages, ', COALESCE(r.visual_count,0), ' visuals') as description
                    FROM powerbi_meta.reports r
                    WHERE r.view_id = %s AND r.report_name LIKE %s
                    ORDER BY r.report_name LIMIT %s
                """, (view_id, search_term, limit)))

    # Power BI report pages
    if not platform or platform == "powerbi":
        if not object_type or object_type == "page":
            if view_id:
                results.extend(_search("""
                    SELECT p.page_id as id, p.page_name as name, 'page' as object_type,
                           'powerbi' as platform, CONCAT(COALESCE(p.visual_count,0), ' visuals') as purpose,
                           NULL as layer, r.report_name as description
                    FROM powerbi_meta.report_pages p
                    JOIN powerbi_meta.reports r ON p.report_id = r.report_id
                    WHERE r.view_id = %s AND (p.page_name LIKE %s OR r.report_name LIKE %s)
                    ORDER BY r.report_name, p.page_number LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI visuals
    if not platform or platform == "powerbi":
        if not object_type or object_type == "visual":
            if view_id:
                results.extend(_search("""
                    SELECT v.visual_id as id, COALESCE(NULLIF(v.visual_name,''), v.visual_type) as name,
                           'visual' as object_type, 'powerbi' as platform, v.visual_type as purpose, NULL as layer,
                           CONCAT(r.report_name, ' / ', p.page_name) as description
                    FROM powerbi_meta.visuals v
                    JOIN powerbi_meta.report_pages p ON v.page_id = p.page_id
                    JOIN powerbi_meta.reports r ON v.report_id = r.report_id
                    WHERE r.view_id = %s AND (v.visual_name LIKE %s OR v.visual_type LIKE %s OR p.page_name LIKE %s)
                    ORDER BY r.report_name, p.page_number LIMIT %s
                """, (view_id, search_term, search_term, search_term, limit)))

    # Power BI relationships (click -> parent dataset)
    if not platform or platform == "powerbi":
        if not object_type or object_type == "relationship":
            if view_id:
                results.extend(_search("""
                    SELECT d.dataset_id as id,
                           CONCAT(COALESCE(ft.table_name,'?'), ' -> ', COALESCE(tt.table_name,'?')) as name,
                           'relationship' as object_type, 'powerbi' as platform, rel.cardinality as purpose,
                           NULL as layer, d.dataset_name as description
                    FROM powerbi_meta.relationships rel
                    JOIN powerbi_meta.datasets d ON rel.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    LEFT JOIN powerbi_meta.tables ft ON rel.from_table_id = ft.table_id
                    LEFT JOIN powerbi_meta.tables tt ON rel.to_table_id = tt.table_id
                    WHERE r.view_id = %s AND (ft.table_name LIKE %s OR tt.table_name LIKE %s)
                    ORDER BY ft.table_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Power BI columns (click -> parent table)
    if not platform or platform == "powerbi":
        if not object_type or object_type == "column":
            if view_id:
                results.extend(_search("""
                    SELECT col.table_id as id, col.column_name as name, 'column' as object_type,
                           'powerbi' as platform, col.data_type as purpose, NULL as layer, t.table_name as description
                    FROM powerbi_meta.columns col
                    JOIN powerbi_meta.tables t ON col.table_id = t.table_id
                    JOIN powerbi_meta.datasets d ON col.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    WHERE r.view_id = %s AND (col.column_name LIKE %s OR t.table_name LIKE %s)
                    ORDER BY t.table_name, col.column_name LIMIT %s
                """, (view_id, search_term, search_term, limit)))

    # Fabric pipelines
    if not platform or platform == "fabric":
        if not object_type or object_type == "pipeline":
            if view_id:
                results.extend(_search(f"""
                    SELECT p.pipeline_id as id, p.pipeline_name as name, 'pipeline' as object_type,
                           'fabric' as platform, p.activity_count::text || ' activities' as purpose, NULL as layer, w.workspace_name as description
                    FROM fabric_infra.pipelines p JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (p.pipeline_name LIKE %s OR w.workspace_name LIKE %s){ws}
                    ORDER BY p.pipeline_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))
            else:
                results.extend(_search("""
                    SELECT p.pipeline_id as id, p.pipeline_name as name, 'pipeline' as object_type,
                           'fabric' as platform, p.activity_count::text || ' activities' as purpose, NULL as layer, w.workspace_name as description
                    FROM fabric_infra.pipelines p JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                    WHERE p.pipeline_name LIKE %s OR w.workspace_name LIKE %s
                    ORDER BY p.pipeline_name LIMIT %s
                """, (search_term, search_term, limit)))

    # Fabric warehouses
    if not platform or platform == "fabric":
        if not object_type or object_type == "warehouse":
            if view_id:
                results.extend(_search(f"""
                    SELECT wh.warehouse_id as id, wh.warehouse_name as name, 'warehouse' as object_type,
                           'fabric' as platform, 'Warehouse' as purpose, NULL as layer, w.workspace_name as description
                    FROM fabric_infra.warehouses wh JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (wh.warehouse_name LIKE %s OR w.workspace_name LIKE %s){ws}
                    ORDER BY wh.warehouse_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))

    # Fabric lakehouses
    if not platform or platform == "fabric":
        if not object_type or object_type == "lakehouse":
            if view_id:
                results.extend(_search(f"""
                    SELECT lh.lakehouse_id as id, lh.lakehouse_name as name, 'lakehouse' as object_type,
                           'fabric' as platform, 'Lakehouse' as purpose, NULL as layer, w.workspace_name as description
                    FROM fabric_infra.lakehouses lh JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (lh.lakehouse_name LIKE %s OR w.workspace_name LIKE %s){ws}
                    ORDER BY lh.lakehouse_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))

    # Fabric notebooks
    if not platform or platform == "fabric":
        if not object_type or object_type == "fab_notebook":
            if view_id:
                results.extend(_search(f"""
                    SELECT nb.notebook_id as id, nb.notebook_name as name, 'fab_notebook' as object_type,
                           'fabric' as platform, 'Notebook' as purpose, NULL as layer, w.workspace_name as description
                    FROM fabric_infra.notebooks nb JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (nb.notebook_name LIKE %s OR w.workspace_name LIKE %s){ws}
                    ORDER BY nb.notebook_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))

    # Fabric lakehouse tables
    if not platform or platform == "fabric":
        if not object_type or object_type == "lh_table":
            if view_id:
                results.extend(_search(f"""
                    SELECT t.table_id as id, t.table_name as name, 'lh_table' as object_type,
                           'fabric' as platform, NULL as purpose, NULL as layer,
                           lh.lakehouse_name || '.' || s.schema_name as description
                    FROM lakehouse_meta.tables t
                    JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id
                    JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id
                    JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (t.table_name LIKE %s OR lh.lakehouse_name LIKE %s){ws}
                    ORDER BY t.table_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))

    # Fabric warehouse tables
    if not platform or platform == "fabric":
        if not object_type or object_type == "wh_table":
            if view_id:
                results.extend(_search(f"""
                    SELECT t.table_id as id, t.table_name as name, 'wh_table' as object_type,
                           'fabric' as platform, NULL as purpose, NULL as layer,
                           wh.warehouse_name || '.' || s.schema_name as description
                    FROM warehouse_meta.tables t
                    JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id
                    JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id
                    JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                    WHERE w.view_id = %s AND (t.table_name LIKE %s OR wh.warehouse_name LIKE %s){ws}
                    ORDER BY t.table_name LIMIT %s
                """, (view_id, search_term, search_term, *wp, limit)))

    # Fabric workspaces (browsable class — land on a workspace to see its contents)
    if not platform or platform == "fabric":
        if not object_type or object_type == "workspace":
            if view_id:
                results.extend(_search(f"""
                    SELECT w.workspace_id as id, w.workspace_name as name, 'workspace' as object_type,
                           'fabric' as platform, 'Workspace' as purpose, NULL as layer,
                           w.capacity_region as description
                    FROM fabric_infra.workspaces w
                    WHERE w.view_id = %s AND w.workspace_name LIKE %s{ws}
                    ORDER BY w.workspace_name LIMIT %s
                """, (view_id, search_term, *wp, limit)))

    # Lineage-first orgs (data only in lineage_meta, e.g. a UC-lineage scan) have
    # no platform-meta inventory — fall back to listing straight from the graph so
    # the Catalog works uniformly across every org.
    if not results:
        results = _search_catalog_from_lineage(config, query, platform, object_type, view_id, limit)
    return results


def search_catalog(
    config: CatalogConfig,
    query: str = "",
    platform: str = "",
    object_type: str = "",
    view_id: int = None,
    limit: int = 50,
    workspace_id: int = None,
) -> list[dict]:
    """
    Search across all catalog objects.
    Returns unified results with: name, type, platform, description, id fields.
    """
    results = []
    search_term = f"%{query}%" if query else "%"
    # A Fabric workspace filter narrows to that workspace (Fabric-only).
    if workspace_id:
        platform = "fabric"

    # Use backend abstraction when on Supabase
    if _use_backend(config):
        return _search_catalog_backend(config, query, platform, object_type, view_id, limit, workspace_id)

    conn = get_connection(_get_db_config(config))
    try:
        cursor = conn.cursor()

        # Only search platform schemas that are actually deployed in this DB, so a
        # partial deployment (e.g. a Fabric+PBI view without databricks_meta) still
        # returns results instead of aborting on the first missing schema.
        def _has(table: str) -> bool:
            # Table-level check (OBJECT_ID): robust even when the schema exists
            # but is empty (no tables), which is the case for a partial deploy.
            try:
                cursor.execute("SELECT OBJECT_ID(?)", (table,))
                return cursor.fetchone()[0] is not None
            except Exception:
                return False

        has_dbx = _has("databricks_meta.notebooks")
        has_pbi = _has("powerbi_meta.reports")
        has_fab = _has("fabric_infra.workspaces")

        # Databricks notebooks
        if (not platform or platform == "databricks") and has_dbx:
            if not object_type or object_type == "notebook":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            n.notebook_id as id,
                            n.notebook_name as name,
                            'notebook' as object_type,
                            'databricks' as platform,
                            n.purpose,
                            n.layer,
                            CAST(n.notebook_path AS NVARCHAR(MAX)) as description
                        FROM databricks_meta.notebooks n
                        JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (n.notebook_name LIKE ? OR CAST(n.notebook_path AS NVARCHAR(MAX)) LIKE ?)
                        ORDER BY n.notebook_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            notebook_id as id,
                            notebook_name as name,
                            'notebook' as object_type,
                            'databricks' as platform,
                            purpose,
                            layer,
                            CAST(notebook_path AS NVARCHAR(MAX)) as description
                        FROM databricks_meta.notebooks
                        WHERE notebook_name LIKE ? OR CAST(notebook_path AS NVARCHAR(MAX)) LIKE ?
                        ORDER BY notebook_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Databricks table access (unique tables)
        if (not platform or platform == "databricks") and has_dbx:
            if not object_type or object_type == "table":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            MIN(nta.access_id) as id,
                            nta.table_name as name,
                            'table' as object_type,
                            'databricks' as platform,
                            nta.table_type as purpose,
                            NULL as layer,
                            nta.full_table_name as description
                        FROM databricks_meta.notebook_table_access nta
                        JOIN databricks_meta.notebooks n ON nta.notebook_id = n.notebook_id
                        JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (nta.table_name LIKE ? OR nta.full_table_name LIKE ?)
                        GROUP BY nta.table_name, nta.table_type, nta.full_table_name
                        ORDER BY nta.table_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            MIN(access_id) as id,
                            table_name as name,
                            'table' as object_type,
                            'databricks' as platform,
                            table_type as purpose,
                            NULL as layer,
                            full_table_name as description
                        FROM databricks_meta.notebook_table_access
                        WHERE table_name LIKE ? OR full_table_name LIKE ?
                        GROUP BY table_name, table_type, full_table_name
                        ORDER BY table_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI datasets
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "dataset":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            d.dataset_id as id,
                            d.dataset_name as name,
                            'dataset' as object_type,
                            'powerbi' as platform,
                            d.connection_mode as purpose,
                            NULL as layer,
                            r.report_name as description
                        FROM powerbi_meta.datasets d
                        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                        WHERE r.view_id = ?
                          AND (d.dataset_name LIKE ? OR r.report_name LIKE ?)
                        ORDER BY d.dataset_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            d.dataset_id as id,
                            d.dataset_name as name,
                            'dataset' as object_type,
                            'powerbi' as platform,
                            d.connection_mode as purpose,
                            NULL as layer,
                            r.report_name as description
                        FROM powerbi_meta.datasets d
                        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                        WHERE d.dataset_name LIKE ? OR r.report_name LIKE ?
                        ORDER BY d.dataset_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI tables
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "table":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'pbi_table' as object_type,
                            'powerbi' as platform,
                            t.source_type as purpose,
                            NULL as layer,
                            d.dataset_name as description
                        FROM powerbi_meta.tables t
                        JOIN powerbi_meta.datasets d ON t.dataset_id = d.dataset_id
                        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                        WHERE r.view_id = ?
                          AND (t.table_name LIKE ? OR d.dataset_name LIKE ?)
                        ORDER BY t.table_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'pbi_table' as object_type,
                            'powerbi' as platform,
                            t.source_type as purpose,
                            NULL as layer,
                            d.dataset_name as description
                        FROM powerbi_meta.tables t
                        JOIN powerbi_meta.datasets d ON t.dataset_id = d.dataset_id
                        WHERE t.table_name LIKE ? OR d.dataset_name LIKE ?
                        ORDER BY t.table_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI measures
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "measure":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            m.measure_id as id,
                            m.measure_name as name,
                            'measure' as object_type,
                            'powerbi' as platform,
                            m.measure_type as purpose,
                            NULL as layer,
                            d.dataset_name as description
                        FROM powerbi_meta.measures m
                        JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
                        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                        WHERE r.view_id = ?
                          AND (m.measure_name LIKE ? OR m.dax_expression LIKE ?)
                        ORDER BY m.measure_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            m.measure_id as id,
                            m.measure_name as name,
                            'measure' as object_type,
                            'powerbi' as platform,
                            m.measure_type as purpose,
                            NULL as layer,
                            d.dataset_name as description
                        FROM powerbi_meta.measures m
                        JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
                        WHERE m.measure_name LIKE ? OR m.dax_expression LIKE ?
                        ORDER BY m.measure_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI reports
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "report":
                base = """
                    SELECT TOP (?) r.report_id as id, r.report_name as name,
                        'report' as object_type, 'powerbi' as platform,
                        ISNULL(r.report_type,'report') as purpose, NULL as layer,
                        CAST(ISNULL(r.page_count,0) AS NVARCHAR(20)) + ' pages, '
                          + CAST(ISNULL(r.visual_count,0) AS NVARCHAR(20)) + ' visuals' as description
                    FROM powerbi_meta.reports r
                    WHERE {where} r.report_name LIKE ?
                    ORDER BY r.report_name
                """
                if view_id:
                    cursor.execute(base.format(where="r.view_id = ? AND"), (limit, view_id, search_term))
                else:
                    cursor.execute(base.format(where=""), (limit, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI report pages
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "page":
                base = """
                    SELECT TOP (?) p.page_id as id, p.page_name as name,
                        'page' as object_type, 'powerbi' as platform,
                        CAST(ISNULL(p.visual_count,0) AS NVARCHAR(20)) + ' visuals' as purpose,
                        NULL as layer, r.report_name as description
                    FROM powerbi_meta.report_pages p
                    JOIN powerbi_meta.reports r ON p.report_id = r.report_id
                    WHERE {where} (p.page_name LIKE ? OR r.report_name LIKE ?)
                    ORDER BY r.report_name, p.page_number
                """
                if view_id:
                    cursor.execute(base.format(where="r.view_id = ? AND"), (limit, view_id, search_term, search_term))
                else:
                    cursor.execute(base.format(where=""), (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI visuals
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "visual":
                base = """
                    SELECT TOP (?) v.visual_id as id,
                        COALESCE(NULLIF(v.visual_name,''), v.visual_type) as name,
                        'visual' as object_type, 'powerbi' as platform,
                        v.visual_type as purpose, NULL as layer,
                        r.report_name + ' / ' + p.page_name as description
                    FROM powerbi_meta.visuals v
                    JOIN powerbi_meta.report_pages p ON v.page_id = p.page_id
                    JOIN powerbi_meta.reports r ON v.report_id = r.report_id
                    WHERE {where} (v.visual_name LIKE ? OR v.visual_type LIKE ? OR p.page_name LIKE ?)
                    ORDER BY r.report_name, p.page_number
                """
                if view_id:
                    cursor.execute(base.format(where="r.view_id = ? AND"), (limit, view_id, search_term, search_term, search_term))
                else:
                    cursor.execute(base.format(where=""), (limit, search_term, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI relationships (click -> parent dataset)
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "relationship":
                base = """
                    SELECT TOP (?) d.dataset_id as id,
                        ISNULL(ft.table_name,'?') + ' -> ' + ISNULL(tt.table_name,'?') as name,
                        'relationship' as object_type, 'powerbi' as platform,
                        rel.cardinality as purpose, NULL as layer, d.dataset_name as description
                    FROM powerbi_meta.relationships rel
                    JOIN powerbi_meta.datasets d ON rel.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    LEFT JOIN powerbi_meta.tables ft ON rel.from_table_id = ft.table_id
                    LEFT JOIN powerbi_meta.tables tt ON rel.to_table_id = tt.table_id
                    WHERE {where} (ft.table_name LIKE ? OR tt.table_name LIKE ?)
                    ORDER BY ft.table_name
                """
                if view_id:
                    cursor.execute(base.format(where="r.view_id = ? AND"), (limit, view_id, search_term, search_term))
                else:
                    cursor.execute(base.format(where=""), (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Power BI columns (click -> parent table)
        if (not platform or platform == "powerbi") and has_pbi:
            if not object_type or object_type == "column":
                base = """
                    SELECT TOP (?) col.table_id as id, col.column_name as name,
                        'column' as object_type, 'powerbi' as platform,
                        col.data_type as purpose, NULL as layer, t.table_name as description
                    FROM powerbi_meta.columns col
                    JOIN powerbi_meta.tables t ON col.table_id = t.table_id
                    JOIN powerbi_meta.datasets d ON col.dataset_id = d.dataset_id
                    JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                    WHERE {where} (col.column_name LIKE ? OR t.table_name LIKE ?)
                    ORDER BY t.table_name, col.column_name
                """
                if view_id:
                    cursor.execute(base.format(where="r.view_id = ? AND"), (limit, view_id, search_term, search_term))
                else:
                    cursor.execute(base.format(where=""), (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Fabric pipelines
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "pipeline":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            p.pipeline_id as id,
                            p.pipeline_name as name,
                            'pipeline' as object_type,
                            'fabric' as platform,
                            CAST(p.activity_count AS NVARCHAR(20)) + ' activities' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.pipelines p
                        JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (p.pipeline_name LIKE ? OR w.workspace_name LIKE ?)
                        ORDER BY p.pipeline_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            p.pipeline_id as id,
                            p.pipeline_name as name,
                            'pipeline' as object_type,
                            'fabric' as platform,
                            CAST(p.activity_count AS NVARCHAR(20)) + ' activities' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.pipelines p
                        JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
                        WHERE p.pipeline_name LIKE ? OR w.workspace_name LIKE ?
                        ORDER BY p.pipeline_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Fabric warehouses
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "warehouse":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            wh.warehouse_id as id,
                            wh.warehouse_name as name,
                            'warehouse' as object_type,
                            'fabric' as platform,
                            'Warehouse' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.warehouses wh
                        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (wh.warehouse_name LIKE ? OR w.workspace_name LIKE ?)
                        ORDER BY wh.warehouse_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            wh.warehouse_id as id,
                            wh.warehouse_name as name,
                            'warehouse' as object_type,
                            'fabric' as platform,
                            'Warehouse' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.warehouses wh
                        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                        WHERE wh.warehouse_name LIKE ? OR w.workspace_name LIKE ?
                        ORDER BY wh.warehouse_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Fabric lakehouses
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "lakehouse":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            lh.lakehouse_id as id,
                            lh.lakehouse_name as name,
                            'lakehouse' as object_type,
                            'fabric' as platform,
                            'Lakehouse' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.lakehouses lh
                        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (lh.lakehouse_name LIKE ? OR w.workspace_name LIKE ?)
                        ORDER BY lh.lakehouse_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            lh.lakehouse_id as id,
                            lh.lakehouse_name as name,
                            'lakehouse' as object_type,
                            'fabric' as platform,
                            'Lakehouse' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.lakehouses lh
                        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                        WHERE lh.lakehouse_name LIKE ? OR w.workspace_name LIKE ?
                        ORDER BY lh.lakehouse_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Fabric notebooks
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "fab_notebook":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            nb.notebook_id as id,
                            nb.notebook_name as name,
                            'fab_notebook' as object_type,
                            'fabric' as platform,
                            'Notebook' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.notebooks nb
                        JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (nb.notebook_name LIKE ? OR w.workspace_name LIKE ?)
                        ORDER BY nb.notebook_name
                    """, (limit, view_id, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            nb.notebook_id as id,
                            nb.notebook_name as name,
                            'fab_notebook' as object_type,
                            'fabric' as platform,
                            'Notebook' as purpose,
                            NULL as layer,
                            w.workspace_name as description
                        FROM fabric_infra.notebooks nb
                        JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
                        WHERE nb.notebook_name LIKE ? OR w.workspace_name LIKE ?
                        ORDER BY nb.notebook_name
                    """, (limit, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Warehouse tables
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "table":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'wh_table' as object_type,
                            'fabric' as platform,
                            s.schema_name as purpose,
                            NULL as layer,
                            wh.warehouse_name + ' / ' + s.schema_name as description
                        FROM warehouse_meta.tables t
                        JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id
                        JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id
                        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (t.table_name LIKE ? OR s.schema_name LIKE ? OR wh.warehouse_name LIKE ?)
                        ORDER BY t.table_name
                    """, (limit, view_id, search_term, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'wh_table' as object_type,
                            'fabric' as platform,
                            s.schema_name as purpose,
                            NULL as layer,
                            wh.warehouse_name + ' / ' + s.schema_name as description
                        FROM warehouse_meta.tables t
                        JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id
                        JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id
                        WHERE t.table_name LIKE ? OR s.schema_name LIKE ? OR wh.warehouse_name LIKE ?
                        ORDER BY t.table_name
                    """, (limit, search_term, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Lakehouse tables
        if (not platform or platform == "fabric") and has_fab:
            if not object_type or object_type == "table":
                if view_id:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'lh_table' as object_type,
                            'fabric' as platform,
                            s.schema_name as purpose,
                            NULL as layer,
                            lh.lakehouse_name + ' / ' + s.schema_name as description
                        FROM lakehouse_meta.tables t
                        JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id
                        JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id
                        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
                        WHERE w.view_id = ?
                          AND (t.table_name LIKE ? OR s.schema_name LIKE ? OR lh.lakehouse_name LIKE ?)
                        ORDER BY t.table_name
                    """, (limit, view_id, search_term, search_term, search_term))
                else:
                    cursor.execute("""
                        SELECT TOP (?)
                            t.table_id as id,
                            t.table_name as name,
                            'lh_table' as object_type,
                            'fabric' as platform,
                            s.schema_name as purpose,
                            NULL as layer,
                            lh.lakehouse_name + ' / ' + s.schema_name as description
                        FROM lakehouse_meta.tables t
                        JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id
                        JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id
                        WHERE t.table_name LIKE ? OR s.schema_name LIKE ? OR lh.lakehouse_name LIKE ?
                        ORDER BY t.table_name
                    """, (limit, search_term, search_term, search_term))
                results.extend(_rows_to_dicts(cursor))

        # Lineage-first orgs (data only in lineage_meta, e.g. a UC-lineage scan)
        # have no platform-meta inventory — every block above is gated behind a
        # has_dbx/has_pbi/has_fab check that's False for them, so `results` stays
        # empty. Mirrors the same fallback in _search_catalog_backend (used when
        # a Flask session org-switch is active) so this legacy path — the one
        # MCP calls actually take, since they carry no browser session — covers
        # lineage-first orgs too.
        if not results:
            results = _search_catalog_from_lineage(config, query, platform, object_type, view_id, limit)
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Object detail queries (no view filter — accessed by direct ID)
# ---------------------------------------------------------------------------

def get_notebook_detail(config: CatalogConfig, notebook_id: int) -> dict | None:
    """Get full notebook detail."""
    rows = _query(config, """
        SELECT
            n.notebook_id, n.notebook_name, n.notebook_path, n.notebook_language,
            n.purpose, n.layer, n.line_count, n.cell_count,
            n.has_widgets, n.has_sql,
            n.created_at, n.last_modified_at,
            w.workspace_name, w.workspace_url, w.view_id,
            j.job_name
        FROM databricks_meta.notebooks n
        JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
        LEFT JOIN databricks_meta.jobs j ON n.parent_job_id = j.job_id
        WHERE n.notebook_id = ?
    """, (notebook_id,))
    return rows[0] if rows else None


def get_notebook_tables(config: CatalogConfig, notebook_id: int) -> list[dict]:
    """Get tables accessed by a notebook."""
    return _query(config, """
        SELECT table_name, full_table_name, table_type, access_type,
               confidence, detected_method
        FROM databricks_meta.notebook_table_access
        WHERE notebook_id = ?
        ORDER BY access_type, table_name
    """, (notebook_id,))


def get_dataset_detail(config: CatalogConfig, dataset_id: int) -> dict | None:
    """Get full dataset detail."""
    rows = _query(config, """
        SELECT
            d.dataset_id, d.dataset_name, d.connection_mode,
            d.table_count, d.relationship_count, d.measure_count,
            d.created_at,
            r.report_name, r.report_type, r.page_count, r.visual_count, r.view_id
        FROM powerbi_meta.datasets d
        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
        WHERE d.dataset_id = ?
    """, (dataset_id,))
    return rows[0] if rows else None


def get_dataset_tables(config: CatalogConfig, dataset_id: int) -> list[dict]:
    """Get tables in a dataset."""
    return _query(config, """
        SELECT table_id, table_name, source_type, source_table,
               row_count, column_count, is_calculated_table, description
        FROM powerbi_meta.tables
        WHERE dataset_id = ?
        ORDER BY table_name
    """, (dataset_id,))


def get_dataset_measures(config: CatalogConfig, dataset_id: int) -> list[dict]:
    """Get measures in a dataset."""
    return _query(config, """
        SELECT measure_id, measure_name, dax_expression, measure_type,
               complexity_score, format_string, description, display_folder
        FROM powerbi_meta.measures
        WHERE dataset_id = ?
        ORDER BY measure_name
    """, (dataset_id,))


def get_dataset_relationships(config: CatalogConfig, dataset_id: int) -> list[dict]:
    """Get relationships in a dataset."""
    return _query(config, """
        SELECT
            r.relationship_id,
            ft.table_name as from_table,
            r.from_column,
            tt.table_name as to_table,
            r.to_column,
            r.cardinality,
            r.cross_filter_direction,
            r.is_active
        FROM powerbi_meta.relationships r
        LEFT JOIN powerbi_meta.tables ft ON r.from_table_id = ft.table_id
        LEFT JOIN powerbi_meta.tables tt ON r.to_table_id = tt.table_id
        WHERE r.dataset_id = ?
        ORDER BY ft.table_name, r.from_column
    """, (dataset_id,))


def get_report_detail(config: CatalogConfig, report_id: int) -> dict | None:
    """Get full Power BI report detail."""
    rows = _query(config, """
        SELECT report_id, report_name, report_type, page_count, visual_count,
               measure_count, table_count, web_url, source_report_id,
               source_workspace_id
        FROM powerbi_meta.reports WHERE report_id = ?
    """, (report_id,))
    return rows[0] if rows else None


def get_report_pages(config: CatalogConfig, report_id: int) -> list[dict]:
    """Get pages of a report."""
    return _query(config, """
        SELECT page_id, page_name, page_number, visual_count
        FROM powerbi_meta.report_pages WHERE report_id = ? ORDER BY page_number
    """, (report_id,))


def get_report_datasets(config: CatalogConfig, report_id: int) -> list[dict]:
    """Get datasets belonging to a report."""
    return _query(config, """
        SELECT dataset_id, dataset_name FROM powerbi_meta.datasets
        WHERE report_id = ? ORDER BY dataset_name
    """, (report_id,))


def get_page_detail(config: CatalogConfig, page_id: int) -> dict | None:
    """Get full report-page detail."""
    rows = _query(config, """
        SELECT p.page_id, p.page_name, p.page_number, p.page_order,
               p.is_hidden, p.page_type, p.width, p.height, p.visual_count,
               r.report_id, r.report_name
        FROM powerbi_meta.report_pages p
        JOIN powerbi_meta.reports r ON p.report_id = r.report_id
        WHERE p.page_id = ?
    """, (page_id,))
    return rows[0] if rows else None


def get_page_visuals(config: CatalogConfig, page_id: int) -> list[dict]:
    """Get visuals on a page."""
    return _query(config, """
        SELECT visual_id, visual_name, visual_type, tables_used, measures_used
        FROM powerbi_meta.visuals WHERE page_id = ? ORDER BY visual_name
    """, (page_id,))


def get_visual_detail(config: CatalogConfig, visual_id: int) -> dict | None:
    """Get full visual detail (with data bindings)."""
    rows = _query(config, """
        SELECT v.visual_id, v.visual_name, v.visual_type,
               v.tables_used, v.measures_used, v.columns_used,
               p.page_id, p.page_name, r.report_id, r.report_name, d.dataset_name
        FROM powerbi_meta.visuals v
        JOIN powerbi_meta.report_pages p ON v.page_id = p.page_id
        JOIN powerbi_meta.reports r ON v.report_id = r.report_id
        LEFT JOIN powerbi_meta.datasets d ON v.dataset_id = d.dataset_id
        WHERE v.visual_id = ?
    """, (visual_id,))
    return rows[0] if rows else None


def get_measure_detail(config: CatalogConfig, measure_id: int) -> dict | None:
    """Get full measure detail.

    Falls back to the lineage graph for orgs with no powerbi_meta inventory
    (e.g. a UC-lineage scan) — the measure's DAX/format/description live on
    the node's properties (written by the lineage-first scan scripts).
    """
    try:
        rows = _query(config, """
            SELECT
                m.measure_id, m.measure_name, m.dax_expression, m.measure_type,
                m.complexity_score, m.format_string, m.description,
                m.display_folder, m.table_name as home_table,
                d.dataset_name,
                r.report_name, r.view_id
            FROM powerbi_meta.measures m
            JOIN powerbi_meta.datasets d ON m.dataset_id = d.dataset_id
            JOIN powerbi_meta.reports r ON d.report_id = r.report_id
            WHERE m.measure_id = ?
        """, (measure_id,))
    except Exception:
        rows = []
    if rows:
        return rows[0]
    return _get_measure_detail_from_lineage(config, measure_id)


def _get_measure_detail_from_lineage(config: CatalogConfig, node_id: int) -> dict | None:
    rows = _query(config,
        "SELECT node_id, display_name, view_id, properties FROM lineage_meta.nodes "
        "WHERE node_id = %s AND node_type = 'measure'", (node_id,))
    if not rows:
        return None
    row = rows[0]
    props = row.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (ValueError, TypeError):
            props = {}
    props = props or {}

    # Dataset name: measure --belongs_to--> semantic_table --belongs_to--> dataset
    # (or directly --belongs_to--> dataset when it has no home table).
    dataset_name = None
    home_rows = _query(config,
        "SELECT target_node_id AS tid FROM lineage_meta.edges "
        "WHERE source_node_id = %s AND relationship_type = 'belongs_to'", (node_id,))
    if home_rows:
        info = _query(config, "SELECT node_type, display_name FROM lineage_meta.nodes WHERE node_id = %s",
                      (home_rows[0]["tid"],))
        if info and info[0]["node_type"] == "dataset":
            dataset_name = info[0]["display_name"]
        elif info:
            ds_rows = _query(config,
                "SELECT n.display_name FROM lineage_meta.edges e "
                "JOIN lineage_meta.nodes n ON n.node_id = e.target_node_id "
                "WHERE e.source_node_id = %s AND e.relationship_type = 'belongs_to' AND n.node_type = 'dataset'",
                (home_rows[0]["tid"],))
            dataset_name = ds_rows[0]["display_name"] if ds_rows else None

    return {
        "measure_id": row["node_id"],
        "measure_name": row["display_name"],
        "dax_expression": props.get("dax_expression"),
        "measure_type": None,
        "complexity_score": None,
        "format_string": props.get("format_string"),
        "description": props.get("description"),
        "display_folder": None,
        "home_table": props.get("home_table"),
        "dataset_name": dataset_name,
        "report_name": None,
        "view_id": row["view_id"],
    }


def get_pbi_table_detail(config: CatalogConfig, table_id: int) -> dict | None:
    """Get full Power BI table detail."""
    rows = _query(config, """
        SELECT
            t.table_id, t.table_name, t.source_type, t.source_database,
            t.source_schema, t.source_table, t.description,
            t.column_count, t.row_count, t.is_calculated_table,
            d.dataset_name, d.dataset_id,
            r.report_name, r.view_id
        FROM powerbi_meta.tables t
        JOIN powerbi_meta.datasets d ON t.dataset_id = d.dataset_id
        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
        WHERE t.table_id = ?
    """, (table_id,))
    return rows[0] if rows else None


def get_table_columns(config: CatalogConfig, table_id: int) -> list[dict]:
    """Get columns for a Power BI table."""
    return _query(config, """
        SELECT column_name, data_type, is_hidden, is_calculated,
               source_column, format_string, is_key, description
        FROM powerbi_meta.columns
        WHERE table_id = ?
        ORDER BY column_name
    """, (table_id,))


def get_all_notebooks(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all notebooks for catalog listing."""
    if view_id:
        return _query(config, """
            SELECT n.notebook_id, n.notebook_name, n.notebook_path,
                   n.notebook_language, n.purpose, n.layer,
                   n.last_modified_at,
                   w.workspace_name
            FROM databricks_meta.notebooks n
            JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            ORDER BY n.layer, n.notebook_name
        """, (view_id,))
    return _query(config, """
        SELECT n.notebook_id, n.notebook_name, n.notebook_path,
               n.notebook_language, n.purpose, n.layer,
               n.last_modified_at,
               w.workspace_name
        FROM databricks_meta.notebooks n
        JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
        ORDER BY n.layer, n.notebook_name
    """)


def get_all_datasets(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all Power BI datasets for catalog listing."""
    if view_id:
        return _query(config, """
            SELECT d.dataset_id, d.dataset_name, d.connection_mode,
                   d.table_count, d.measure_count, d.relationship_count,
                   r.report_name
            FROM powerbi_meta.datasets d
            JOIN powerbi_meta.reports r ON d.report_id = r.report_id
            WHERE r.view_id = ?
            ORDER BY d.dataset_name
        """, (view_id,))
    return _query(config, """
        SELECT d.dataset_id, d.dataset_name, d.connection_mode,
               d.table_count, d.measure_count, d.relationship_count,
               r.report_name
        FROM powerbi_meta.datasets d
        JOIN powerbi_meta.reports r ON d.report_id = r.report_id
        ORDER BY d.dataset_name
    """)


# ---------------------------------------------------------------------------
# Fabric queries
# ---------------------------------------------------------------------------

def get_fabric_stats(config: CatalogConfig, view_id: int = None) -> dict:
    """Get Fabric platform stats for the dashboard.

    Covers the whole Fabric estate: workspaces + their warehouses/lakehouses/
    pipelines/notebooks, plus pipeline activities and lakehouse/warehouse table
    counts — so a Fabric-only view lands on real numbers, not zeros.

    Each count is independent and tolerant of a missing schema: a DB provisioned
    for only part of the Fabric stack (e.g. lakehouses but no warehouse_meta)
    still returns stats for what it has, instead of one failed query taking down
    the whole Fabric panel. Everything is view-scoped via
    fabric_infra.workspaces.view_id.
    """
    def safe(sql, params=None):
        try:
            return _count(config, sql, params)
        except Exception:
            return 0

    stats = {}
    if view_id:
        stats["fabric_workspaces"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.workspaces WHERE view_id = ? AND is_active = 1", (view_id,))
        stats["fabric_warehouses"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.warehouses wh JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_lakehouses"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.lakehouses lh JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_pipelines"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.pipelines p JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_activities"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.pipeline_activities a JOIN fabric_infra.pipelines p ON a.pipeline_id = p.pipeline_id JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_notebooks"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.notebooks n JOIN fabric_infra.workspaces w ON n.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_lakehouse_tables"] = safe("SELECT COUNT(*) as cnt FROM lakehouse_meta.tables t JOIN lakehouse_meta.schemas s ON t.schema_id = s.schema_id JOIN fabric_infra.lakehouses lh ON s.lakehouse_id = lh.lakehouse_id JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
        stats["fabric_warehouse_tables"] = safe("SELECT COUNT(*) as cnt FROM warehouse_meta.tables t JOIN warehouse_meta.schemas s ON t.schema_id = s.schema_id JOIN fabric_infra.warehouses wh ON s.warehouse_id = wh.warehouse_id JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id WHERE w.view_id = ? AND w.is_active = 1", (view_id,))
    else:
        stats["fabric_workspaces"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.workspaces WHERE is_active = 1")
        stats["fabric_warehouses"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.warehouses")
        stats["fabric_lakehouses"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.lakehouses")
        stats["fabric_pipelines"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.pipelines")
        stats["fabric_activities"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.pipeline_activities")
        stats["fabric_notebooks"] = safe("SELECT COUNT(*) as cnt FROM fabric_infra.notebooks")
        stats["fabric_lakehouse_tables"] = safe("SELECT COUNT(*) as cnt FROM lakehouse_meta.tables")
        stats["fabric_warehouse_tables"] = safe("SELECT COUNT(*) as cnt FROM warehouse_meta.tables")
    return stats


def get_fabric_workspaces(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get Fabric workspaces for tree navigation."""
    if view_id:
        return _query(config, """
            SELECT w.workspace_id, w.workspace_name, w.fabric_workspace_id,
                   w.capacity_id,
                   (SELECT COUNT(*) FROM fabric_infra.warehouses WHERE workspace_id = w.workspace_id) as warehouse_count,
                   (SELECT COUNT(*) FROM fabric_infra.lakehouses WHERE workspace_id = w.workspace_id) as lakehouse_count,
                   (SELECT COUNT(*) FROM fabric_infra.pipelines WHERE workspace_id = w.workspace_id) as pipeline_count,
                   (SELECT COUNT(*) FROM fabric_infra.notebooks WHERE workspace_id = w.workspace_id) as notebook_count
            FROM fabric_infra.workspaces w
            WHERE w.view_id = ? AND w.is_active = 1
            ORDER BY w.workspace_name
        """, (view_id,))
    return _query(config, """
        SELECT w.workspace_id, w.workspace_name, w.fabric_workspace_id,
               w.capacity_id,
               (SELECT COUNT(*) FROM fabric_infra.warehouses WHERE workspace_id = w.workspace_id) as warehouse_count,
               (SELECT COUNT(*) FROM fabric_infra.lakehouses WHERE workspace_id = w.workspace_id) as lakehouse_count,
               (SELECT COUNT(*) FROM fabric_infra.pipelines WHERE workspace_id = w.workspace_id) as pipeline_count,
               (SELECT COUNT(*) FROM fabric_infra.notebooks WHERE workspace_id = w.workspace_id) as notebook_count
        FROM fabric_infra.workspaces w
        WHERE w.is_active = 1
        ORDER BY w.workspace_name
    """)


def get_fabric_environments(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Distinct workspace environments present, derived from workspace names.

    Returns an ordered list of {value, label, count, workspace_ids} for the Catalog
    Environment filter. The environment is classified from the name at query time
    (no stored column, no re-scan) — see core.environment / project_views_vs_filters.
    """
    from ade_catalog.core.environment import ENVIRONMENTS, classify_environment, env_label

    if view_id:
        rows = _query(config,
                      "SELECT workspace_id, workspace_name FROM fabric_infra.workspaces "
                      "WHERE view_id = ? AND is_active = 1", (view_id,))
    else:
        rows = _query(config,
                      "SELECT workspace_id, workspace_name FROM fabric_infra.workspaces "
                      "WHERE is_active = 1")

    buckets: dict[str, list[int]] = {}
    for r in rows:
        env = classify_environment(r["workspace_name"])
        buckets.setdefault(env, []).append(r["workspace_id"])

    # Stable order (prod first), then any unexpected bucket alphabetically.
    ordered = ENVIRONMENTS + sorted(k for k in buckets if k not in ENVIRONMENTS)
    return [
        {"value": env, "label": env_label(env),
         "count": len(buckets[env]), "workspace_ids": buckets[env]}
        for env in ordered if env in buckets
    ]


def get_fabric_containers(config: CatalogConfig, workspace_id: int) -> list[dict]:
    """Get warehouses + lakehouses for a workspace (unified container list)."""
    return _query(config, """
        SELECT warehouse_id as container_id, warehouse_name as container_name,
               'Warehouse' as container_type, sql_endpoint
        FROM fabric_infra.warehouses WHERE workspace_id = ?
        UNION ALL
        SELECT lakehouse_id, lakehouse_name, 'Lakehouse', sql_endpoint
        FROM fabric_infra.lakehouses WHERE workspace_id = ?
        ORDER BY container_name
    """, (workspace_id, workspace_id))


def get_fabric_pipelines(config: CatalogConfig, workspace_id: int) -> list[dict]:
    """Get pipelines for a workspace."""
    return _query(config, """
        SELECT pipeline_id, pipeline_name, fabric_pipeline_id,
               activity_count, last_extracted_at
        FROM fabric_infra.pipelines
        WHERE workspace_id = ?
        ORDER BY pipeline_name
    """, (workspace_id,))


def get_fabric_notebooks_for_workspace(config: CatalogConfig, workspace_id: int) -> list[dict]:
    """Get notebooks for a Fabric workspace."""
    return _query(config, """
        SELECT notebook_id, notebook_name, fabric_notebook_id, language
        FROM fabric_infra.notebooks
        WHERE workspace_id = ?
        ORDER BY notebook_name
    """, (workspace_id,))


def get_fabric_schemas(config: CatalogConfig, container_id: int, container_type: str) -> list[dict]:
    """Get schemas for a warehouse or lakehouse."""
    if container_type == "Warehouse":
        return _query(config, """
            SELECT s.schema_id, s.schema_name, s.object_count,
                   (SELECT COUNT(*) FROM warehouse_meta.tables WHERE schema_id = s.schema_id) as table_count,
                   (SELECT COUNT(*) FROM warehouse_meta.views WHERE schema_id = s.schema_id) as view_count,
                   (SELECT COUNT(*) FROM warehouse_meta.procedures WHERE schema_id = s.schema_id) as proc_count
            FROM warehouse_meta.schemas s
            WHERE s.warehouse_id = ?
            ORDER BY s.schema_name
        """, (container_id,))
    else:
        return _query(config, """
            SELECT s.schema_id, s.schema_name, s.table_count, s.total_rows
            FROM lakehouse_meta.schemas s
            WHERE s.lakehouse_id = ?
            ORDER BY s.schema_name
        """, (container_id,))


def get_warehouse_tables(config: CatalogConfig, schema_id: int) -> list[dict]:
    """Get tables for a warehouse schema."""
    return _query(config, """
        SELECT t.table_id, t.table_name, t.table_type, t.column_count, t.row_count
        FROM warehouse_meta.tables t
        WHERE t.schema_id = ?
        ORDER BY t.table_name
    """, (schema_id,))


def get_warehouse_table_columns(config: CatalogConfig, table_id: int) -> list[dict]:
    """Get columns for a warehouse table."""
    return _query(config, """
        SELECT column_name, data_type, max_length, is_nullable, column_default, ordinal_position
        FROM warehouse_meta.columns
        WHERE table_id = ?
        ORDER BY ordinal_position
    """, (table_id,))


def get_warehouse_views(config: CatalogConfig, schema_id: int) -> list[dict]:
    """Get views for a warehouse schema."""
    return _query(config, """
        SELECT wh_view_id, view_name, definition
        FROM warehouse_meta.views
        WHERE schema_id = ?
        ORDER BY view_name
    """, (schema_id,))


def get_warehouse_procedures(config: CatalogConfig, schema_id: int) -> list[dict]:
    """Get procedures for a warehouse schema."""
    return _query(config, """
        SELECT procedure_id, procedure_name, definition, parameter_count
        FROM warehouse_meta.procedures
        WHERE schema_id = ?
        ORDER BY procedure_name
    """, (schema_id,))


def get_lakehouse_tables(config: CatalogConfig, schema_id: int) -> list[dict]:
    """Get tables for a lakehouse schema."""
    return _query(config, """
        SELECT t.table_id, t.table_name, t.row_count, t.size_mb, t.location
        FROM lakehouse_meta.tables t
        WHERE t.schema_id = ?
        ORDER BY t.table_name
    """, (schema_id,))


def get_pipeline_detail(config: CatalogConfig, pipeline_id: int) -> dict | None:
    """Get pipeline details."""
    rows = _query(config, """
        SELECT p.pipeline_id, p.pipeline_name, p.fabric_pipeline_id,
               p.description, p.activity_count, p.last_extracted_at,
               w.workspace_name, w.view_id
        FROM fabric_infra.pipelines p
        JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
        WHERE p.pipeline_id = ?
    """, (pipeline_id,))
    return rows[0] if rows else None


def get_pipeline_activities(config: CatalogConfig, pipeline_id: int) -> list[dict]:
    """Get activities for a pipeline."""
    return _query(config, """
        SELECT activity_id, activity_name, activity_type, category,
               parent_activity_name, nesting_level,
               target_pipeline_id, target_notebook_id,
               source_type, source_object, sink_type, sink_object,
               timeout, retry_count, ordinal_position
        FROM fabric_infra.pipeline_activities
        WHERE pipeline_id = ?
        ORDER BY ordinal_position
    """, (pipeline_id,))


# ---------------------------------------------------------------------------
# Flat listing queries (all objects of a type in a view)
# ---------------------------------------------------------------------------

def get_all_fabric_pipelines(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all pipelines across workspaces for flat listing."""
    if view_id:
        return _query(config, """
            SELECT p.pipeline_id, p.pipeline_name, p.description,
                   p.activity_count, w.workspace_name
            FROM fabric_infra.pipelines p
            JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            ORDER BY w.workspace_name, p.pipeline_name
        """, (view_id,))
    return _query(config, """
        SELECT p.pipeline_id, p.pipeline_name, p.description,
               p.activity_count, w.workspace_name
        FROM fabric_infra.pipelines p
        JOIN fabric_infra.workspaces w ON p.workspace_id = w.workspace_id
        ORDER BY w.workspace_name, p.pipeline_name
    """)


def get_all_fabric_warehouses(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all warehouses across workspaces for flat listing."""
    if view_id:
        return _query(config, """
            SELECT wh.warehouse_id, wh.warehouse_name, wh.sql_endpoint,
                   wh.description, w.workspace_name
            FROM fabric_infra.warehouses wh
            JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            ORDER BY w.workspace_name, wh.warehouse_name
        """, (view_id,))
    return _query(config, """
        SELECT wh.warehouse_id, wh.warehouse_name, wh.sql_endpoint,
               wh.description, w.workspace_name
        FROM fabric_infra.warehouses wh
        JOIN fabric_infra.workspaces w ON wh.workspace_id = w.workspace_id
        ORDER BY w.workspace_name, wh.warehouse_name
    """)


def get_all_fabric_lakehouses(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all lakehouses across workspaces for flat listing."""
    if view_id:
        return _query(config, """
            SELECT lh.lakehouse_id, lh.lakehouse_name, lh.sql_endpoint,
                   lh.onelake_path, lh.description, w.workspace_name
            FROM fabric_infra.lakehouses lh
            JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            ORDER BY w.workspace_name, lh.lakehouse_name
        """, (view_id,))
    return _query(config, """
        SELECT lh.lakehouse_id, lh.lakehouse_name, lh.sql_endpoint,
               lh.onelake_path, lh.description, w.workspace_name
        FROM fabric_infra.lakehouses lh
        JOIN fabric_infra.workspaces w ON lh.workspace_id = w.workspace_id
        ORDER BY w.workspace_name, lh.lakehouse_name
    """)


def get_all_fabric_notebooks(config: CatalogConfig, view_id: int = None) -> list[dict]:
    """Get all Fabric notebooks across workspaces for flat listing."""
    if view_id:
        return _query(config, """
            SELECT nb.notebook_id, nb.notebook_name, nb.description,
                   w.workspace_name
            FROM fabric_infra.notebooks nb
            JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
            WHERE w.view_id = ?
            ORDER BY w.workspace_name, nb.notebook_name
        """, (view_id,))
    return _query(config, """
        SELECT nb.notebook_id, nb.notebook_name, nb.description,
               w.workspace_name
        FROM fabric_infra.notebooks nb
        JOIN fabric_infra.workspaces w ON nb.workspace_id = w.workspace_id
        ORDER BY w.workspace_name, nb.notebook_name
    """)
