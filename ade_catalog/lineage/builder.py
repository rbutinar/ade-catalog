"""
Lineage graph builder — backend-agnostic foundation.

This is the v2 re-implementation of the v1 LineageGraphBuilder
(C:\\codebase\\ade\\ade_app\\core\\lineage\\build_lineage_graph.py).

Key differences from v1:
- v1 was pyodbc/SQL-Server-only (raw connection, @@IDENTITY, GETDATE()).
- v2 runs on the DatabaseBackend abstraction (ade_catalog.core.db_backend),
  so the SAME builder populates either Supabase (PostgreSQL) or SQL Server.
- To stay portable across both dialects we avoid RETURNING/OUTPUT (whose
  translation is position-sensitive in T-SQL) and instead INSERT then SELECT
  the id by qualified_name (which is UNIQUE in lineage_meta.nodes).

The graph schema (lineage_meta: nodes, edges, entity_types, relationship_types,
recursive traversal functions) is already deployed on both backends and is NOT
managed here — this module only writes nodes/edges into it.

Platform-specific extraction lives in sibling modules (databricks.py, powerbi.py,
fabric.py); each receives a LineageGraphBuilder and calls create_node/create_edge.
"""

import json
import logging
from typing import Any, Optional

from ade_catalog.core.db_backend import DatabaseBackend

logger = logging.getLogger(__name__)

# qualified_name is stored in a non-Unicode SQL Server column (VARCHAR(1000)).
# Some source names (PBI measures especially) carry characters with no exact
# ANSI-codepage mapping (≥, ≤, curly quotes, em-dash, …); the ODBC driver
# silently best-fit-substitutes them on INSERT (e.g. '≥' -> '=') WITHOUT
# raising, so the string actually stored differs from the Python string used
# for the immediate read-back-by-qualified_name lookup in create_node — a
# hard-to-diagnose IndexError ("list index out of range") on an empty result.
# Normalize deterministically up front so the value used for the cache key /
# INSERT / SELECT is always identical to what gets persisted, on both backends.
_UNICODE_QN_MAP = str.maketrans({
    "≥": ">=", "≤": "<=", "≠": "!=",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...",
})


def _ascii_safe_qn(qualified_name: str) -> str:
    """Transliterate a qualified_name to pure ASCII (map common symbols first,
    then replace — not drop — anything left so distinct inputs don't collide)."""
    return qualified_name.translate(_UNICODE_QN_MAP).encode("ascii", "replace").decode("ascii")


# Relationship type -> category. Mirrors the seed data in the lineage_meta
# schema (relationship_types table) and v1's RELATIONSHIP_CATEGORIES map.
RELATIONSHIP_CATEGORIES: dict[str, str] = {
    # lineage (data flow)
    "produces": "lineage",
    "consumes": "lineage",
    "derives_from": "lineage",
    "feeds": "lineage",
    "transforms_to": "lineage",
    "copies_to": "lineage",
    "aggregates": "lineage",
    "joins": "lineage",
    "filters": "lineage",
    "unions": "lineage",
    # composition (internal structure of a process)
    "contains": "composition",
    "executes": "composition",
    "implements": "composition",
    "calls": "composition",
    "includes": "composition",
    "depends_on_task": "composition",
    # containment (organizational hierarchy)
    "belongs_to": "containment",
    "groups": "containment",   # data_product -> member (aggregator; not data-flow)
    "hosted_in": "containment",
    "member_of": "containment",
    "deployed_to": "containment",
    "stored_in": "containment",
    # reference (logical dependencies)
    "uses": "reference",
    "references": "reference",
    "relates_to": "reference",
    "based_on": "reference",
    "visualizes": "reference",
    "displays": "reference",
    "calculates": "reference",
    # orchestration (scheduling / execution dependencies)
    "triggers": "orchestration",
    "schedules": "orchestration",
    "depends_on": "orchestration",
    "notifies": "orchestration",
    "precedes": "orchestration",
    "runs_after": "orchestration",
    "retries": "orchestration",
}


class LineageGraphBuilder:
    """
    Writes nodes and edges into lineage_meta on any configured backend.

    Usage:
        from ade_catalog.core.config import load_config
        from ade_catalog.core.db_backend import get_backend
        from ade_catalog.lineage.builder import LineageGraphBuilder

        backend = get_backend(load_config())
        builder = LineageGraphBuilder(backend)
        builder.clear_lineage()          # optional full rebuild
        # ... platform extractors call builder.create_node / create_edge ...
        builder.compute_metrics()        # refresh upstream/downstream counts
    """

    def __init__(self, backend: DatabaseBackend):
        self.backend = backend
        # qualified_name -> node_id, to avoid duplicate INSERTs within a run
        self.nodes_cache: dict[str, int] = {}
        self._load_existing_nodes()

    # ------------------------------------------------------------------
    # Cache / lifecycle
    # ------------------------------------------------------------------

    def _load_existing_nodes(self) -> None:
        """Prime the cache with nodes already in the graph."""
        rows = self.backend.query(
            "SELECT qualified_name, node_id FROM lineage_meta.nodes WHERE is_active = TRUE"
        )
        for row in rows:
            self.nodes_cache[row["qualified_name"]] = row["node_id"]
        logger.info("Loaded %d existing lineage nodes into cache", len(self.nodes_cache))

    def clear_lineage(self) -> None:
        """Hard-delete all nodes and edges (full rebuild).

        Edges are removed first to respect the FK from edges -> nodes.
        """
        logger.info("Clearing existing lineage data (edges then nodes)...")
        self.backend.execute("DELETE FROM lineage_meta.edges")
        self.backend.execute("DELETE FROM lineage_meta.nodes")
        self.nodes_cache.clear()

    def clear_view(self, view_id: int) -> None:
        """Delete all nodes/edges for a single catalog view (tenant-safe rebuild).

        Edges touching any node of the view are removed first, then the view's
        nodes. Other tenants' lineage is left untouched. The in-memory cache is
        rebuilt afterwards.
        """
        logger.info("Clearing lineage for view_id=%s", view_id)
        self.backend.execute(
            """
            DELETE FROM lineage_meta.edges
            WHERE source_node_id IN (SELECT node_id FROM lineage_meta.nodes WHERE view_id = %s)
               OR target_node_id IN (SELECT node_id FROM lineage_meta.nodes WHERE view_id = %s)
            """,
            (view_id, view_id),
        )
        self.backend.execute(
            "DELETE FROM lineage_meta.nodes WHERE view_id = %s", (view_id,)
        )
        self.nodes_cache.clear()
        self._load_existing_nodes()

    # ------------------------------------------------------------------
    # Node / edge writers
    # ------------------------------------------------------------------

    def create_node(
        self,
        node_type: str,
        display_name: str,
        qualified_name: str,
        platform: str,
        view_id: Optional[int] = None,
        external_id: Optional[str] = None,
        parent_node_id: Optional[int] = None,
        hierarchy_path: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
        source_system: Optional[str] = None,
        owner: Optional[str] = None,
        classification: Optional[str] = None,
        description: Optional[str] = None,
        extraction_method: str = "api",
        processing_mode: str = "static",
        config_dependency: Optional[str] = None,
    ) -> int:
        """Create a node if it does not exist; return its node_id.

        Idempotent on qualified_name (UNIQUE): a second call with the same
        qualified_name returns the existing id without inserting.

        view_id scopes the node to a catalog view (multi-tenant isolation).
        Callers MUST namespace qualified_name per view so names cannot collide
        across tenants (the UNIQUE(qualified_name) constraint is global).
        """
        qualified_name = _ascii_safe_qn(qualified_name)
        # One degenerate name (e.g. a DAX expression leaked as a column name)
        # must not abort a whole build: nodes.display_name is NVARCHAR(500).
        if display_name and len(display_name) > 500:
            display_name = display_name[:497] + "..."

        # In-memory cache first
        if qualified_name in self.nodes_cache:
            return self.nodes_cache[qualified_name]

        # Then the database (node may exist from a previous run)
        existing = self.backend.query(
            "SELECT node_id FROM lineage_meta.nodes WHERE qualified_name = %s",
            (qualified_name,),
        )
        if existing:
            node_id = existing[0]["node_id"]
            self.nodes_cache[qualified_name] = node_id
            return node_id

        properties_json = json.dumps(properties) if properties else None

        self.backend.execute(
            """
            INSERT INTO lineage_meta.nodes (
                node_type, display_name, qualified_name, platform, view_id,
                external_id, parent_node_id, hierarchy_path, properties,
                source_system, owner, classification, description,
                extraction_source, last_seen_at, last_extracted_at,
                processing_mode, config_dependency
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    NOW(), NOW(), %s, %s)
            """,
            (
                node_type, display_name, qualified_name, platform, view_id,
                external_id, parent_node_id, hierarchy_path, properties_json,
                source_system, owner, classification, description,
                extraction_method, processing_mode, config_dependency,
            ),
        )

        # Read back the id (portable across both backends — no RETURNING/OUTPUT)
        row = self.backend.query(
            "SELECT node_id FROM lineage_meta.nodes WHERE qualified_name = %s",
            (qualified_name,),
        )
        node_id = row[0]["node_id"]
        self.nodes_cache[qualified_name] = node_id
        return node_id

    def create_edge(
        self,
        source_node_id: int,
        target_node_id: int,
        relationship_type: str,
        confidence: str = "high",
        properties: Optional[dict[str, Any]] = None,
        source_columns: Optional[list[str]] = None,
        target_columns: Optional[list[str]] = None,
        transformation_logic: Optional[str] = None,
        extraction_method: str = "api",
        lineage_type: str = "explicit",
        config_node_id: Optional[int] = None,
        parameter_expression: Optional[str] = None,
    ) -> bool:
        """Create an edge if it does not already exist.

        Returns True if a new edge was inserted, False if it already existed
        or was a no-op (e.g. a self-loop, which the schema forbids).
        """
        if source_node_id is None or target_node_id is None:
            return False
        if source_node_id == target_node_id:
            # lineage_meta.edges has CK_edges_no_self_loop
            return False

        relationship_category = RELATIONSHIP_CATEGORIES.get(relationship_type, "lineage")

        existing = self.backend.query(
            """
            SELECT edge_id FROM lineage_meta.edges
            WHERE source_node_id = %s AND target_node_id = %s
              AND relationship_type = %s AND is_active = TRUE
            """,
            (source_node_id, target_node_id, relationship_type),
        )
        if existing:
            return False

        self.backend.execute(
            """
            INSERT INTO lineage_meta.edges (
                source_node_id, target_node_id, relationship_type, relationship_category,
                confidence, properties, source_columns, target_columns,
                transformation_logic, extraction_method, last_validated_at,
                lineage_type, config_node_id, parameter_expression
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
            """,
            (
                source_node_id, target_node_id, relationship_type, relationship_category,
                confidence,
                json.dumps(properties) if properties else None,
                json.dumps(source_columns) if source_columns else None,
                json.dumps(target_columns) if target_columns else None,
                transformation_logic, extraction_method,
                lineage_type, config_node_id, parameter_expression,
            ),
        )
        return True

    def get_node_id(self, qualified_name: str) -> Optional[int]:
        """Look up a node id by qualified_name (cache, then DB)."""
        if qualified_name in self.nodes_cache:
            return self.nodes_cache[qualified_name]
        rows = self.backend.query(
            "SELECT node_id FROM lineage_meta.nodes WHERE qualified_name = %s",
            (qualified_name,),
        )
        if rows:
            node_id = rows[0]["node_id"]
            self.nodes_cache[qualified_name] = node_id
            return node_id
        return None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def compute_metrics(self) -> None:
        """Refresh upstream/downstream counts and depths on all nodes.

        On PostgreSQL/Supabase this delegates to the deployed
        lineage_meta.fn_compute_node_metrics() function. On SQL Server the
        equivalent stored procedure is invoked. Safe to skip if absent.
        """
        try:
            if getattr(self.backend, "dialect", "postgres") == "postgres":
                self.backend.execute("SELECT lineage_meta.fn_compute_node_metrics()")
            else:
                self.backend.execute("EXEC lineage_meta.sp_compute_node_metrics")
        except Exception as exc:  # metrics are non-critical for graph correctness
            logger.warning("compute_metrics skipped: %s", exc)

    def stats(self) -> dict[str, int]:
        """Return current node/edge counts (for verification after a build)."""
        nodes = self.backend.query("SELECT COUNT(*) AS c FROM lineage_meta.nodes")
        edges = self.backend.query("SELECT COUNT(*) AS c FROM lineage_meta.edges")
        return {
            "nodes": nodes[0]["c"] if nodes else 0,
            "edges": edges[0]["c"] if edges else 0,
        }
