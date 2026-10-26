"""
Databricks JSON importer.

Reads extracted JSON files (jobs, notebooks, tables) and imports
metadata into the SQL Server databricks_meta schema.

Uses a full-refresh strategy per workspace: deletes existing records
for the workspace, then re-inserts. This naturally handles deletions
and avoids complex merge logic.

Usage:
    from ade_catalog.core.config import load_config
    from ade_catalog.platforms.databricks.importers.json_importer import import_databricks

    config = load_config()
    result = import_databricks(config)

CLI:
    python -m ade_catalog.platforms.databricks.importers.json_importer
    python -m ade_catalog.platforms.databricks.importers.json_importer --instance C:/path/to/instance
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ade_catalog.core.config import CatalogConfig
from ade_catalog.core.database import get_connection
from ade_catalog.core.views import get_default_view_id

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary of an import run."""
    workspace: str = ""
    notebooks_imported: int = 0
    table_access_imported: int = 0
    jobs_imported: int = 0
    job_tasks_imported: int = 0
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
    platform_cfg = config.platforms.get("databricks")
    if platform_cfg and key in platform_cfg.settings.get("local", {}):
        relative = platform_cfg.settings["local"][key]
        return config.data_root / relative
    return Path()


def _upsert_workspace(cursor, view_id: int, workspace_url: str) -> int:
    """Ensure workspace exists within view, return workspace_id."""
    cursor.execute(
        "SELECT workspace_id FROM databricks_meta.workspaces WHERE view_id = ? AND workspace_url = ?",
        (view_id, workspace_url)
    )
    row = cursor.fetchone()
    if row:
        workspace_id = row[0]
        cursor.execute(
            "UPDATE databricks_meta.workspaces SET last_analyzed_at = GETDATE() WHERE workspace_id = ?",
            (workspace_id,)
        )
        return workspace_id

    cursor.execute(
        """INSERT INTO databricks_meta.workspaces
           (view_id, workspace_name, workspace_url, workspace_type, last_analyzed_at)
           OUTPUT INSERTED.workspace_id
           VALUES (?, ?, ?, 'production', GETDATE())""",
        (view_id, workspace_url, workspace_url)
    )
    return int(cursor.fetchone()[0])


def _clear_workspace_data(cursor, workspace_id: int):
    """Delete all existing data for a workspace in correct FK order."""
    # Delete in dependency order to respect NO ACTION foreign keys
    cursor.execute("""
        DELETE jt FROM databricks_meta.job_tasks jt
        INNER JOIN databricks_meta.jobs j ON jt.job_id = j.job_id
        WHERE j.workspace_id = ?
    """, (workspace_id,))
    cursor.execute("""
        DELETE nd FROM databricks_meta.notebook_dependencies nd
        INNER JOIN databricks_meta.notebooks n ON nd.from_notebook_id = n.notebook_id
        WHERE n.workspace_id = ?
    """, (workspace_id,))
    # Clear parent_job_id references before deleting jobs
    cursor.execute(
        "UPDATE databricks_meta.notebooks SET parent_job_id = NULL WHERE workspace_id = ?",
        (workspace_id,)
    )
    cursor.execute(
        "DELETE FROM databricks_meta.jobs WHERE workspace_id = ?",
        (workspace_id,)
    )
    # notebook_table_access cascades from notebooks
    cursor.execute(
        "DELETE FROM databricks_meta.notebooks WHERE workspace_id = ?",
        (workspace_id,)
    )


def _import_notebooks(cursor, workspace_id: int, notebooks: list[dict]) -> int:
    """Import notebook records. Returns count imported."""
    count = 0
    for nb in notebooks:
        layer = nb.get("layer", "none")
        # Map JSON purpose from layer
        purpose_map = {
            "bronze": "ingestion",
            "silver": "transformation",
            "gold": "aggregation",
            "setup": "setup",
        }
        purpose = purpose_map.get(layer, "utility")

        cursor.execute(
            """INSERT INTO databricks_meta.notebooks
               (workspace_id, notebook_path, notebook_name, notebook_language,
                purpose, layer, created_at, last_modified_at, last_analyzed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, GETDATE())""",
            (
                workspace_id,
                nb["path"],
                nb["name"],
                nb.get("language"),
                purpose,
                layer if layer in ("bronze", "silver", "gold", "utility", "none") else "utility",
                nb.get("created_time"),
                nb.get("modified_time"),
            )
        )
        count += 1
    return count


def _get_notebook_id_by_path(cursor, workspace_id: int, path: str) -> Optional[int]:
    """Look up notebook_id by path."""
    cursor.execute(
        "SELECT notebook_id FROM databricks_meta.notebooks WHERE workspace_id = ? AND notebook_path = ?",
        (workspace_id, path)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _get_notebook_id_by_name(cursor, workspace_id: int, name: str) -> Optional[int]:
    """Look up notebook_id by name."""
    cursor.execute(
        "SELECT notebook_id FROM databricks_meta.notebooks WHERE workspace_id = ? AND notebook_name = ?",
        (workspace_id, name)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _import_table_access(cursor, workspace_id: int, notebooks: list[dict]) -> int:
    """Import notebook_table_access from notebooks' tables_read/tables_written."""
    count = 0
    for nb in notebooks:
        notebook_id = _get_notebook_id_by_name(cursor, workspace_id, nb["name"])
        if not notebook_id:
            continue

        # Tables read
        for table_ref in nb.get("tables_read", []):
            parts = table_ref.split(".")
            db_name = parts[0] if len(parts) >= 3 else None
            schema_name = parts[1] if len(parts) >= 3 else (parts[0] if len(parts) == 2 else None)
            table_name = parts[-1]

            cursor.execute(
                """INSERT INTO databricks_meta.notebook_table_access
                   (notebook_id, table_type, database_name, schema_name, table_name,
                    access_type, confidence, detected_method)
                   VALUES (?, 'databricks_delta', ?, ?, ?, 'read', 'high', 'extraction_metadata')""",
                (notebook_id, db_name, schema_name, table_name)
            )
            count += 1

        # Tables written
        for table_ref in nb.get("tables_written", []):
            parts = table_ref.split(".")
            db_name = parts[0] if len(parts) >= 3 else None
            schema_name = parts[1] if len(parts) >= 3 else (parts[0] if len(parts) == 2 else None)
            table_name = parts[-1]

            cursor.execute(
                """INSERT INTO databricks_meta.notebook_table_access
                   (notebook_id, table_type, database_name, schema_name, table_name,
                    access_type, confidence, detected_method)
                   VALUES (?, 'databricks_delta', ?, ?, ?, 'write', 'high', 'extraction_metadata')""",
                (notebook_id, db_name, schema_name, table_name)
            )
            count += 1

    return count


def _import_jobs(cursor, workspace_id: int, pipelines: list[dict]) -> tuple[int, int]:
    """Import jobs and job_tasks from conceptual pipelines. Returns (jobs_count, tasks_count)."""
    jobs_count = 0
    tasks_count = 0

    for pipeline in pipelines:
        cursor.execute(
            """INSERT INTO databricks_meta.jobs
               (workspace_id, databricks_job_id, job_name, job_type,
                is_scheduled, is_active, created_at)
               OUTPUT INSERTED.job_id
               VALUES (?, ?, ?, 'notebook', 0, 1, ?)""",
            (
                workspace_id,
                pipeline["pipeline_id"],
                pipeline["name"],
                pipeline.get("created_time"),
            )
        )
        job_id = int(cursor.fetchone()[0])
        jobs_count += 1

        # Import tasks
        for task in pipeline.get("tasks", []):
            # Try to link task to notebook
            notebook_id = _get_notebook_id_by_path(
                cursor, workspace_id, task.get("notebook_path", "")
            )

            depends_on = json.dumps(task.get("depends_on", []))

            cursor.execute(
                """INSERT INTO databricks_meta.job_tasks
                   (job_id, notebook_id, task_key, task_type, depends_on)
                   VALUES (?, ?, ?, 'notebook', ?)""",
                (job_id, notebook_id, task["task_key"], depends_on)
            )
            tasks_count += 1

            # Link notebook to parent job
            if notebook_id:
                cursor.execute(
                    "UPDATE databricks_meta.notebooks SET parent_job_id = ? WHERE notebook_id = ?",
                    (job_id, notebook_id)
                )

    return jobs_count, tasks_count


def import_databricks(config: CatalogConfig, view_id: Optional[int] = None) -> ImportResult:
    """
    Import Databricks metadata from JSON extraction files into the database.

    Reads jobs.json, notebooks.json, and tables.json from the configured
    extraction paths and imports into the databricks_meta schema.

    Args:
        config: Catalog configuration.
        view_id: Target view. Uses default view if not specified.
    """
    result = ImportResult()

    if view_id is None:
        view_id = get_default_view_id(config)

    # Load JSON files
    jobs_path = _resolve_file(config, "jobs_file")
    notebooks_path = _resolve_file(config, "notebooks_file")
    tables_path = _resolve_file(config, "tables_file")

    jobs_data = _load_json(jobs_path)
    notebooks_data = _load_json(notebooks_path)
    tables_data = _load_json(tables_path)

    if not notebooks_data and not jobs_data:
        result.errors.append("No data files found or all empty")
        return result

    # Get workspace URL from any available file
    workspace_url = (
        notebooks_data.get("workspace")
        or jobs_data.get("workspace")
        or tables_data.get("workspace")
        or "unknown"
    )
    result.workspace = workspace_url

    logger.info("Importing Databricks metadata for workspace: %s (view_id=%d)", workspace_url, view_id)

    conn = get_connection(config.database)
    try:
        cursor = conn.cursor()

        # Upsert workspace within view
        workspace_id = _upsert_workspace(cursor, view_id, workspace_url)
        logger.info("Workspace ID: %d", workspace_id)

        # Clear existing data for this workspace
        _clear_workspace_data(cursor, workspace_id)

        # Import notebooks
        notebooks = notebooks_data.get("notebooks", [])
        if notebooks:
            result.notebooks_imported = _import_notebooks(cursor, workspace_id, notebooks)
            logger.info("Imported %d notebooks", result.notebooks_imported)

        # Import table access from notebook metadata
        if notebooks:
            result.table_access_imported = _import_table_access(cursor, workspace_id, notebooks)
            logger.info("Imported %d table access records", result.table_access_imported)

        # Import jobs/pipelines
        pipelines = jobs_data.get("conceptual_pipelines", [])
        if pipelines:
            jobs_count, tasks_count = _import_jobs(cursor, workspace_id, pipelines)
            result.jobs_imported = jobs_count
            result.job_tasks_imported = tasks_count
            logger.info("Imported %d jobs with %d tasks", jobs_count, tasks_count)

        conn.commit()
        logger.info("Databricks import completed successfully")

    except Exception as e:
        conn.rollback()
        result.errors.append(str(e))
        logger.error("Databricks import failed: %s", e)
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

    parser = argparse.ArgumentParser(description="Import Databricks metadata from JSON files")
    parser.add_argument("--instance", help="Path to instance directory")
    args = parser.parse_args()

    from ade_catalog.core.config import load_config
    config = load_config(instance_path=args.instance)

    result = import_databricks(config)

    if result.success:
        print(f"OK: {result.notebooks_imported} notebooks, "
              f"{result.table_access_imported} table access, "
              f"{result.jobs_imported} jobs, "
              f"{result.job_tasks_imported} tasks")
    else:
        print(f"FAILED: {result.errors}", file=sys.stderr)
        sys.exit(1)
