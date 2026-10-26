"""
ade-catalog ingest — Import extracted JSON into Supabase (or SQL Server).

Uses the database backend abstraction, so works with both backends.
Full-refresh strategy: clears existing data for the view, then re-imports.

Usage:
    python -m ade_catalog.ingest --source demo
    python -m ade_catalog.ingest --source /path/to/extractions --view "Acme Fabric"
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from ade_catalog.core.config import load_config
from ade_catalog.core.db_backend import get_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_json(path: Path) -> list | dict:
    if not path.exists():
        logger.warning("File not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Databricks importer
# ---------------------------------------------------------------------------

def ingest_databricks(backend, view_id: int, extractions_dir: Path) -> dict:
    """Import Databricks metadata from JSON files."""
    result = {"notebooks": 0, "table_access": 0, "jobs": 0, "job_tasks": 0}

    notebooks_data = _load_json(extractions_dir / "databricks" / "notebooks.json")
    jobs_data = _load_json(extractions_dir / "databricks" / "jobs.json")
    tables_data = _load_json(extractions_dir / "databricks" / "tables.json")

    if not notebooks_data and not jobs_data:
        logger.info("No Databricks data found in %s", extractions_dir)
        return result

    # Determine workspace name
    ws_name = notebooks_data.get("workspace_url", "demo-workspace") if isinstance(notebooks_data, dict) else "demo-workspace"

    # Upsert workspace
    existing = backend.query(
        "SELECT workspace_id FROM databricks_meta.workspaces WHERE view_id = %s AND workspace_url = %s",
        (view_id, ws_name)
    )
    if existing:
        ws_id = existing[0]["workspace_id"]
        backend.execute("UPDATE databricks_meta.workspaces SET last_analyzed_at = NOW() WHERE workspace_id = %s", (ws_id,))
    else:
        rows = backend.execute_returning("""
            INSERT INTO databricks_meta.workspaces (view_id, workspace_name, workspace_url, workspace_type, last_analyzed_at)
            VALUES (%s, %s, %s, 'production', NOW()) RETURNING workspace_id
        """, (view_id, ws_name, ws_name))
        ws_id = rows[0]["workspace_id"]

    logger.info("Workspace: %s (id=%s)", ws_name, ws_id)

    # Clear existing data
    backend.execute("DELETE FROM databricks_meta.notebook_table_access WHERE notebook_id IN (SELECT notebook_id FROM databricks_meta.notebooks WHERE workspace_id = %s)", (ws_id,))
    backend.execute("UPDATE databricks_meta.notebooks SET parent_job_id = NULL WHERE workspace_id = %s", (ws_id,))
    backend.execute("DELETE FROM databricks_meta.job_tasks WHERE job_id IN (SELECT job_id FROM databricks_meta.jobs WHERE workspace_id = %s)", (ws_id,))
    backend.execute("DELETE FROM databricks_meta.jobs WHERE workspace_id = %s", (ws_id,))
    backend.execute("DELETE FROM databricks_meta.notebooks WHERE workspace_id = %s", (ws_id,))

    # Import notebooks
    notebooks = notebooks_data.get("notebooks", []) if isinstance(notebooks_data, dict) else notebooks_data
    for nb in notebooks:
        layer = nb.get("layer", "none")
        # Map layers to valid check constraint values
        valid_layers = ("bronze", "silver", "gold", "utility", "none")
        if layer not in valid_layers:
            layer = "utility"
        purpose_map = {"bronze": "ingestion", "silver": "transformation", "gold": "aggregation"}
        purpose = purpose_map.get(layer, "utility")

        backend.execute_returning("""
            INSERT INTO databricks_meta.notebooks
            (workspace_id, notebook_path, notebook_name, notebook_language, purpose, layer, last_analyzed_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW()) RETURNING notebook_id
        """, (ws_id, nb.get("path", nb.get("name")), nb["name"], nb.get("language"), purpose, layer))
        result["notebooks"] += 1

        # Import table access
        for ta in nb.get("tables_read", []):
            _insert_table_access(backend, ws_id, nb["name"], ta, "read")
            result["table_access"] += 1
        for ta in nb.get("tables_written", []):
            _insert_table_access(backend, ws_id, nb["name"], ta, "write")
            result["table_access"] += 1

    # Import jobs
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else (jobs_data if isinstance(jobs_data, list) else [])
    for job in jobs:
        rows = backend.execute_returning("""
            INSERT INTO databricks_meta.jobs
            (workspace_id, job_name, schedule, max_concurrent_runs, last_analyzed_at)
            VALUES (%s, %s, %s, %s, NOW()) RETURNING job_id
        """, (ws_id, job.get("name", job.get("job_name")), job.get("schedule"), job.get("max_concurrent_runs", 1)))
        job_id = rows[0]["job_id"]
        result["jobs"] += 1

        # Import tasks
        for task in job.get("tasks", []):
            backend.execute("""
                INSERT INTO databricks_meta.job_tasks
                (job_id, task_key, task_type, notebook_path, depends_on)
                VALUES (%s, %s, %s, %s, %s)
            """, (job_id, task.get("task_key"), task.get("task_type", "notebook_task"),
                  task.get("notebook_path"), json.dumps(task.get("depends_on", []))))
            result["job_tasks"] += 1

    logger.info("Databricks: %d notebooks, %d table_access, %d jobs, %d tasks",
                result["notebooks"], result["table_access"], result["jobs"], result["job_tasks"])
    return result


def _insert_table_access(backend, ws_id, nb_name, table_name, access_type):
    """Insert a notebook_table_access row, looking up notebook_id.

    Splits a dotted reference (catalog.schema.table) into its parts and tags
    it as a Databricks Delta table so downstream lineage classifies it on the
    `databricks` platform (needed for cross-platform table matching).
    """
    nb_rows = backend.query(
        "SELECT notebook_id FROM databricks_meta.notebooks WHERE workspace_id = %s AND notebook_name = %s",
        (ws_id, nb_name)
    )
    if not nb_rows:
        return

    parts = table_name.split(".")
    db_name = parts[0] if len(parts) >= 3 else None
    schema_name = parts[1] if len(parts) >= 3 else (parts[0] if len(parts) == 2 else None)
    tbl = parts[-1]

    backend.execute("""
        INSERT INTO databricks_meta.notebook_table_access
        (notebook_id, table_type, database_name, schema_name, table_name,
         access_type, confidence, detected_method)
        VALUES (%s, 'databricks_delta', %s, %s, %s, %s, 'high', 'static_analysis')
    """, (nb_rows[0]["notebook_id"], db_name, schema_name, tbl, access_type))


# ---------------------------------------------------------------------------
# Power BI importer
# ---------------------------------------------------------------------------

def ingest_powerbi(backend, view_id: int, extractions_dir: Path) -> dict:
    """Import Power BI metadata from JSON files."""
    result = {"reports": 0, "datasets": 0, "tables": 0, "measures": 0, "relationships": 0}

    datasets_data = _load_json(extractions_dir / "powerbi" / "datasets.json")
    if not datasets_data:
        logger.info("No Power BI data found in %s", extractions_dir)
        return result

    datasets = datasets_data.get("datasets", datasets_data) if isinstance(datasets_data, dict) else datasets_data

    # Clean existing data for this view
    existing_reports = backend.query(
        "SELECT report_id FROM powerbi_meta.reports WHERE view_id = %s", (view_id,)
    )
    for r in existing_reports:
        rid = r["report_id"]
        ds_ids = backend.query("SELECT dataset_id FROM powerbi_meta.datasets WHERE report_id = %s", (rid,))
        for d in ds_ids:
            did = d["dataset_id"]
            backend.execute("DELETE FROM powerbi_meta.relationships WHERE dataset_id = %s", (did,))
            backend.execute("DELETE FROM powerbi_meta.columns WHERE table_id IN (SELECT table_id FROM powerbi_meta.tables WHERE dataset_id = %s)", (did,))
            backend.execute("DELETE FROM powerbi_meta.measures WHERE dataset_id = %s", (did,))
            backend.execute("DELETE FROM powerbi_meta.tables WHERE dataset_id = %s", (did,))
        backend.execute("DELETE FROM powerbi_meta.datasets WHERE report_id = %s", (rid,))
    backend.execute("DELETE FROM powerbi_meta.reports WHERE view_id = %s", (view_id,))

    for ds in datasets:
        report_name = ds.get("report_name", ds.get("name", "Unknown Report"))

        # Upsert report
        existing = backend.query(
            "SELECT report_id FROM powerbi_meta.reports WHERE view_id = %s AND report_name = %s",
            (view_id, report_name)
        )
        if existing:
            report_id = existing[0]["report_id"]
        else:
            rows = backend.execute_returning("""
                INSERT INTO powerbi_meta.reports (view_id, report_name, report_type, last_analyzed_at)
                VALUES (%s, %s, 'pbip', NOW()) RETURNING report_id
            """, (view_id, report_name))
            report_id = rows[0]["report_id"]
            result["reports"] += 1

        # Insert dataset
        ds_name = ds.get("name", ds.get("dataset_name", "Unknown"))
        rows = backend.execute_returning("""
            INSERT INTO powerbi_meta.datasets
            (report_id, dataset_name, connection_mode, table_count, measure_count, relationship_count)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING dataset_id
        """, (report_id, ds_name, ds.get("connection_mode", "import"),
              len(ds.get("tables", [])), len(ds.get("measures", [])), len(ds.get("relationships", []))))
        dataset_id = rows[0]["dataset_id"]
        result["datasets"] += 1

        # Import tables
        for tbl in ds.get("tables", []):
            # Source reference: prefer explicit fields, fall back to the bare
            # `source` field. 'calculated' marks a DAX calculated table (no
            # external source). source_table is the cross-platform match key.
            raw_source = tbl.get("source")
            source_table = tbl.get("source_table") or (
                raw_source if raw_source and raw_source != "calculated" else None
            )
            is_calculated = tbl.get("is_calculated", raw_source == "calculated")
            source_type = tbl.get("source_type", "import")
            valid_source_types = ('postgres', 'sql_server', 'databricks', 'fabric', 'odbc', 'excel',
                                  'sharepoint', 'calculated', 'import', 'entity', 'directquery', 'view', 'unknown', 'other')
            if source_type not in valid_source_types:
                source_type = "import"
            rows = backend.execute_returning("""
                INSERT INTO powerbi_meta.tables
                (dataset_id, table_name, source_type, source_database, source_schema, source_table,
                 row_count, column_count, is_calculated_table)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING table_id
            """, (dataset_id, tbl["name"], source_type,
                  tbl.get("source_database"), tbl.get("source_schema"), source_table,
                  tbl.get("row_count"), len(tbl.get("columns", [])),
                  is_calculated))
            table_id = rows[0]["table_id"]
            result["tables"] += 1

            # Import columns
            for i, col in enumerate(tbl.get("columns", [])):
                backend.execute("""
                    INSERT INTO powerbi_meta.columns
                    (table_id, column_name, data_type, is_hidden, is_key, ordinal_position)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (table_id, col.get("name"), col.get("data_type"),
                      col.get("is_hidden", False), col.get("is_key", False), i + 1))

        # Import measures
        for m in ds.get("measures", []):
            backend.execute("""
                INSERT INTO powerbi_meta.measures
                (dataset_id, measure_name, dax_expression, measure_type, table_name, description, format_string)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (dataset_id, m["name"], m.get("expression", m.get("dax_expression", "")),
                  m.get("type", "basic"), m.get("table", m.get("home_table")),
                  m.get("description"), m.get("format_string")))
            result["measures"] += 1

        # Import relationships
        for rel in ds.get("relationships", []):
            # Look up table IDs
            from_tbl = backend.query(
                "SELECT table_id FROM powerbi_meta.tables WHERE dataset_id = %s AND table_name = %s",
                (dataset_id, rel.get("from_table", ""))
            )
            to_tbl = backend.query(
                "SELECT table_id FROM powerbi_meta.tables WHERE dataset_id = %s AND table_name = %s",
                (dataset_id, rel.get("to_table", ""))
            )
            if from_tbl and to_tbl:
                backend.execute("""
                    INSERT INTO powerbi_meta.relationships
                    (dataset_id, from_table_id, from_column, to_table_id, to_column,
                     cardinality, cross_filter_direction, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (dataset_id, from_tbl[0]["table_id"], rel.get("from_column"),
                      to_tbl[0]["table_id"], rel.get("to_column"),
                      rel.get("cardinality", "many-to-one"),
                      rel.get("cross_filter", "single"), rel.get("is_active", True)))
                result["relationships"] += 1

    logger.info("Power BI: %d reports, %d datasets, %d tables, %d measures, %d relationships",
                result["reports"], result["datasets"], result["tables"],
                result["measures"], result["relationships"])
    return result


# ---------------------------------------------------------------------------
# Fabric importer (workspace JSON + SQL schema JSONs)
# ---------------------------------------------------------------------------

def _clear_fabric_workspace(backend, ws_id: int) -> None:
    """Delete a workspace's entire Fabric footprint so a re-scan is idempotent.

    Deletes child -> parent explicitly (not relying on ON DELETE CASCADE, so it
    behaves the same on SQL Server and Postgres). The workspace row itself is
    kept (its id is reused). Mirrors the Databricks re-ingest path. A re-scan
    then reflects the current state — handling added, changed AND removed objects.
    """
    # Pipelines: activity_dependencies first (NO ACTION FK to pipeline_activities),
    # then activities, then pipelines.
    pl_subq = "SELECT pipeline_id FROM fabric_infra.pipelines WHERE workspace_id = %s"
    backend.execute(f"DELETE FROM fabric_infra.activity_dependencies WHERE pipeline_id IN ({pl_subq})", (ws_id,))
    backend.execute(f"DELETE FROM fabric_infra.pipeline_activities WHERE pipeline_id IN ({pl_subq})", (ws_id,))
    backend.execute("DELETE FROM fabric_infra.pipelines WHERE workspace_id = %s", (ws_id,))

    # Notebooks
    backend.execute("DELETE FROM fabric_infra.notebooks WHERE workspace_id = %s", (ws_id,))

    # Warehouse SQL schema (columns -> views/procedures -> tables -> schemas -> warehouses)
    wh_schemas = ("SELECT s.schema_id FROM warehouse_meta.schemas s "
                  "JOIN fabric_infra.warehouses w ON s.warehouse_id = w.warehouse_id "
                  "WHERE w.workspace_id = %s")
    backend.execute(
        "DELETE FROM warehouse_meta.columns WHERE table_id IN ("
        f"SELECT t.table_id FROM warehouse_meta.tables t WHERE t.schema_id IN ({wh_schemas}))", (ws_id,))
    backend.execute(f"DELETE FROM warehouse_meta.views WHERE schema_id IN ({wh_schemas})", (ws_id,))
    backend.execute(f"DELETE FROM warehouse_meta.procedures WHERE schema_id IN ({wh_schemas})", (ws_id,))
    backend.execute(f"DELETE FROM warehouse_meta.tables WHERE schema_id IN ({wh_schemas})", (ws_id,))
    backend.execute("DELETE FROM warehouse_meta.schemas WHERE warehouse_id IN ("
                    "SELECT warehouse_id FROM fabric_infra.warehouses WHERE workspace_id = %s)", (ws_id,))
    backend.execute("DELETE FROM fabric_infra.warehouses WHERE workspace_id = %s", (ws_id,))

    # Lakehouse SQL schema (tables -> schemas -> lakehouses)
    lh_schemas = ("SELECT s.schema_id FROM lakehouse_meta.schemas s "
                  "JOIN fabric_infra.lakehouses l ON s.lakehouse_id = l.lakehouse_id "
                  "WHERE l.workspace_id = %s")
    backend.execute(f"DELETE FROM lakehouse_meta.tables WHERE schema_id IN ({lh_schemas})", (ws_id,))
    backend.execute("DELETE FROM lakehouse_meta.schemas WHERE lakehouse_id IN ("
                    "SELECT lakehouse_id FROM fabric_infra.lakehouses WHERE workspace_id = %s)", (ws_id,))
    backend.execute("DELETE FROM fabric_infra.lakehouses WHERE workspace_id = %s", (ws_id,))


def ingest_fabric(backend, view_id: int, source_dir: Path) -> dict:
    """Import Fabric metadata from workspace + SQL schema JSON files.

    Idempotent per workspace: re-ingesting a workspace clears its existing Fabric
    footprint first (see _clear_fabric_workspace), so re-scans don't duplicate.
    """
    result = {"workspaces": 0, "warehouses": 0, "lakehouses": 0,
              "pipelines": 0, "activities": 0, "notebooks": 0,
              "schemas": 0, "tables": 0, "columns": 0, "views": 0}

    # Find workspace JSON files (not _lakehouse_ or _warehouse_ suffixed)
    ws_files = [f for f in source_dir.glob("*.json")
                if "_lakehouse_" not in f.name and "_warehouse_" not in f.name]

    for ws_file in sorted(ws_files):
        ws_data = _load_json(ws_file)
        if not ws_data or "workspace" not in ws_data:
            continue

        ws_info = ws_data["workspace"]
        ws_name = ws_info.get("displayName") or ws_info.get("name") or ws_file.stem
        logger.info("Fabric workspace: %s", ws_name)

        # Upsert workspace
        existing = backend.query(
            "SELECT workspace_id FROM fabric_infra.workspaces WHERE view_id = %s AND workspace_name = %s",
            (view_id, ws_name))
        if existing:
            ws_id = existing[0]["workspace_id"]
            # Idempotent re-ingest: clear this workspace's footprint before reload.
            _clear_fabric_workspace(backend, ws_id)
            backend.execute(
                "UPDATE fabric_infra.workspaces SET last_extracted_at = NOW() WHERE workspace_id = %s",
                (ws_id,))
        else:
            rows = backend.execute_returning("""
                INSERT INTO fabric_infra.workspaces (view_id, workspace_name, fabric_workspace_id, capacity_region, last_extracted_at)
                VALUES (%s, %s, %s, %s, NOW()) RETURNING workspace_id
            """, (view_id, ws_name, ws_info.get("id"), ws_info.get("capacityRegion")))
            ws_id = rows[0]["workspace_id"]
            result["workspaces"] += 1

        # Warehouses
        for wh in ws_data.get("warehouses", []):
            wh_name = wh.get("name", wh.get("displayName", ""))
            existing_wh = backend.query(
                "SELECT warehouse_id FROM fabric_infra.warehouses WHERE workspace_id = %s AND warehouse_name = %s",
                (ws_id, wh_name))
            if existing_wh:
                wh_id = existing_wh[0]["warehouse_id"]
            else:
                rows = backend.execute_returning("""
                    INSERT INTO fabric_infra.warehouses (workspace_id, warehouse_name, fabric_warehouse_id, sql_endpoint, description, last_extracted_at)
                    VALUES (%s, %s, %s, %s, %s, NOW()) RETURNING warehouse_id
                """, (ws_id, wh_name, wh.get("id"), wh.get("sql_endpoint"), wh.get("description")))
                wh_id = rows[0]["warehouse_id"]
                result["warehouses"] += 1

            # Import SQL schema if file exists
            sql_file = source_dir / f"{ws_file.stem}_warehouse_{wh_name}.json"
            if sql_file.exists():
                _import_sql_schemas(backend, sql_file, wh_id, "warehouse", result)

        # Lakehouses
        for lh in ws_data.get("lakehouses", []):
            lh_name = lh.get("name", lh.get("displayName", ""))
            existing_lh = backend.query(
                "SELECT lakehouse_id FROM fabric_infra.lakehouses WHERE workspace_id = %s AND lakehouse_name = %s",
                (ws_id, lh_name))
            if existing_lh:
                lh_id = existing_lh[0]["lakehouse_id"]
            else:
                rows = backend.execute_returning("""
                    INSERT INTO fabric_infra.lakehouses (workspace_id, lakehouse_name, fabric_lakehouse_id, sql_endpoint, onelake_path, description, last_extracted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW()) RETURNING lakehouse_id
                """, (ws_id, lh_name, lh.get("id"), lh.get("sql_endpoint"), lh.get("onelake_path"), lh.get("description")))
                lh_id = rows[0]["lakehouse_id"]
                result["lakehouses"] += 1

            # Import SQL schema if file exists
            sql_file = source_dir / f"{ws_file.stem}_lakehouse_{lh_name}.json"
            if sql_file.exists():
                _import_sql_schemas(backend, sql_file, lh_id, "lakehouse", result)

        # Pipelines
        for pl in ws_data.get("pipelines", []):
            pl_name = pl.get("name", pl.get("displayName", ""))
            activities = pl.get("activities", [])
            try:
                rows = backend.execute_returning("""
                    INSERT INTO fabric_infra.pipelines (workspace_id, pipeline_name, fabric_pipeline_id, description, activity_count, last_extracted_at)
                    VALUES (%s, %s, %s, %s, %s, NOW()) RETURNING pipeline_id
                """, (ws_id, pl_name, pl.get("id"), pl.get("description"), len(activities)))
                pl_id = rows[0]["pipeline_id"]
                result["pipelines"] += 1

                for i, act in enumerate(activities):
                    try:
                        backend.execute("""
                            INSERT INTO fabric_infra.pipeline_activities
                            (pipeline_id, activity_name, activity_type, category, parent_activity_name, nesting_level,
                             source_type, source_object, sink_type, sink_object, timeout, retry_count, ordinal_position,
                             target_notebook_id, target_pipeline_id, depends_on)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (pl_id, act.get("name"), act.get("type"), act.get("category"),
                              act.get("parent_activity"), act.get("nesting_level", 0),
                              act.get("source_type"), act.get("source_object"),
                              act.get("sink_type"), act.get("sink_object"),
                              act.get("timeout"), act.get("retry_count"), i,
                              act.get("target_notebook_id"), act.get("target_pipeline_id"),
                              json.dumps(act.get("depends_on")) if act.get("depends_on") else None))
                        result["activities"] += 1
                    except Exception:
                        pass
            except Exception:
                pass

        # Notebooks
        for nb in ws_data.get("notebooks", []):
            nb_name = nb.get("name", nb.get("displayName", ""))
            try:
                backend.execute("""
                    INSERT INTO fabric_infra.notebooks (workspace_id, notebook_name, fabric_notebook_id, language, description, last_extracted_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                """, (ws_id, nb_name, nb.get("id"), nb.get("language"), nb.get("description")))
                result["notebooks"] += 1
            except Exception:
                pass

    logger.info("Fabric: %s", result)
    return result


def _import_sql_schemas(backend, sql_file: Path, container_id: int, container_type: str, result: dict):
    """Import SQL schema data (tables, columns, views) from a SQL extraction JSON."""
    data = _load_json(sql_file)
    if not data:
        return

    schema_table = f"{container_type}_meta.schemas"
    table_table = f"{container_type}_meta.tables"
    id_col = f"{container_type}_id"

    # Import schemas
    seen_schemas = {}
    for tbl in data.get("tables", []) + data.get("views", []):
        schema_name = tbl.get("schema", "dbo")
        if schema_name not in seen_schemas:
            try:
                rows = backend.execute_returning(f"""
                    INSERT INTO {schema_table} ({id_col}, schema_name)
                    VALUES (%s, %s) RETURNING schema_id
                """, (container_id, schema_name))
                seen_schemas[schema_name] = rows[0]["schema_id"]
                result["schemas"] += 1
            except Exception:
                # Schema might already exist
                existing = backend.query(f"""
                    SELECT schema_id FROM {schema_table}
                    WHERE {id_col} = %s AND schema_name = %s
                """, (container_id, schema_name))
                if existing:
                    seen_schemas[schema_name] = existing[0]["schema_id"]

    # Import tables
    for tbl in data.get("tables", []):
        schema_name = tbl.get("schema", "dbo")
        schema_id = seen_schemas.get(schema_name)
        if not schema_id:
            continue

        columns = tbl.get("columns", [])
        try:
            if container_type == "warehouse":
                rows = backend.execute_returning(f"""
                    INSERT INTO {table_table} (schema_id, table_name, table_type, column_count, row_count)
                    VALUES (%s, %s, %s, %s, %s) RETURNING table_id
                """, (schema_id, tbl["name"], tbl.get("type", "BASE TABLE"),
                      len(columns), tbl.get("row_count")))
            else:
                rows = backend.execute_returning(f"""
                    INSERT INTO {table_table} (schema_id, table_name, row_count, size_mb, location)
                    VALUES (%s, %s, %s, %s, %s) RETURNING table_id
                """, (schema_id, tbl["name"], tbl.get("row_count"),
                      tbl.get("size_mb"), tbl.get("location")))

            table_id = rows[0]["table_id"]
            result["tables"] += 1

            # Import columns (warehouse only — lakehouse columns in separate schema)
            if container_type == "warehouse":
                for i, col in enumerate(columns):
                    try:
                        backend.execute("""
                            INSERT INTO warehouse_meta.columns
                            (table_id, column_name, data_type, max_length, is_nullable, column_default, ordinal_position)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (table_id, col.get("name"), col.get("data_type"),
                              col.get("max_length"), col.get("is_nullable", True),
                              col.get("default"), i + 1))
                        result["columns"] += 1
                    except Exception:
                        pass
        except Exception:
            pass

    # Import views (warehouse only)
    if container_type == "warehouse":
        for v in data.get("views", []):
            schema_name = v.get("schema", "dbo")
            schema_id = seen_schemas.get(schema_name)
            if schema_id:
                try:
                    backend.execute("""
                        INSERT INTO warehouse_meta.views (schema_id, view_name, definition)
                        VALUES (%s, %s, %s)
                    """, (schema_id, v["name"], v.get("definition")))
                    result["views"] += 1
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Main ingest command
# ---------------------------------------------------------------------------

def ingest(source: str = "demo", view_name: str = None, org_slug: str = None):
    """Run full ingestion from extracted JSON files."""
    config = load_config()
    backend = get_backend(config)

    # Resolve source directory
    if source == "demo":
        source_dir = config.product_path / "demo" / "extractions"
        view_name = view_name or "Demo"
    else:
        source_dir = Path(source)
        view_name = view_name or source_dir.parent.name

    if not source_dir.exists():
        logger.error("Source directory not found: %s", source_dir)
        sys.exit(1)

    logger.info("Ingesting from: %s", source_dir)
    logger.info("Backend: %s", config.db_backend)

    # Resolve org_id from slug
    org_id = None
    if org_slug:
        orgs = backend.query(
            "SELECT org_id FROM auth_meta.organizations WHERE org_slug = %s", (org_slug,)
        )
        if orgs:
            org_id = orgs[0]["org_id"]
            logger.info("Organization: %s (id=%s)", org_slug, org_id)
        else:
            logger.warning("Organization '%s' not found — view will have no org", org_slug)

    # Get or create view (scoped to org if provided)
    if org_id:
        existing = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s AND org_id = %s",
            (view_name, str(org_id))
        )
    else:
        existing = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s", (view_name,)
        )

    if existing:
        view_id = existing[0]["view_id"]
    else:
        rows = backend.execute_returning("""
            INSERT INTO catalog_meta.catalog_views (view_name, description, view_type, is_default, org_id)
            VALUES (%s, %s, 'scan', FALSE, %s) RETURNING view_id
        """, (view_name, f"Imported from {source}", str(org_id) if org_id else None))
        view_id = rows[0]["view_id"]

    logger.info("View: %s (id=%s)", view_name, view_id)

    # For the demo, make its view the default so the dashboard lands on data
    # (otherwise first load shows the empty 'Default' view).
    if source == "demo":
        backend.execute("UPDATE catalog_meta.catalog_views SET is_default = FALSE WHERE is_default = TRUE")
        backend.execute("UPDATE catalog_meta.catalog_views SET is_default = TRUE WHERE view_id = %s", (view_id,))

    # Run platform importers
    results = {}

    if (source_dir / "databricks").exists():
        results["databricks"] = ingest_databricks(backend, view_id, source_dir)

    if (source_dir / "powerbi").exists():
        results["powerbi"] = ingest_powerbi(backend, view_id, source_dir)

    # Fabric: check for workspace JSON files in the source dir directly
    fabric_files = [f for f in source_dir.glob("*.json")
                    if "_lakehouse_" not in f.name and "_warehouse_" not in f.name]
    has_fabric = any("workspace" in json.loads(f.read_text()).keys()
                     for f in fabric_files[:1]) if fabric_files else False
    if has_fabric:
        results["fabric"] = ingest_fabric(backend, view_id, source_dir)

    # Summary
    logger.info("=== Ingestion complete ===")
    for platform, counts in results.items():
        logger.info("  %s: %s", platform, counts)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import extracted metadata into ade-catalog")
    parser.add_argument("--source", default="demo", help="Source: 'demo' or path to extractions dir")
    parser.add_argument("--view", default=None, help="View name to import into")
    parser.add_argument("--org", default=None, help="Organization slug (e.g. 'acme', 'ade')")
    args = parser.parse_args()

    ingest(source=args.source, view_name=args.view, org_slug=args.org)
