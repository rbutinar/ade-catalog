"""
Power BI PBIP/TMDL importer.

Parses a PBIP project directory using the deterministic TMDL parser
and imports all metadata (tables, columns, measures, relationships,
M queries) into the powerbi_meta schema.

This is the primary import path for Power BI — it extracts far more
detail than the REST API (which only returns dataset/report names).

Usage:
    python -m ade_catalog.platforms.powerbi.importers.pbip_importer \
        --path C:/path/to/Project.pbip \
        --view "My View"

    python -m ade_catalog.platforms.powerbi.importers.pbip_importer \
        --path C:/path/to/Project.SemanticModel
"""

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ade_catalog.core.config import CatalogConfig, load_config
from ade_catalog.core.database import get_connection
from ade_catalog.core.views import get_default_view_id, get_or_create_view
from ade_catalog.platforms.powerbi.extractors.pbip_extractor import extract_all_from_pbip

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary of a PBIP import run."""
    name: str = ""
    tables_imported: int = 0
    columns_imported: int = 0
    measures_imported: int = 0
    relationships_imported: int = 0
    data_sources_imported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def _classify_measure(dax: str) -> tuple[str, int]:
    """Classify a DAX measure by type and complexity. Returns (type, score)."""
    dax_upper = dax.upper()

    if any(kw in dax_upper for kw in ("SAMEPERIODLASTYEAR", "TOTALMTD", "TOTALYTD",
                                       "TOTALQTD", "DATEADD", "PARALLELPERIOD")):
        measure_type = "time_intelligence"
    elif any(kw in dax_upper for kw in ("SUM(", "COUNT(", "AVERAGE(", "MIN(", "MAX(",
                                         "COUNTROWS(", "DISTINCTCOUNT(")):
        measure_type = "aggregation"
    elif "CALCULATE(" in dax_upper:
        measure_type = "calculation"
    elif "DIVIDE(" in dax_upper:
        measure_type = "ratio"
    else:
        measure_type = "calculation"

    # Complexity: length + nesting + function count
    func_count = dax_upper.count("(")
    nesting = max(0, _max_nesting(dax) - 1)
    score = min(10, max(1, len(dax) // 40 + func_count + nesting))

    return measure_type, score


def _max_nesting(expr: str) -> int:
    """Count max parentheses nesting depth."""
    depth = 0
    max_depth = 0
    for c in expr:
        if c == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif c == ")":
            depth -= 1
    return max_depth


def _extract_source_from_m(m_code: str) -> dict:
    """Extract data source details from M (Power Query) expression."""
    result = {
        "source_type": "unknown",
        "server": "",
        "database": "",
        "schema": "",
        "table": "",
    }

    if not m_code:
        return result

    # Databricks.Catalogs("host", "path", [Catalog="cat", Database="db"])
    dbx_match = re.search(
        r'Databricks\.Catalogs\("([^"]+)",\s*"([^"]+)".*?Catalog="([^"]*)".*?Database="([^"]*)"',
        m_code
    )
    if dbx_match:
        result["source_type"] = "databricks"
        result["server"] = dbx_match.group(1)
        result["database"] = dbx_match.group(3)
        result["schema"] = dbx_match.group(4)
        # Table from navigation: Source{[Schema="default",Item="tablename"]}
        item_match = re.search(r'Item="([^"]+)"', m_code)
        if item_match:
            result["table"] = item_match.group(1)
        return result

    # ODBC hierarchical navigation (Impala/Hive over a DSN — the Fabrikam pattern):
    #   Origine = Odbc.DataSource(DSN, [HierarchicalNavigation=true]),
    #   X = Origine{[Name="IMPALA",Kind="Database"]}[Data],
    #   Y = X{[Name="pnrr_dh",Kind="Schema"]}[Data],
    #   Z = Y{[Name="h_advisory",Kind="Table"]}[Data]
    # source_type stays 'odbc' (the CHECK-constrained column has no impala/hive
    # value); the engine is preserved in database ("IMPALA") + schema + table,
    # which is what lineage uses to map the source onto a hive table node.
    nav_table = re.search(r'\{\[Name="([^"]+)",\s*Kind="(?:Table|View)"\]\}', m_code)
    if nav_table:
        nav_db = re.search(r'\{\[Name="([^"]+)",\s*Kind="Database"\]\}', m_code)
        nav_schema = re.search(r'\{\[Name="([^"]+)",\s*Kind="Schema"\]\}', m_code)
        result["source_type"] = "odbc"
        result["database"] = nav_db.group(1) if nav_db else ""
        result["schema"] = nav_schema.group(1) if nav_schema else ""
        result["table"] = nav_table.group(1)
        dsn_match = re.search(r'Odbc\.DataSource\("([^"]+)"', m_code)
        if dsn_match:
            result["server"] = dsn_match.group(1)
        return result

    # Impala.Database("host") native connector (+ {[Schema=...,Item=...]} nav)
    impala_match = re.search(r'Impala\.Database\("([^"]+)"', m_code)
    if impala_match:
        result["source_type"] = "odbc"
        result["server"] = impala_match.group(1)
        result["database"] = "IMPALA"
        sch_item = re.search(r'\{\[Schema="([^"]+)",\s*Item="([^"]+)"\]\}', m_code)
        if sch_item:
            result["schema"] = sch_item.group(1)
            result["table"] = sch_item.group(2)
        return result

    # Sql.Database("server", "database")
    sql_match = re.search(r'Sql\.Database\("([^"]+)",\s*"([^"]+)"', m_code)
    if sql_match:
        result["source_type"] = "sql_server"
        result["server"] = sql_match.group(1)
        result["database"] = sql_match.group(2)
        sch_item = re.search(r'\{\[Schema="([^"]+)",\s*Item="([^"]+)"\]\}', m_code)
        if sch_item:
            result["schema"] = sch_item.group(1)
            result["table"] = sch_item.group(2)
        return result

    # PostgreSQL.Database("server", "database")
    pg_match = re.search(r'PostgreSQL\.Database\("([^"]+)",\s*"([^"]+)"', m_code)
    if pg_match:
        result["source_type"] = "postgres"
        result["server"] = pg_match.group(1)
        result["database"] = pg_match.group(2)
        return result

    # Odbc.DataSource("dsn=...")
    odbc_match = re.search(r'Odbc\.(?:DataSource|Query)\("([^"]+)"', m_code)
    if odbc_match:
        result["source_type"] = "odbc"
        result["server"] = odbc_match.group(1)
        return result

    # Fabric.Warehouse
    if "Fabric.Warehouse" in m_code or "Lakehouse" in m_code:
        result["source_type"] = "fabric"
        return result

    return result


def import_pbip(
    config: CatalogConfig,
    pbip_path: Path,
    view_id: Optional[int] = None,
) -> ImportResult:
    """
    Parse a PBIP project and import all metadata into the database.

    Args:
        config: Catalog configuration.
        pbip_path: Path to .pbip file or .SemanticModel directory.
        view_id: Target view. Uses default view if not specified.
    """
    result = ImportResult()

    if view_id is None:
        view_id = get_default_view_id(config)

    # Parse PBIP
    try:
        parsed = extract_all_from_pbip(pbip_path)
    except Exception as e:
        result.errors.append(f"PBIP parsing failed: {e}")
        return result

    result.name = parsed["name"]
    logger.info("Importing PBIP: %s (view_id=%d)", result.name, view_id)

    conn = get_connection(config.database)
    try:
        cursor = conn.cursor()

        # Upsert report
        report_name = parsed["name"]
        cursor.execute(
            "SELECT report_id FROM powerbi_meta.reports WHERE view_id = ? AND report_name = ?",
            (view_id, report_name)
        )
        row = cursor.fetchone()
        if row:
            report_id = row[0]
            cursor.execute("""
                UPDATE powerbi_meta.reports
                SET measure_count = ?, table_count = ?, last_analyzed_at = GETDATE()
                WHERE report_id = ?
            """, (parsed["total_measures"], parsed["total_tables"], report_id))
        else:
            cursor.execute("""
                INSERT INTO powerbi_meta.reports
                    (view_id, report_name, report_type, measure_count, table_count,
                     page_count, is_active, last_analyzed_at)
                OUTPUT INSERTED.report_id
                VALUES (?, ?, 'pbip', ?, ?, ?, 1, GETDATE())
            """, (view_id, report_name, parsed["total_measures"],
                  parsed["total_tables"], len(parsed.get("report_pages", []))))
            report_id = int(cursor.fetchone()[0])

        # Delete the report layer FIRST: visuals reference datasets without
        # cascade, so they must go (via report_pages ON DELETE CASCADE) before
        # the dataset row can be deleted.
        cursor.execute("DELETE FROM powerbi_meta.report_pages WHERE report_id = ?",
                       (report_id,))

        # Delete existing dataset (cascades to tables, measures, etc.)
        cursor.execute(
            "DELETE FROM powerbi_meta.datasets WHERE report_id = ? AND dataset_name = ?",
            (report_id, report_name)
        )

        # Create dataset
        cursor.execute("""
            INSERT INTO powerbi_meta.datasets
                (report_id, dataset_name, connection_mode, table_count,
                 relationship_count, measure_count)
            OUTPUT INSERTED.dataset_id
            VALUES (?, ?, 'import', ?, ?, ?)
        """, (report_id, report_name, parsed["total_tables"],
              parsed["total_relationships"], parsed["total_measures"]))
        dataset_id = int(cursor.fetchone()[0])

        # Build table name -> id map (skip auto date tables)
        table_id_map = {}
        for tbl in parsed["tables"]:
            tbl_name = tbl["name"]
            is_auto_date = "LocalDateTable" in tbl_name or "DateTableTemplate" in tbl_name

            # Find source from M queries
            source_info = {"source_type": "unknown", "server": "", "database": "",
                          "schema": "", "table": ""}
            for m in parsed.get("transformations", []):
                if m["table_name"] == tbl_name:
                    source_info = _extract_source_from_m(m["m_code"])
                    break

            if tbl.get("source_type") == "calculated" or is_auto_date:
                db_source_type = "calculated"
            elif source_info["source_type"] == "databricks":
                db_source_type = "databricks"
            elif source_info["source_type"] in ("postgres", "sql_server", "odbc"):
                db_source_type = source_info["source_type"]
            else:
                db_source_type = tbl.get("source_type", "other")

            cursor.execute("""
                INSERT INTO powerbi_meta.tables
                    (dataset_id, table_name, source_type, source_database,
                     source_schema, source_table, is_hidden, is_calculated_table)
                OUTPUT INSERTED.table_id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (dataset_id, tbl_name, db_source_type,
                  source_info.get("database") or source_info.get("server"),
                  source_info.get("schema"),
                  source_info.get("table"),
                  1 if tbl.get("is_hidden") else 0,
                  1 if db_source_type == "calculated" else 0))
            table_id = int(cursor.fetchone()[0])
            table_id_map[tbl_name] = table_id
            result.tables_imported += 1

        # Import columns
        for col in parsed["columns"]:
            tbl_id = table_id_map.get(col["table_name"])
            if not tbl_id:
                continue

            cursor.execute("""
                INSERT INTO powerbi_meta.columns
                    (table_id, dataset_id, column_name, data_type, is_hidden,
                     is_calculated, source_column, format_string, is_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tbl_id, dataset_id, col["name"], col["data_type"],
                  1 if col.get("is_hidden") else 0,
                  1 if col.get("source_column", "").startswith(("[", "=")) else 0,
                  col.get("source_column"),
                  col.get("format_string"),
                  1 if col.get("is_key") else 0))
            result.columns_imported += 1

        # Update column counts on tables
        for tbl_name, tbl_id in table_id_map.items():
            cursor.execute("""
                UPDATE powerbi_meta.tables
                SET column_count = (SELECT COUNT(*) FROM powerbi_meta.columns WHERE table_id = ?)
                WHERE table_id = ?
            """, (tbl_id, tbl_id))

        # Import measures
        for m in parsed["measures"]:
            measure_type, complexity = _classify_measure(m["dax_expression"])

            cursor.execute("""
                INSERT INTO powerbi_meta.measures
                    (dataset_id, table_name, measure_name, dax_expression,
                     format_string, is_hidden, display_folder,
                     measure_type, complexity_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (dataset_id, m["table_name"], m["name"], m["dax_expression"],
                  m.get("format_string"), 1 if m.get("is_hidden") else 0,
                  m.get("display_folder"), measure_type, complexity))
            result.measures_imported += 1

        # Import relationships
        for rel in parsed["relationships"]:
            from_table_id = table_id_map.get(rel["from_table"])
            to_table_id = table_id_map.get(rel["to_table"])

            cursor.execute("""
                INSERT INTO powerbi_meta.relationships
                    (dataset_id, from_table_id, from_column, to_table_id, to_column,
                     cardinality, cross_filter_direction, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (dataset_id, from_table_id, rel["from_column"],
                  to_table_id, rel["to_column"],
                  rel.get("cardinality", "many_to_one"),
                  rel.get("cross_filter_direction", "single"),
                  1 if rel.get("is_active", True) else 0))
            result.relationships_imported += 1

        # Import data sources from M queries
        seen_sources = set()
        for m in parsed.get("transformations", []):
            source = _extract_source_from_m(m["m_code"])
            if source["source_type"] == "unknown":
                continue
            source_key = (source["source_type"], source["server"], source["table"])
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            source_name = f"{source['source_type']}:{source.get('table', source.get('server', ''))}"
            cursor.execute("""
                INSERT INTO powerbi_meta.data_sources
                    (dataset_id, source_name, source_type, server,
                     database_name, connection_mode)
                VALUES (?, ?, ?, ?, ?, 'import')
            """, (dataset_id, source_name, source["source_type"],
                  source.get("server"), source.get("database")))
            result.data_sources_imported += 1

        # Report layer (pages + visuals with measure/column bindings) — parity
        # with the XMLA import path; lineage/powerbi.py draws visual nodes from it.
        # (Old pages/visuals were already deleted before the dataset refresh.)
        page_db_ids: dict[str, int] = {}
        seen_page_names: set[str] = set()
        for p in parsed.get("report_pages", []) or []:
            page_name = p["page_name"]
            if page_name in seen_page_names:  # UNIQUE(report_id, page_name)
                page_name = f"{page_name} ({p.get('page_number')})"
            seen_page_names.add(page_name)
            cursor.execute("""
                INSERT INTO powerbi_meta.report_pages
                    (report_id, page_name, page_number, page_order, is_hidden,
                     page_type, width, height)
                OUTPUT INSERTED.page_id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (report_id, page_name, p.get("page_number"), p.get("page_number"),
                  1 if p.get("is_hidden") else 0, p.get("page_type"),
                  p.get("width"), p.get("height")))
            page_db_ids[p["page_id"]] = int(cursor.fetchone()[0])

        n_visuals = 0
        for v in parsed.get("visuals", []) or []:
            page_db_id = page_db_ids.get(v["page_id"])
            if not page_db_id:
                continue
            cursor.execute("""
                INSERT INTO powerbi_meta.visuals
                    (page_id, report_id, dataset_id, visual_name, visual_type,
                     tables_used, measures_used, columns_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (page_db_id, report_id, dataset_id,
                  (v.get("visual_name") or "")[:500] or None, v.get("visual_type"),
                  json.dumps(v.get("tables_used", [])),
                  json.dumps(v.get("measures_used", [])),
                  json.dumps(v.get("columns_used", []))))
            n_visuals += 1
        cursor.execute("""
            UPDATE powerbi_meta.reports
            SET page_count = ?, visual_count = ?
            WHERE report_id = ?
        """, (len(page_db_ids), n_visuals, report_id))

        conn.commit()
        logger.info("PBIP import complete: %d tables, %d columns, %d measures, %d rels, "
                    "%d pages, %d visuals",
                    result.tables_imported, result.columns_imported,
                    result.measures_imported, result.relationships_imported,
                    len(page_db_ids), n_visuals)

    except Exception as e:
        conn.rollback()
        result.errors.append(str(e))
        logger.error("PBIP import failed: %s", e)
    finally:
        conn.close()

    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Import Power BI PBIP project metadata")
    parser.add_argument("--path", required=True, help="Path to .pbip file or .SemanticModel dir")
    parser.add_argument("--view", help="View name (created if not exists)")
    parser.add_argument("--view-id", type=int, help="Target catalog view id (takes precedence)")
    parser.add_argument("--instance", help="Path to instance directory")
    args = parser.parse_args()

    config = load_config(instance_path=args.instance)

    view_id = args.view_id
    if view_id is None and args.view:
        view_id = get_or_create_view(config, args.view, view_type="scan")

    result = import_pbip(config, Path(args.path), view_id=view_id)

    if result.success:
        print(f"OK: {result.name}")
        print(f"  Tables: {result.tables_imported}")
        print(f"  Columns: {result.columns_imported}")
        print(f"  Measures: {result.measures_imported}")
        print(f"  Relationships: {result.relationships_imported}")
        print(f"  Data sources: {result.data_sources_imported}")
    else:
        print(f"FAILED: {result.errors}", file=sys.stderr)
        sys.exit(1)
