"""
Power BI lineage extractor (view-scoped).

Reads from the v2 `powerbi_meta` schema and populates lineage_meta via
LineageGraphBuilder. View-scoped like the other extractors.

Graph produced (per view):
    report --contains--> dataset --contains--> table / measure
    table  --relates_to--> table        (model relationships)
    table  --feeds--> measure           (DAX Table[Column] references)
    measure --feeds--> measure          (DAX [Measure] references)

The measure edges are derived by lightly parsing each measure's DAX expression
(already stored in powerbi_meta.measures.dax_expression) — no extra ingest step
needed. This yields real intra-model lineage (table -> measure -> measure chains).

Cross-platform edges (PBI table -> its Databricks/Fabric source) are a separate
milestone: they require ingest to persist powerbi_meta.tables.source_table
(currently only source_type is stored). See ADR 2026-06-28-lineage-engine-port.
"""

import json
import logging
import re

from ade_catalog.lineage.builder import LineageGraphBuilder

logger = logging.getLogger(__name__)


def _names(raw) -> list[str]:
    """Parse a visual binding (JSON array string, list, or delimited string)
    into a list of name strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val if x]
    except (ValueError, TypeError):
        pass
    return [s for s in re.split(r"[,\s]+", str(raw)) if s]

# DAX reference patterns.
#   Table[Column]  or  'Table Name'[Column]   -> a column/table reference
_COL_REF = re.compile(r"(?:'([^']+)'|([A-Za-z_]\w*))\s*\[[^\]]+\]")
#   [Measure]  (a bracketed name NOT preceded by a table identifier)
_BRACKET = re.compile(r"\[([^\]]+)\]")


def _dax_table_refs(expr: str) -> set[str]:
    """Table names referenced as Table[Column] in a DAX expression."""
    refs = set()
    for m in _COL_REF.finditer(expr or ""):
        refs.add((m.group(1) or m.group(2)).strip())
    return refs


#   Table[Column] / 'Table Name'[Column]  -> capture BOTH table and column
_COL_REF_FULL = re.compile(r"(?:'([^']+)'|([A-Za-z_]\w*))\s*\[([^\]]+)\]")


def _dax_column_refs(expr: str) -> set[tuple[str, str]]:
    """(table, column) pairs referenced as Table[Column] in a DAX expression."""
    refs = set()
    for m in _COL_REF_FULL.finditer(expr or ""):
        table = (m.group(1) or m.group(2)).strip()
        column = m.group(3).strip()
        refs.add((table, column))
    return refs


def _dax_measure_refs(expr: str) -> set[str]:
    """Bare [Measure] references, after removing Table[Column] references."""
    if not expr:
        return set()
    stripped = _COL_REF.sub(" ", expr)
    return {m.strip() for m in _BRACKET.findall(stripped)}


class PowerBILineageExtractor:
    """Populate Power BI lineage for one catalog view."""

    def __init__(self, builder: LineageGraphBuilder, view_id: int):
        self.b = builder
        self.view_id = view_id
        self._prefix = f"v{view_id}.powerbi"
        self._hive_schemas: dict[str, int] = {}

    def _hive_source_node(self, schema: str, table: str) -> int:
        """Physical hive/impala table a PBI table reads (ODBC navigation source).

        Reuses the shared ``v{view}.hive.{schema}.{table}`` convention (same as
        lineage/cloudera.py and lineage/talend.py) so the node dedups with the
        one a DDL parse or an ETL connector already created — and is created as
        an external fallback when no other connector covers that schema (e.g.
        the Fabrikam pnrr_dh world, reached only by Power BI)."""
        key = schema.lower()
        if key not in self._hive_schemas:
            self._hive_schemas[key] = self.b.create_node(
                node_type="schema", display_name=schema,
                qualified_name=f"v{self.view_id}.hive.{schema}",
                platform="hive", view_id=self.view_id,
                properties={"schema": schema},
            )
        schema_node = self._hive_schemas[key]
        node = self.b.create_node(
            node_type="table", display_name=f"{schema}.{table}",
            qualified_name=f"v{self.view_id}.hive.{schema}.{table}",
            platform="hive", view_id=self.view_id, parent_node_id=schema_node,
            properties={"schema": schema, "table": table},
            extraction_method="code_parsing",
        )
        self.b.create_edge(node, schema_node, "belongs_to")
        return node

    def _qn(self, *parts) -> str:
        return ".".join([self._prefix, *[str(p) for p in parts]])

    def _node(self, **kwargs) -> int:
        return self.b.create_node(view_id=self.view_id, **kwargs)

    # ------------------------------------------------------------------

    def build(self) -> dict:
        logger.info("Building Power BI lineage for view_id=%s", self.view_id)
        datasets = self.b.backend.query(
            """
            SELECT d.dataset_id, d.dataset_name, d.connection_mode,
                   d.table_count AS ds_table_count, d.measure_count AS ds_measure_count,
                   d.relationship_count,
                   r.report_id, r.report_name, r.web_url, r.report_type,
                   r.page_count, r.visual_count, r.measure_count, r.table_count,
                   r.source_report_id, r.source_workspace_id
            FROM powerbi_meta.datasets d
            JOIN powerbi_meta.reports r ON d.report_id = r.report_id
            WHERE r.view_id = %s
            """,
            (self.view_id,),
        )
        n_tab = n_meas = n_rel = n_dax = n_vis = n_visedge = n_page = 0
        n_col = n_coledge = n_daxcol = n_src = 0
        for ds in datasets:
            report = ds["report_name"]
            dsn = ds["dataset_name"]

            report_node = self._node(
                node_type="report", display_name=report,
                qualified_name=self._qn(report), platform="powerbi",
                external_id=str(ds["report_id"]) if ds.get("report_id") is not None else None,
                properties={"web_url": ds.get("web_url"), "report_type": ds.get("report_type"),
                            "page_count": ds.get("page_count"), "visual_count": ds.get("visual_count"),
                            "measure_count": ds.get("measure_count"), "table_count": ds.get("table_count"),
                            "dataset": dsn,
                            "source_report_id": ds.get("source_report_id"),
                            "source_workspace_id": ds.get("source_workspace_id")},
            )
            ds_node = self._node(
                node_type="dataset", display_name=dsn,
                qualified_name=self._qn(report, "dataset", dsn), platform="powerbi",
                external_id=str(ds["dataset_id"]) if ds.get("dataset_id") is not None else None,
                properties={"connection_mode": ds.get("connection_mode"),
                            "table_count": ds.get("ds_table_count"),
                            "measure_count": ds.get("ds_measure_count"),
                            "relationship_count": ds.get("relationship_count"),
                            "report": report},
            )
            # The dataset (semantic model) is NOT contained by the report — it
            # FEEDS it (and can feed several reports). Data flows dataset -> report.
            self.b.create_edge(ds_node, report_node, "feeds")

            # Tables (keyed by name within this dataset for DAX resolution)
            tbl_nodes: dict[str, int] = {}
            tables = self.b.backend.query(
                """SELECT table_id, table_name, source_type, source_table,
                          source_schema, source_database
                   FROM powerbi_meta.tables WHERE dataset_id = %s""",
                (ds["dataset_id"],),
            )
            for t in tables:
                node = self._node(
                    node_type="semantic_table", display_name=t["table_name"],
                    qualified_name=self._qn(report, dsn, "table", t["table_name"]),
                    platform="powerbi", parent_node_id=ds_node,
                    external_id=str(t["table_id"]) if t.get("table_id") is not None else None,
                    properties={"source_type": t.get("source_type"),
                                "source_table": t.get("source_table"),
                                "source_schema": t.get("source_schema")},
                )
                self.b.create_edge(node, ds_node, "belongs_to")
                tbl_nodes[t["table_name"]] = node
                n_tab += 1

                # ODBC/Impala navigation source: precise (database=IMPALA/HIVE,
                # schema, table) -> draw hive_table --feeds--> semantic_table
                # directly (no name-match needed; TICK-017 ACT-004 PBI leg).
                src_db = (t.get("source_database") or "").strip().upper()
                src_schema = (t.get("source_schema") or "").strip()
                src_table = (t.get("source_table") or "").strip()
                if src_db in ("IMPALA", "HIVE") and src_schema and src_table:
                    hive_node = self._hive_source_node(src_schema, src_table)
                    if self.b.create_edge(hive_node, node, "feeds",
                                          extraction_method="code_parsing"):
                        n_src += 1

            # Measures (keyed by name for DAX measure-to-measure resolution)
            meas_nodes: dict[str, int] = {}
            measures = self.b.backend.query(
                "SELECT measure_id, table_name, measure_name, dax_expression, format_string, "
                "display_folder, description FROM powerbi_meta.measures WHERE dataset_id = %s",
                (ds["dataset_id"],),
            )
            for m in measures:
                node = self._node(
                    node_type="measure", display_name=m["measure_name"],
                    qualified_name=self._qn(report, dsn, "measure", m["measure_name"]),
                    platform="powerbi", parent_node_id=ds_node,
                    external_id=str(m["measure_id"]) if m.get("measure_id") is not None else None,
                    properties={"home_table": m.get("table_name"),
                                "dax_expression": m.get("dax_expression"),
                                "format_string": m.get("format_string"),
                                "display_folder": m.get("display_folder"),
                                "description": m.get("description")},
                )
                # measure belongs to its home table if known, else the dataset
                home = tbl_nodes.get(m.get("table_name"))
                self.b.create_edge(node, home or ds_node, "belongs_to")
                meas_nodes[m["measure_name"]] = node
                n_meas += 1

            # Columns (nodes) — keyed by (table_name, column_name). Each belongs to
            # its table; used to resolve a visual's field bindings (column-level).
            col_nodes: dict[tuple, int] = {}
            columns = self.b.backend.query(
                "SELECT c.column_name, c.data_type, c.is_key, c.is_calculated, "
                "c.description, t.table_name "
                "FROM powerbi_meta.columns c "
                "JOIN powerbi_meta.tables t ON c.table_id = t.table_id "
                "WHERE c.dataset_id = %s",
                (ds["dataset_id"],),
            )
            for col in columns:
                tname = col["table_name"]
                cname = col["column_name"]
                props = {"home_table": tname, "data_type": col.get("data_type")}
                if col.get("is_key"):
                    props["is_key"] = True
                if col.get("is_calculated"):
                    props["is_calculated"] = True
                if col.get("description"):
                    props["description"] = col["description"]
                cnode = self._node(
                    node_type="column", display_name=cname,
                    qualified_name=self._qn(report, dsn, "column", tname, cname),
                    platform="powerbi", parent_node_id=tbl_nodes.get(tname),
                    properties=props,
                )
                home = tbl_nodes.get(tname)
                if home:
                    self.b.create_edge(cnode, home, "belongs_to")
                col_nodes[(tname, cname)] = cnode
                n_col += 1

            # DAX-derived lineage: table -> measure, measure -> measure
            for m in measures:
                tgt = meas_nodes.get(m["measure_name"])
                if not tgt:
                    continue
                expr = m.get("dax_expression") or ""
                for tname in _dax_table_refs(expr):
                    src = tbl_nodes.get(tname)
                    if src and self.b.create_edge(src, tgt, "feeds",
                                                  extraction_method="code_parsing"):
                        n_dax += 1
                # column-level: Table[Column] refs -> column --feeds--> measure
                for (tname, cname) in _dax_column_refs(expr):
                    csrc = col_nodes.get((tname, cname))
                    if csrc and self.b.create_edge(csrc, tgt, "feeds",
                                                   extraction_method="code_parsing"):
                        n_daxcol += 1
                for mname in _dax_measure_refs(expr):
                    if mname == m["measure_name"]:
                        continue
                    src = meas_nodes.get(mname)
                    if src and self.b.create_edge(src, tgt, "feeds",
                                                  extraction_method="code_parsing"):
                        n_dax += 1

            # Model relationships: from_table -> to_table
            rels = self.b.backend.query(
                """
                SELECT ft.table_name AS from_t, tt.table_name AS to_t,
                       r.from_column, r.to_column
                FROM powerbi_meta.relationships r
                JOIN powerbi_meta.tables ft ON r.from_table_id = ft.table_id
                JOIN powerbi_meta.tables tt ON r.to_table_id = tt.table_id
                WHERE r.dataset_id = %s AND r.is_active = TRUE
                """,
                (ds["dataset_id"],),
            )
            for rel in rels:
                a = tbl_nodes.get(rel["from_t"])
                b = tbl_nodes.get(rel["to_t"])
                if a and b and self.b.create_edge(
                    a, b, "relates_to",
                    source_columns=[rel["from_column"]] if rel.get("from_column") else None,
                    target_columns=[rel["to_column"]] if rel.get("to_column") else None,
                ):
                    n_rel += 1

            # Report-layer lineage: visual nodes + table/measure -> visual edges.
            # Mirrors v1 extract_powerbi_visuals.py. Bindings come from the report
            # definition (PBIP / report-def API); empty for pure XMLA model extracts.
            # Pages: report --contains--> page --contains--> visual.
            page_nodes: dict[str, int] = {}
            pages = self.b.backend.query(
                "SELECT page_name, page_number, visual_count "
                "FROM powerbi_meta.report_pages WHERE report_id = %s",
                (ds["report_id"],),
            )
            for pg in pages:
                pgname = pg["page_name"]
                pnode = self._node(
                    node_type="page", display_name=pgname,
                    qualified_name=self._qn(report, "page", pgname),
                    platform="powerbi", parent_node_id=report_node,
                    properties={"page_number": pg.get("page_number"),
                                "visual_count": pg.get("visual_count")},
                )
                self.b.create_edge(pnode, report_node, "belongs_to")
                page_nodes[pgname] = pnode
                n_page += 1

            visuals = self.b.backend.query(
                """
                SELECT v.visual_name, v.visual_type, v.tables_used, v.measures_used,
                       p.page_name
                FROM powerbi_meta.visuals v
                JOIN powerbi_meta.report_pages p ON v.page_id = p.page_id
                WHERE v.dataset_id = %s
                """,
                (ds["dataset_id"],),
            )
            for v in visuals:
                vname = v.get("visual_name") or "(unnamed)"
                page = v.get("page_name") or ""
                # A visual is CONTAINED by its page (which belongs to the report),
                # not the dataset — the dataset only supplies the data it consumes.
                container = page_nodes.get(page, report_node)
                vnode = self._node(
                    node_type="visual",
                    display_name=f"{page}/{vname}" if page else vname,
                    qualified_name=self._qn(report, dsn, "visual", page, vname),
                    platform="powerbi", parent_node_id=container,
                    properties={"visual_type": v.get("visual_type"), "page": page},
                )
                self.b.create_edge(vnode, container, "belongs_to")
                n_vis += 1
                # table --feeds--> visual
                for tname in _names(v.get("tables_used")):
                    src = tbl_nodes.get(tname)
                    if src and self.b.create_edge(src, vnode, "feeds",
                                                  confidence="medium",
                                                  extraction_method="report_binding"):
                        n_visedge += 1
                # measures_used holds mixed field refs "Table.Field" (measures AND
                # columns). Resolve each to a measure first, else a column.
                for ref in _names(v.get("measures_used")):
                    if "." in ref:
                        tpart, field = ref.rsplit(".", 1)
                    else:
                        tpart, field = None, ref
                    msrc = meas_nodes.get(field)
                    if msrc:
                        if self.b.create_edge(msrc, vnode, "feeds", confidence="medium",
                                              extraction_method="report_binding"):
                            n_visedge += 1
                        continue
                    csrc = col_nodes.get((tpart, field))
                    if csrc and self.b.create_edge(csrc, vnode, "feeds", confidence="medium",
                                                   extraction_method="report_binding"):
                        n_coledge += 1

        if n_src:
            logger.info("  ODBC/Impala source edges (hive_table -> semantic_table): %d", n_src)
        logger.info("  datasets: %d, tables: %d, columns: %d, measures: %d, dax table-edges: %d, "
                    "dax column-edges: %d, relationships: %d, pages: %d, visuals: %d, "
                    "measure-edges: %d, column-edges: %d",
                    len(datasets), n_tab, n_col, n_meas, n_dax, n_daxcol, n_rel, n_page, n_vis,
                    n_visedge, n_coledge)
        return self.b.stats()
