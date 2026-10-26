"""
Cross-platform lineage linker (view-scoped).

Draws the edges single-platform tools (e.g. Unity Catalog) cannot: a data asset
produced in one platform feeding a consumer in another. This is the core
differentiating value of ade-catalog.

Currently links Power BI semantic tables to the Databricks/Fabric table that
produces them, by matching `powerbi_meta.tables.source_table` (stored on the
node) against the producer table's bare name. Runs after the per-platform
extractors, over the lineage graph already written for the view.

Example (demo): gold_ft_sales (databricks) --feeds--> FactSales (powerbi),
extending the medallion chain into the semantic model and its DAX measures.
"""

import json
import logging
from typing import Optional

from ade_catalog.lineage.builder import LineageGraphBuilder

logger = logging.getLogger(__name__)

# Node types/platforms that can be a producing data asset.
_PRODUCER_TYPES = {"delta_table", "unity_table", "table", "view", "materialized_view"}
# "oracle" added for TICK-003 ACT-004 so name-based matching can link consumers
# to Oracle producers (the precise BO->Oracle link is drawn in
# lineage/businessobjects.py via the resolver; this is the fallback layer).
# "hive" added for TICK-017 so a Tableau datasource / Cloudera-defined Hive table
# can be a producer feeding a Tableau source_table (the Tableau -> Hive link).
_PRODUCER_PLATFORMS = {"databricks", "fabric", "postgresql", "synapse", "oracle", "hive"}


def _loads(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _bare_name(props: dict, display_name: str) -> str:
    return (props.get("table") or display_name.split(".")[-1]).strip().lower()


class CrossPlatformLinker:
    """Link consumer assets to cross-platform producers for one view."""

    def __init__(self, builder: LineageGraphBuilder, view_id: int):
        self.b = builder
        self.view_id = view_id

    def link(self) -> int:
        rows = self.b.backend.query(
            """
            SELECT node_id, node_type, platform, display_name, qualified_name, properties
            FROM lineage_meta.nodes
            WHERE view_id = %s AND is_active = TRUE
            """,
            (self.view_id,),
        )

        producers: dict[str, list[dict]] = {}
        consumers: list[tuple[int, str]] = []
        pbi_cols: dict[tuple, int] = {}   # (pbi_table_lower, pbi_col_lower) -> node_id
        for n in rows:
            props = _loads(n["properties"])
            if n["node_type"] in _PRODUCER_TYPES and n["platform"] in _PRODUCER_PLATFORMS:
                producers.setdefault(_bare_name(props, n["display_name"]), []).append(
                    {"id": n["node_id"], "qn": n["qualified_name"],
                     "platform": n["platform"], "name": n["display_name"]})
            # Power BI tables carrying a source_table reference are consumers.
            if n["node_type"] == "semantic_table" and n["platform"] == "powerbi":
                src = props.get("source_table")
                if src:
                    consumers.append((n["node_id"], src.strip().lower()))
            # Tableau source_table nodes consume the physical producer of the same
            # bare name (the Tableau -> Hive/DB link). TICK-017.
            if n["node_type"] == "source_table" and n["platform"] == "tableau":
                tbl = (props.get("table") or "").strip().lower()
                if tbl:
                    consumers.append((n["node_id"], tbl))
            if n["node_type"] == "column" and n["platform"] == "powerbi":
                ht = (props.get("home_table") or "").strip().lower()
                pbi_cols[(ht, n["display_name"].strip().lower())] = n["node_id"]

        # Table-level bridge: producer table --feeds--> semantic table.
        n_edges = 0
        for consumer_id, src_name in consumers:
            for p in producers.get(src_name, []):
                if self.b.create_edge(p["id"], consumer_id, "feeds",
                                      confidence="medium",
                                      extraction_method="name_match"):
                    n_edges += 1

        # Column-level bridge: the lakehouse column that a PBI column reads from
        # (DirectLake maps 1:1 by name). Creates the lakehouse column node under
        # its producer table and links lakehouse_column --feeds--> pbi_column.
        n_coledges = self._link_columns(producers, pbi_cols)

        logger.info("  cross-platform edges: %d table, %d column (producers=%d, consumers=%d)",
                    n_edges, n_coledges, len(producers), len(consumers))
        return n_edges + n_coledges

    def _link_columns(self, producers: dict, pbi_cols: dict) -> int:
        """Bridge each PBI column to its source (lakehouse) column via
        powerbi_meta.columns.source_column + tables.source_table.

        Lineage-first orgs (e.g. a Contoso-style UC/getDefinition scan that writes
        only lineage_meta, not the powerbi_meta relational tables) have no
        powerbi_meta schema — skip the column-level bridge gracefully instead of
        aborting the whole cross-platform link (the table-level bridge above and
        all per-platform edges are already committed)."""
        try:
            col_map = self.b.backend.query(
                """
                SELECT t.table_name AS pbi_table, t.source_table AS src_table,
                       c.column_name AS pbi_col, c.source_column AS src_col
                FROM powerbi_meta.columns c
                JOIN powerbi_meta.tables t ON c.table_id = t.table_id
                JOIN powerbi_meta.datasets d ON c.dataset_id = d.dataset_id
                JOIN powerbi_meta.reports r ON d.report_id = r.report_id
                WHERE r.view_id = %s
                """,
                (self.view_id,),
            )
        except Exception as exc:
            logger.info("  column-level cross-platform bridge skipped "
                        "(no powerbi_meta relational tables for this view): %s", exc)
            return 0
        n = 0
        for cm in col_map:
            src_table = (cm.get("src_table") or "").strip().lower()
            src_col = (cm.get("src_col") or "").strip()
            if not src_table or not src_col:
                continue
            if src_col.startswith(("=", "[")):
                continue  # calculated column — no physical source counterpart
            pbi_node = pbi_cols.get((
                (cm.get("pbi_table") or "").strip().lower(),
                (cm.get("pbi_col") or "").strip().lower(),
            ))
            if not pbi_node:
                continue
            for p in producers.get(src_table, []):
                lh_col = self.b.create_node(
                    node_type="column", display_name=src_col,
                    qualified_name=f"{p['qn']}.column.{src_col}",
                    platform=p["platform"], view_id=self.view_id,
                    parent_node_id=p["id"], properties={"home_table": p["name"]},
                )
                self.b.create_edge(lh_col, p["id"], "belongs_to")
                if self.b.create_edge(lh_col, pbi_node, "feeds",
                                      confidence="medium", extraction_method="name_match"):
                    n += 1
        return n
