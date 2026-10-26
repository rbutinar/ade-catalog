"""
Databricks lineage extractor (view-scoped).

Reads from the v2 `databricks_meta` schema and populates lineage_meta via
LineageGraphBuilder. Everything is scoped to a single catalog view (view_id)
for multi-tenant isolation: every node carries view_id and every
qualified_name is namespaced with the view, so identical workspace/table names
in different tenants never collide on the global UNIQUE(qualified_name).

Graph produced (per view):
    workspace --contains--> job --contains--> task --executes--> notebook
    task --depends_on_task--> task            (intra-job ordering)
    notebook --calls/triggers--> notebook     (notebook_dependencies)
    table --feeds--> notebook                 (read access)
    notebook --produces--> table              (write/create/merge access)

This is the v2 re-implementation of the Databricks portions of v1's
build_lineage_graph.py, rewritten against the v2 schema and the backend
abstraction. See ADR docs/decisions/2026-06-28-lineage-engine-port.md.
"""

import json
import logging
from typing import Optional

from ade_catalog.lineage.builder import LineageGraphBuilder

logger = logging.getLogger(__name__)

# access_type -> (edge direction, relationship_type)
# 'read'  : table feeds the notebook  (table -> notebook)
# others  : notebook produces the table (notebook -> table)
_WRITE_ACCESS = {"write", "create", "merge", "truncate"}


class DatabricksLineageExtractor:
    """Populate Databricks lineage for one catalog view."""

    def __init__(self, builder: LineageGraphBuilder, view_id: int):
        self.b = builder
        self.view_id = view_id
        self._prefix = f"v{view_id}.databricks"

    # ------------------------------------------------------------------
    # qualified-name helpers (view-namespaced)
    # ------------------------------------------------------------------

    def _qn(self, *parts: str) -> str:
        return ".".join([self._prefix, *[str(p) for p in parts]])

    def _node(self, **kwargs) -> int:
        """create_node with this view's view_id pinned."""
        return self.b.create_node(view_id=self.view_id, **kwargs)

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------

    def build(self) -> dict:
        """Run all Databricks extraction steps for this view."""
        logger.info("Building Databricks lineage for view_id=%s", self.view_id)
        self.extract_workspaces()
        self.extract_notebooks()
        self.extract_jobs_and_tasks()
        self.extract_notebook_dependencies()
        self.extract_notebook_table_access()
        return self.b.stats()

    # ------------------------------------------------------------------
    # workspaces
    # ------------------------------------------------------------------

    def extract_workspaces(self) -> None:
        rows = self.b.backend.query(
            """
            SELECT workspace_id, workspace_name, workspace_url
            FROM databricks_meta.workspaces
            WHERE view_id = %s
            """,
            (self.view_id,),
        )
        for r in rows:
            self._node(
                node_type="workspace",
                display_name=r["workspace_name"],
                qualified_name=self._qn(r["workspace_name"]),
                platform="databricks",
                external_id=str(r["workspace_id"]),
                properties={"url": r.get("workspace_url")},
            )
        logger.info("  workspaces: %d", len(rows))

    # ------------------------------------------------------------------
    # notebooks
    # ------------------------------------------------------------------

    def extract_notebooks(self) -> None:
        rows = self.b.backend.query(
            """
            SELECT n.notebook_id, n.notebook_name, n.notebook_path,
                   n.purpose, n.layer, w.workspace_name
            FROM databricks_meta.notebooks n
            JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
            WHERE w.view_id = %s
            """,
            (self.view_id,),
        )
        for r in rows:
            ws = r["workspace_name"]
            ws_node = self.b.get_node_id(self._qn(ws))
            self._node(
                node_type="notebook",
                display_name=r["notebook_name"],
                qualified_name=self._qn(ws, "notebook", r["notebook_path"]),
                platform="databricks",
                external_id=str(r["notebook_id"]),
                parent_node_id=ws_node,
                hierarchy_path=f"/{ws}/notebooks{r['notebook_path']}",
                properties={
                    "path": r["notebook_path"],
                    "purpose": r.get("purpose"),
                    "layer": r.get("layer"),
                },
            )
        logger.info("  notebooks: %d", len(rows))

    # ------------------------------------------------------------------
    # jobs + tasks
    # ------------------------------------------------------------------

    def extract_jobs_and_tasks(self) -> None:
        rows = self.b.backend.query(
            """
            SELECT j.job_id, j.job_name, j.databricks_job_id,
                   w.workspace_name,
                   jt.task_key, jt.task_type, jt.depends_on,
                   n.notebook_path
            FROM databricks_meta.jobs j
            JOIN databricks_meta.workspaces w ON j.workspace_id = w.workspace_id
            LEFT JOIN databricks_meta.job_tasks jt ON jt.job_id = j.job_id
            LEFT JOIN databricks_meta.notebooks n ON jt.notebook_id = n.notebook_id
            WHERE w.view_id = %s
            ORDER BY j.job_id, jt.task_key
            """,
            (self.view_id,),
        )

        jobs_seen: set[str] = set()
        n_jobs = n_tasks = n_exec = n_dep = 0

        for r in rows:
            ws = r["workspace_name"]
            job_name = r["job_name"]
            ws_node = self.b.get_node_id(self._qn(ws))

            job_qn = self._qn(ws, "job", job_name)
            job_node = self._node(
                node_type="job",
                display_name=job_name,
                qualified_name=job_qn,
                platform="databricks",
                external_id=str(r["databricks_job_id"]),
                parent_node_id=ws_node,
                hierarchy_path=f"/{ws}/jobs/{job_name}",
            )
            if job_qn not in jobs_seen:
                jobs_seen.add(job_qn)
                if ws_node:
                    self.b.create_edge(job_node, ws_node, "belongs_to")
                n_jobs += 1

            task_key = r.get("task_key")
            if not task_key:
                continue

            task_qn = self._qn(ws, "job", job_name, "task", task_key)
            task_node = self._node(
                node_type="task",
                display_name=f"{job_name}/{task_key}",
                qualified_name=task_qn,
                platform="databricks",
                parent_node_id=job_node,
                hierarchy_path=f"/{ws}/jobs/{job_name}/tasks/{task_key}",
                properties={"task_type": r.get("task_type")},
            )
            self.b.create_edge(job_node, task_node, "contains")
            n_tasks += 1

            # task executes notebook (v2 links notebook_id directly on the task)
            nb_path = r.get("notebook_path")
            if nb_path:
                nb_node = self.b.get_node_id(self._qn(ws, "notebook", nb_path))
                if nb_node:
                    self.b.create_edge(task_node, nb_node, "executes")
                    n_exec += 1

            # intra-job task ordering from depends_on (JSON array of task_keys)
            for dep_key in _parse_json_list(r.get("depends_on")):
                dep_qn = self._qn(ws, "job", job_name, "task", dep_key)
                dep_node = self.b.get_node_id(dep_qn)
                if dep_node:
                    # dependency runs before this task
                    self.b.create_edge(dep_node, task_node, "depends_on_task")
                    n_dep += 1

        logger.info(
            "  jobs: %d, tasks: %d, executes: %d, task-deps: %d",
            n_jobs, n_tasks, n_exec, n_dep,
        )

    # ------------------------------------------------------------------
    # notebook -> notebook dependencies
    # ------------------------------------------------------------------

    def extract_notebook_dependencies(self) -> None:
        rows = self.b.backend.query(
            """
            SELECT nd.dependency_type,
                   fn.notebook_path AS from_path,
                   tn.notebook_path AS to_path,
                   w.workspace_name
            FROM databricks_meta.notebook_dependencies nd
            JOIN databricks_meta.notebooks fn ON nd.from_notebook_id = fn.notebook_id
            JOIN databricks_meta.notebooks tn ON nd.to_notebook_id = tn.notebook_id
            JOIN databricks_meta.workspaces w ON fn.workspace_id = w.workspace_id
            WHERE w.view_id = %s
            """,
            (self.view_id,),
        )
        n = 0
        for r in rows:
            ws = r["workspace_name"]
            from_node = self.b.get_node_id(self._qn(ws, "notebook", r["from_path"]))
            to_node = self.b.get_node_id(self._qn(ws, "notebook", r["to_path"]))
            if not (from_node and to_node):
                continue
            # 'run' = orchestration call; import/reference = composition 'calls'
            rel = "triggers" if r["dependency_type"] == "run" else "calls"
            if self.b.create_edge(from_node, to_node, rel,
                                  extraction_method="code_parsing"):
                n += 1
        logger.info("  notebook dependencies: %d", n)

    # ------------------------------------------------------------------
    # notebook <-> table access (the core data lineage)
    # ------------------------------------------------------------------

    def extract_notebook_table_access(self) -> None:
        rows = self.b.backend.query(
            """
            SELECT n.notebook_path, w.workspace_name,
                   nta.table_type, nta.database_name, nta.schema_name,
                   nta.table_name, nta.full_table_name,
                   nta.access_type, nta.confidence
            FROM databricks_meta.notebook_table_access nta
            JOIN databricks_meta.notebooks n ON nta.notebook_id = n.notebook_id
            JOIN databricks_meta.workspaces w ON n.workspace_id = w.workspace_id
            WHERE w.view_id = %s
            """,
            (self.view_id,),
        )
        n_read = n_write = 0
        for r in rows:
            ws = r["workspace_name"]
            nb_node = self.b.get_node_id(self._qn(ws, "notebook", r["notebook_path"]))
            if not nb_node:
                continue

            node_type, platform = _table_node_kind(r["table_type"])
            full_name = r.get("full_table_name") or r["table_name"]
            table_qn = self._qn(platform, "table", full_name)
            table_node = self._node(
                node_type=node_type,
                display_name=full_name,
                qualified_name=table_qn,
                platform=platform,
                properties={
                    "database": r.get("database_name"),
                    "schema": r.get("schema_name"),
                    "table": r["table_name"],
                    "table_type": r["table_type"],
                },
            )

            access = r["access_type"]
            conf = r.get("confidence") or "high"
            if access == "read":
                if self.b.create_edge(table_node, nb_node, "feeds",
                                      confidence=conf,
                                      extraction_method="code_parsing"):
                    n_read += 1
            elif access in _WRITE_ACCESS:
                if self.b.create_edge(nb_node, table_node, "produces",
                                      confidence=conf,
                                      extraction_method="code_parsing"):
                    n_write += 1
            # 'drop' and unknown access types are ignored
        logger.info("  table access edges: read=%d, write=%d", n_read, n_write)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _parse_json_list(raw) -> list:
    """Parse a JSON array column that may be NULL, a string, or already a list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _table_node_kind(table_type: Optional[str]) -> tuple[str, str]:
    """Map databricks_meta.notebook_table_access.table_type to (node_type, platform)."""
    if table_type == "databricks_delta":
        return "delta_table", "databricks"
    if table_type == "databricks_hive":
        return "unity_table", "databricks"
    if table_type == "postgres":
        return "table", "postgresql"
    # 'external' or anything else
    return "table", "external"
