"""
Power BI JSON importer.

Reads extracted JSON files (datasets.json) and imports metadata
into the SQL Server powerbi_meta schema.

Uses a full-refresh strategy per dataset: deletes existing records
for each dataset, then re-inserts. This naturally handles deletions
and avoids complex merge logic.

Usage:
    from ade_catalog.core.config import load_config
    from ade_catalog.platforms.powerbi.importers.json_importer import import_powerbi

    config = load_config()
    result = import_powerbi(config)

CLI:
    python -m ade_catalog.platforms.powerbi.importers.json_importer
    python -m ade_catalog.platforms.powerbi.importers.json_importer --instance C:/path/to/instance
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ade_catalog.core.config import CatalogConfig
from ade_catalog.core.database import get_connection
from ade_catalog.core.views import get_default_view_id

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary of an import run."""
    datasets_imported: int = 0
    tables_imported: int = 0
    measures_imported: int = 0
    relationships_imported: int = 0
    data_sources_imported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def _load_json(path: Path) -> dict:
    """Load a JSON file, return empty dict if missing."""
    if not path.exists():
        logger.warning("File not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_file(config: CatalogConfig, key: str) -> Path:
    """Resolve a platform file path from config."""
    platform_cfg = config.platforms.get("powerbi")
    if platform_cfg and key in platform_cfg.settings.get("local", {}):
        relative = platform_cfg.settings["local"][key]
        return config.data_root / relative
    return Path()


def _upsert_report(cursor, view_id: int, dataset: dict) -> int:
    """Ensure a report exists for this dataset within view. Returns report_id."""
    report_name = dataset["name"]

    cursor.execute(
        "SELECT report_id FROM powerbi_meta.reports WHERE view_id = ? AND report_name = ?",
        (view_id, report_name)
    )
    row = cursor.fetchone()
    if row:
        report_id = row[0]
        cursor.execute(
            """UPDATE powerbi_meta.reports
               SET measure_count = ?, table_count = ?, last_analyzed_at = GETDATE()
               WHERE report_id = ?""",
            (len(dataset.get("measures", [])), len(dataset.get("tables", [])), report_id)
        )
        return report_id

    cursor.execute(
        """INSERT INTO powerbi_meta.reports
           (view_id, report_name, report_type, measure_count, table_count, is_active, last_analyzed_at)
           OUTPUT INSERTED.report_id
           VALUES (?, ?, 'pbip', ?, ?, 1, GETDATE())""",
        (view_id, report_name, len(dataset.get("measures", [])), len(dataset.get("tables", [])))
    )
    return int(cursor.fetchone()[0])


def _upsert_dataset(cursor, report_id: int, dataset: dict) -> int:
    """Create or update a dataset record. Returns dataset_id."""
    dataset_name = dataset["name"]

    # Delete existing dataset data (cascades to tables, measures, relationships, etc.)
    cursor.execute(
        "DELETE FROM powerbi_meta.datasets WHERE report_id = ? AND dataset_name = ?",
        (report_id, dataset_name)
    )

    cursor.execute(
        """INSERT INTO powerbi_meta.datasets
           (report_id, dataset_name, connection_mode, table_count,
            relationship_count, measure_count)
           OUTPUT INSERTED.dataset_id
           VALUES (?, ?, 'import', ?, ?, ?)""",
        (
            report_id,
            dataset_name,
            len(dataset.get("tables", [])),
            len(dataset.get("relationships", [])),
            len(dataset.get("measures", [])),
        )
    )
    return int(cursor.fetchone()[0])


def _import_tables(cursor, dataset_id: int, tables: list[dict]) -> int:
    """Import table records. Returns count imported."""
    count = 0
    for tbl in tables:
        source = tbl.get("source", "")
        is_calculated = source == "calculated"
        source_type = "calculated" if is_calculated else "postgres"

        cursor.execute(
            """INSERT INTO powerbi_meta.tables
               (dataset_id, table_name, source_type, source_table,
                row_count, is_calculated_table)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                dataset_id,
                tbl["name"],
                source_type,
                source if not is_calculated else None,
                tbl.get("rows"),
                1 if is_calculated else 0,
            )
        )
        count += 1
    return count


def _get_table_id(cursor, dataset_id: int, table_name: str) -> int | None:
    """Look up table_id by name within a dataset."""
    cursor.execute(
        "SELECT table_id FROM powerbi_meta.tables WHERE dataset_id = ? AND table_name = ?",
        (dataset_id, table_name)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _import_measures(cursor, dataset_id: int, measures: list[dict]) -> int:
    """Import measure records. Returns count imported."""
    count = 0
    for m in measures:
        # Detect measure type from DAX expression
        expr = m.get("expression", "")
        if any(kw in expr.upper() for kw in ("SAMEPERIODLASTYEAR", "TOTALMTD", "TOTALYTD", "DATEADD")):
            measure_type = "time_intelligence"
        elif any(kw in expr.upper() for kw in ("SUM(", "COUNT(", "AVERAGE(", "MIN(", "MAX(")):
            measure_type = "aggregation"
        else:
            measure_type = "calculation"

        # Simple complexity score based on expression length and nesting
        complexity = min(10, max(1, len(expr) // 30 + expr.count("(") - 1))

        cursor.execute(
            """INSERT INTO powerbi_meta.measures
               (dataset_id, measure_name, dax_expression, format_string,
                description, measure_type, complexity_score, display_folder)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dataset_id,
                m["name"],
                expr,
                m.get("format"),
                m.get("description"),
                measure_type,
                complexity,
                m.get("category"),
            )
        )
        count += 1
    return count


def _import_relationships(cursor, dataset_id: int, relationships: list[dict]) -> int:
    """Import relationship records. Returns count imported."""
    count = 0
    for rel in relationships:
        # Parse "TableName.column" format
        from_parts = rel["from"].split(".")
        to_parts = rel["to"].split(".")
        from_table_name = from_parts[0]
        from_column = from_parts[1] if len(from_parts) > 1 else from_parts[0]
        to_table_name = to_parts[0]
        to_column = to_parts[1] if len(to_parts) > 1 else to_parts[0]

        from_table_id = _get_table_id(cursor, dataset_id, from_table_name)
        to_table_id = _get_table_id(cursor, dataset_id, to_table_name)

        # Normalize cardinality format
        cardinality = rel.get("cardinality", "many_to_one").replace("-", "_")

        cursor.execute(
            """INSERT INTO powerbi_meta.relationships
               (dataset_id, from_table_id, from_column, to_table_id, to_column,
                cardinality, cross_filter_direction, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 'single', 1)""",
            (dataset_id, from_table_id, from_column, to_table_id, to_column, cardinality)
        )
        count += 1
    return count


def _import_data_sources(cursor, dataset_id: int, data_sources: list[dict]) -> int:
    """Import data source records. Returns count imported."""
    count = 0
    for ds in data_sources:
        # Parse "schema.table" format from table field
        table_ref = ds.get("table", "")
        parts = table_ref.split(".")
        schema_name = parts[0] if len(parts) == 2 else None
        table_name = parts[-1] if parts else ""

        source_name = f"{ds['type']}:{table_ref}"

        cursor.execute(
            """INSERT INTO powerbi_meta.data_sources
               (dataset_id, source_name, source_type, database_name, connection_mode)
               VALUES (?, ?, ?, ?, 'import')""",
            (dataset_id, source_name, ds["type"], schema_name)
        )
        count += 1
    return count


def import_powerbi(config: CatalogConfig, view_id: int | None = None) -> ImportResult:
    """
    Import Power BI metadata from JSON extraction files into the database.

    Reads datasets.json from the configured extraction path and imports
    into the powerbi_meta schema.

    Args:
        config: Catalog configuration.
        view_id: Target view. Uses default view if not specified.
    """
    result = ImportResult()

    if view_id is None:
        view_id = get_default_view_id(config)

    datasets_path = _resolve_file(config, "datasets_file")
    data = _load_json(datasets_path)

    if not data:
        result.errors.append(f"No data found in {datasets_path}")
        return result

    datasets = data.get("datasets", [])
    if not datasets:
        result.errors.append("No datasets in extraction file")
        return result

    logger.info("Importing %d Power BI datasets (view_id=%d)", len(datasets), view_id)

    conn = get_connection(config.database)
    try:
        cursor = conn.cursor()

        for ds in datasets:
            ds_name = ds["name"]
            logger.info("Importing dataset: %s", ds_name)

            # Upsert report within view
            report_id = _upsert_report(cursor, view_id, ds)

            # Create dataset (deletes existing + cascades)
            dataset_id = _upsert_dataset(cursor, report_id, ds)
            result.datasets_imported += 1

            # Import tables
            tables = ds.get("tables", [])
            if tables:
                result.tables_imported += _import_tables(cursor, dataset_id, tables)

            # Import measures
            measures = ds.get("measures", [])
            if measures:
                result.measures_imported += _import_measures(cursor, dataset_id, measures)

            # Import relationships
            relationships = ds.get("relationships", [])
            if relationships:
                result.relationships_imported += _import_relationships(
                    cursor, dataset_id, relationships
                )

            # Import data sources
            data_sources = ds.get("data_sources", [])
            if data_sources:
                result.data_sources_imported += _import_data_sources(
                    cursor, dataset_id, data_sources
                )

            logger.info(
                "Dataset '%s': %d tables, %d measures, %d relationships, %d sources",
                ds_name,
                len(tables), len(measures), len(relationships), len(data_sources),
            )

        conn.commit()
        logger.info("Power BI import completed successfully")

    except Exception as e:
        conn.rollback()
        result.errors.append(str(e))
        logger.error("Power BI import failed: %s", e)
    finally:
        conn.close()

    return result


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Import Power BI metadata from JSON files")
    parser.add_argument("--instance", help="Path to instance directory")
    args = parser.parse_args()

    from ade_catalog.core.config import load_config
    config = load_config(instance_path=args.instance)

    result = import_powerbi(config)

    if result.success:
        print(f"OK: {result.datasets_imported} datasets, "
              f"{result.tables_imported} tables, "
              f"{result.measures_imported} measures, "
              f"{result.relationships_imported} relationships, "
              f"{result.data_sources_imported} data sources")
    else:
        print(f"FAILED: {result.errors}", file=sys.stderr)
        sys.exit(1)
