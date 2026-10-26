"""
Power BI XMLA/JSON importer.

Reads JSON files produced by the XMLA extractor (or pbip_extractor)
and imports all metadata into the powerbi_meta schema.

Uses MERGE (upsert) pattern from v1 for idempotent imports.
Supports view_id for multi-view partitioning.

Usage:
    python -m ade_catalog.platforms.powerbi.importers.xmla_importer \
        --path ./extractions/my_xmla \
        --view "Contoso Power BI"
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ade_catalog.core.config import CatalogConfig, load_config
from ade_catalog.core.database import get_connection
from ade_catalog.core.views import get_default_view_id, get_or_create_view
from ade_catalog.platforms.powerbi.importers.pbip_importer import _classify_measure

logger = logging.getLogger(__name__)

# Valid source_type values for the CHECK constraint
VALID_SOURCE_TYPES = {
    "postgres", "sql_server", "databricks", "fabric", "odbc", "excel",
    "sharepoint", "calculated", "import", "entity", "directquery",
    "view", "unknown", "other",
}


@dataclass
class ImportResult:
    """Summary of an import run."""
    files_imported: int = 0
    tables_imported: int = 0
    columns_imported: int = 0
    measures_imported: int = 0
    relationships_imported: int = 0
    pages_imported: int = 0
    visuals_imported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def _normalize_source_type(raw: str) -> str:
    """Normalize source_type to a valid CHECK constraint value."""
    if not raw:
        return "unknown"
    normalized = raw.lower().strip()
    return normalized if normalized in VALID_SOURCE_TYPES else "other"


def import_json_file(
    cursor,
    view_id: int,
    json_path: Path,
) -> dict:
    """Import a single JSON file into the DB. Returns summary dict."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    report_name = data.get("name", json_path.stem)
    logger.info("Importing: %s", report_name)

    # Upsert report within view
    cursor.execute(
        "SELECT report_id FROM powerbi_meta.reports WHERE view_id = ? AND report_name = ?",
        (view_id, report_name),
    )
    row = cursor.fetchone()
    web_url = data.get("web_url")
    src_report = data.get("source_report_id")
    src_ws = data.get("source_workspace_id")
    if row:
        report_id = row[0]
        cursor.execute("""
            UPDATE powerbi_meta.reports
            SET measure_count = ?, table_count = ?, last_analyzed_at = GETDATE(),
                web_url = COALESCE(?, web_url),
                source_report_id = COALESCE(?, source_report_id),
                source_workspace_id = COALESCE(?, source_workspace_id)
            WHERE report_id = ?
        """, (data.get("total_measures", 0), data.get("total_tables", 0),
              web_url, src_report, src_ws, report_id))
    else:
        cursor.execute("""
            INSERT INTO powerbi_meta.reports
                (view_id, report_name, report_type, measure_count, table_count,
                 web_url, source_report_id, source_workspace_id,
                 is_active, last_analyzed_at)
            OUTPUT INSERTED.report_id
            VALUES (?, ?, 'pbip', ?, ?, ?, ?, ?, 1, GETDATE())
        """, (view_id, report_name, data.get("total_measures", 0),
              data.get("total_tables", 0), web_url, src_report, src_ws))
        report_id = int(cursor.fetchone()[0])

    # Clear the report layer first: visuals reference dataset_id with ON DELETE
    # NO ACTION, so they must be removed (via report_pages cascade) before the
    # dataset delete below, or that delete fails on the FK.
    cursor.execute("DELETE FROM powerbi_meta.report_pages WHERE report_id = ?", (report_id,))

    # The semantic model keeps its own name (report != model); fall back to the
    # report name when the extract didn't carry a separate dataset name.
    dataset_name = data.get("dataset_name") or report_name

    # Delete existing dataset (cascade cleans tables, columns, measures, rels)
    cursor.execute(
        "DELETE FROM powerbi_meta.datasets WHERE report_id = ? AND dataset_name = ?",
        (report_id, dataset_name),
    )

    # Create dataset
    cursor.execute("""
        INSERT INTO powerbi_meta.datasets
            (report_id, dataset_name, connection_mode, table_count,
             relationship_count, measure_count)
        OUTPUT INSERTED.dataset_id
        VALUES (?, ?, 'import', ?, ?, ?)
    """, (report_id, dataset_name, data.get("total_tables", 0),
          data.get("total_relationships", 0), data.get("total_measures", 0)))
    dataset_id = int(cursor.fetchone()[0])

    # Import tables (incl. upstream source reference for cross-platform lineage)
    table_id_map = {}
    for tbl in data.get("tables", []):
        source_type = _normalize_source_type(tbl.get("source_type", "import"))

        cursor.execute("""
            INSERT INTO powerbi_meta.tables
                (dataset_id, table_name, source_type, source_database,
                 source_schema, source_table, is_hidden, is_calculated_table)
            OUTPUT INSERTED.table_id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (dataset_id, tbl["name"], source_type,
              tbl.get("source_database"), tbl.get("source_schema"),
              tbl.get("source_table"),
              1 if tbl.get("is_hidden") else 0,
              1 if source_type == "calculated" else 0))
        table_id_map[tbl["name"]] = int(cursor.fetchone()[0])

    # Import columns (skip empty names and duplicates)
    col_count = 0
    seen_columns = set()
    for col in data.get("columns", []):
        col_name = col.get("name", "").strip()
        tbl_id = table_id_map.get(col.get("table_name"))
        if not tbl_id or not col_name:
            continue
        # Skip RowNumber internal columns
        if col_name.startswith("RowNumber-"):
            continue
        # Skip duplicates within same table
        col_key = (tbl_id, col_name)
        if col_key in seen_columns:
            continue
        seen_columns.add(col_key)

        cursor.execute("""
            INSERT INTO powerbi_meta.columns
                (table_id, dataset_id, column_name, data_type, is_hidden,
                 is_calculated, source_column, format_string, is_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tbl_id, dataset_id, col_name, col.get("data_type"),
              1 if col.get("is_hidden") else 0,
              1 if str(col.get("source_column", "")).startswith(("[", "=")) else 0,
              col.get("source_column"), col.get("format_string"),
              1 if col.get("is_key") else 0))
        col_count += 1

    # Update column counts
    for tbl_name, tbl_id in table_id_map.items():
        cursor.execute("""
            UPDATE powerbi_meta.tables
            SET column_count = (SELECT COUNT(*) FROM powerbi_meta.columns WHERE table_id = ?)
            WHERE table_id = ?
        """, (tbl_id, tbl_id))

    # Import measures
    measure_count = 0
    for m in data.get("measures", []):
        dax = m.get("dax_expression", "")
        measure_type, complexity = _classify_measure(dax) if dax else ("calculation", 1)

        cursor.execute("""
            INSERT INTO powerbi_meta.measures
                (dataset_id, table_name, measure_name, dax_expression,
                 format_string, is_hidden, display_folder,
                 measure_type, complexity_score, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (dataset_id, m.get("table_name"), m["name"], dax,
              m.get("format_string"), 1 if m.get("is_hidden") else 0,
              m.get("display_folder"), measure_type, complexity,
              m.get("description")))
        measure_count += 1

    # Import relationships
    rel_count = 0
    for rel in data.get("relationships", []):
        from_table_id = table_id_map.get(rel.get("from_table"))
        to_table_id = table_id_map.get(rel.get("to_table"))

        cardinality = rel.get("cardinality", "many_to_one").lower().replace("-", "_")
        if cardinality not in ("one_to_one", "one_to_many", "many_to_one", "many_to_many"):
            cardinality = "many_to_one"

        cross_filter = rel.get("cross_filter_direction", "single").lower()
        if cross_filter not in ("single", "both", "none"):
            cross_filter = "single"

        cursor.execute("""
            INSERT INTO powerbi_meta.relationships
                (dataset_id, from_table_id, from_column, to_table_id, to_column,
                 cardinality, cross_filter_direction, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (dataset_id, from_table_id, rel.get("from_column", ""),
              to_table_id, rel.get("to_column", ""),
              cardinality, cross_filter,
              1 if rel.get("is_active", True) else 0))
        rel_count += 1

    # Import report pages + visuals (the report layer). Present for PBIP /
    # report-definition extracts; empty for pure XMLA model extracts.
    page_id_map = {}  # extractor page_id (str) -> DB page_id
    page_count = 0
    for pg in data.get("report_pages", []):
        ext_pid = pg.get("page_id")
        page_name = pg.get("page_name") or ext_pid or f"Page {page_count + 1}"
        cursor.execute("""
            INSERT INTO powerbi_meta.report_pages
                (report_id, page_name, page_number, page_order, is_hidden, page_type)
            OUTPUT INSERTED.page_id
            VALUES (?, ?, ?, ?, ?, ?)
        """, (report_id, page_name, pg.get("page_number"),
              pg.get("page_order", pg.get("page_number")),
              1 if pg.get("is_hidden") else 0, pg.get("page_type")))
        page_id_map[ext_pid] = int(cursor.fetchone()[0])
        page_count += 1

    visual_count = 0
    for v in data.get("visuals", []):
        db_pid = page_id_map.get(v.get("page_id"))
        if not db_pid:
            continue  # visual without a resolvable page
        cursor.execute("""
            INSERT INTO powerbi_meta.visuals
                (page_id, report_id, dataset_id, visual_name, visual_type,
                 tables_used, measures_used, columns_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (db_pid, report_id, dataset_id, v.get("visual_name"), v.get("visual_type"),
              json.dumps(v.get("tables_used") or []),
              json.dumps(v.get("measures_used") or []),
              json.dumps(v.get("columns_used") or [])))
        visual_count += 1

    # Backfill per-page visual counts.
    for ext_pid, db_pid in page_id_map.items():
        cursor.execute("""
            UPDATE powerbi_meta.report_pages
            SET visual_count = (SELECT COUNT(*) FROM powerbi_meta.visuals WHERE page_id = ?)
            WHERE page_id = ?
        """, (db_pid, db_pid))

    # Roll the page/visual counts up onto the report (used by recap + search).
    cursor.execute(
        "UPDATE powerbi_meta.reports SET page_count = ?, visual_count = ? WHERE report_id = ?",
        (page_count, visual_count, report_id),
    )

    return {
        "name": report_name,
        "tables": len(table_id_map),
        "columns": col_count,
        "measures": measure_count,
        "relationships": rel_count,
        "pages": page_count,
        "visuals": visual_count,
    }


def import_xmla_dir(
    config: CatalogConfig,
    json_dir: Path,
    view_id: Optional[int] = None,
) -> ImportResult:
    """Import all JSON files from a directory into the DB."""
    result = ImportResult()

    if view_id is None:
        view_id = get_default_view_id(config)

    json_files = sorted(json_dir.glob("*.json"))
    if not json_files:
        result.errors.append(f"No JSON files found in {json_dir}")
        return result

    logger.info("Importing %d files into view_id=%d", len(json_files), view_id)

    conn = get_connection(config.database)
    try:
        cursor = conn.cursor()

        for json_file in json_files:
            try:
                summary = import_json_file(cursor, view_id, json_file)
                result.files_imported += 1
                result.tables_imported += summary["tables"]
                result.columns_imported += summary["columns"]
                result.measures_imported += summary["measures"]
                result.relationships_imported += summary["relationships"]
                result.pages_imported += summary.get("pages", 0)
                result.visuals_imported += summary.get("visuals", 0)
                logger.info(
                    "  %s: %dT %dC %dM %dR %dP %dV",
                    summary["name"], summary["tables"], summary["columns"],
                    summary["measures"], summary["relationships"],
                    summary.get("pages", 0), summary.get("visuals", 0),
                )
            except Exception as e:
                result.errors.append(f"{json_file.name}: {e}")
                logger.error("  FAILED %s: %s", json_file.name, e)

        conn.commit()
        logger.info("Import complete: %d files", result.files_imported)

    except Exception as e:
        conn.rollback()
        result.errors.append(str(e))
        logger.error("Import failed: %s", e)
    finally:
        conn.close()

    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Import Power BI XMLA/JSON metadata")
    parser.add_argument("--path", required=True, help="Directory with JSON files")
    parser.add_argument("--view", help="View name (created if not exists)")
    parser.add_argument("--instance", help="Path to instance directory")
    args = parser.parse_args()

    config = load_config(instance_path=args.instance)

    view_id = None
    if args.view:
        view_id = get_or_create_view(config, args.view, view_type="scan")

    result = import_xmla_dir(config, Path(args.path), view_id=view_id)

    if result.success:
        print(f"\nOK: {result.files_imported} files imported")
        print(f"  Tables: {result.tables_imported}")
        print(f"  Columns: {result.columns_imported}")
        print(f"  Measures: {result.measures_imported}")
        print(f"  Relationships: {result.relationships_imported}")
        print(f"  Pages: {result.pages_imported}")
        print(f"  Visuals: {result.visuals_imported}")
    else:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  - {err}")
        sys.exit(1)
