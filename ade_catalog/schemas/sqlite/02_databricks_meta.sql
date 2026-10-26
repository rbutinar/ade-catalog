-- ============================================================================
-- GENERATED from 02_databricks_meta.sql by _translate_from_supabase.py — DO NOT EDIT BY HAND.
-- SQLite dialect for the public self-host edition (tables + indexes only;
-- helper views/functions dropped, FKs stripped, ATTACH-per-schema resolves
-- the schema qualifier). Regenerate instead of editing.
-- ============================================================================

-- ============================================================================
-- ade-catalog - Databricks Meta Schema (Supabase / PostgreSQL)
-- ============================================================================
-- Purpose: Track Databricks workspaces, jobs, notebooks, and their relationships
-- Schema: databricks_meta
-- ============================================================================
-- Workspaces: Databricks workspace definitions
CREATE TABLE IF NOT EXISTS databricks_meta.workspaces (
    workspace_id INTEGER PRIMARY KEY,
    view_id INT NOT NULL ,
    workspace_name VARCHAR(200) NOT NULL,
    workspace_url TEXT NOT NULL,
    workspace_region VARCHAR(100),
    workspace_type VARCHAR(50) CHECK(workspace_type IN ('production', 'development', 'poc', 'sandbox')),

    -- Authentication
    connected_user VARCHAR(200),

    -- Metadata
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_analyzed_at TEXT
);

-- Jobs: Databricks job definitions
CREATE TABLE IF NOT EXISTS databricks_meta.jobs (
    job_id INTEGER PRIMARY KEY,
    workspace_id INT NOT NULL ,
    databricks_job_id VARCHAR(100) NOT NULL,

    job_name VARCHAR(500) NOT NULL,
    job_type VARCHAR(50) CHECK(job_type IN ('notebook', 'jar', 'python', 'spark_submit', 'pipeline')),

    -- Workflow linkage
    workflow_id INT,

    -- Schedule
    schedule_cron VARCHAR(200),
    schedule_timezone VARCHAR(100),
    is_scheduled INTEGER DEFAULT FALSE,

    -- Configuration
    timeout_seconds INT,
    max_concurrent_runs INT,
    max_retries INT,

    -- Status
    is_active INTEGER DEFAULT TRUE,

    -- URLs
    job_url TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_run_at TEXT,

    UNIQUE(workspace_id, databricks_job_id)
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_workspaces_view ON workspaces(view_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_jobs_workspace ON jobs(workspace_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_jobs_workflow ON jobs(workflow_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_jobs_name ON jobs(job_name);

-- Notebooks: Databricks notebook metadata
CREATE TABLE IF NOT EXISTS databricks_meta.notebooks (
    notebook_id INTEGER PRIMARY KEY,
    workspace_id INT NOT NULL ,

    notebook_path TEXT NOT NULL,
    notebook_name VARCHAR(500) NOT NULL,
    notebook_language VARCHAR(50) CHECK(notebook_language IN ('python', 'scala', 'sql', 'r')),

    -- Workflow linkage
    workflow_id INT,
    component_id INT,

    -- Purpose classification
    purpose VARCHAR(100) CHECK(purpose IN ('ingestion', 'transformation', 'aggregation', 'export', 'validation', 'setup', 'utility', 'analysis')),
    layer VARCHAR(50) CHECK(layer IN ('bronze', 'silver', 'gold', 'utility', 'none')),

    -- Code metadata
    line_count INT,
    cell_count INT,
    has_widgets INTEGER DEFAULT FALSE,
    has_sql INTEGER DEFAULT FALSE,

    -- Relationships (NO ACTION to avoid multiple cascade paths through workspace)
    parent_job_id INT ,

    -- URLs
    notebook_url TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_modified_at TEXT,
    last_analyzed_at TEXT
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebooks_workspace ON notebooks(workspace_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebooks_workflow ON notebooks(workflow_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebooks_parent_job ON notebooks(parent_job_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebooks_purpose ON notebooks(purpose);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebooks_layer ON notebooks(layer);

-- Job Tasks: Tasks within Databricks jobs
CREATE TABLE IF NOT EXISTS databricks_meta.job_tasks (
    task_id INTEGER PRIMARY KEY,
    job_id INT NOT NULL ,
    notebook_id INT ,

    task_key VARCHAR(200) NOT NULL,
    task_type VARCHAR(50),
    depends_on TEXT,  -- TEXT array of task_key dependencies

    timeout_seconds INT,
    max_retries INT,

    UNIQUE(job_id, task_key)
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_job_tasks_job ON job_tasks(job_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_job_tasks_notebook ON job_tasks(notebook_id);

-- Notebook Dependencies: Cross-notebook dependencies
CREATE TABLE IF NOT EXISTS databricks_meta.notebook_dependencies (
    dependency_id INTEGER PRIMARY KEY,
    from_notebook_id INT NOT NULL ,
    to_notebook_id INT NOT NULL ,
    dependency_type VARCHAR(50) CHECK(dependency_type IN ('run', 'import', 'reference', 'data_dependency')),

    CHECK(from_notebook_id != to_notebook_id),
    UNIQUE(from_notebook_id, to_notebook_id, dependency_type)
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_deps_from ON notebook_dependencies(from_notebook_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_deps_to ON notebook_dependencies(to_notebook_id);

-- Notebook Table Access: Tables read/written by notebooks
CREATE TABLE IF NOT EXISTS databricks_meta.notebook_table_access (
    access_id INTEGER PRIMARY KEY,
    notebook_id INT NOT NULL ,

    -- Table reference (can be Databricks or PostgreSQL)
    table_type VARCHAR(50) CHECK(table_type IN ('databricks_delta', 'databricks_hive', 'postgres', 'external')),
    database_name VARCHAR(200),
    schema_name VARCHAR(200),
    table_name VARCHAR(200) NOT NULL,

    -- Generated column replacement: use a stored generated column
    full_table_name TEXT GENERATED ALWAYS AS (
        CASE
            WHEN schema_name IS NOT NULL THEN database_name || '.' || schema_name || '.' || table_name
            WHEN database_name IS NOT NULL THEN database_name || '.' || table_name
            ELSE table_name
        END
    ) STORED,

    -- Access pattern
    access_type VARCHAR(50) NOT NULL CHECK(access_type IN ('read', 'write', 'create', 'drop', 'truncate', 'merge')),

    -- Analysis metadata
    confidence VARCHAR(50) CHECK(confidence IN ('high', 'medium', 'low')),
    detected_method VARCHAR(100),

    UNIQUE(notebook_id, table_type, database_name, schema_name, table_name, access_type)
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_table_notebook ON notebook_table_access(notebook_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_table_type ON notebook_table_access(table_type);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_table_name ON notebook_table_access(table_name);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_notebook_table_access_type ON notebook_table_access(access_type);

-- Clusters: Databricks cluster configurations
CREATE TABLE IF NOT EXISTS databricks_meta.clusters (
    cluster_id INTEGER PRIMARY KEY,
    workspace_id INT NOT NULL ,
    databricks_cluster_id VARCHAR(100),

    cluster_name VARCHAR(200),
    cluster_type VARCHAR(50) CHECK(cluster_type IN ('interactive', 'job', 'all_purpose')),

    spark_version VARCHAR(100),
    node_type_id VARCHAR(100),
    num_workers INT,
    autoscale_min_workers INT,
    autoscale_max_workers INT,

    is_active INTEGER DEFAULT TRUE,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,

    UNIQUE(workspace_id, databricks_cluster_id)
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_clusters_workspace ON clusters(workspace_id);

-- Job Runs: Historical job execution tracking
CREATE TABLE IF NOT EXISTS databricks_meta.job_runs (
    run_id INTEGER PRIMARY KEY,
    job_id INT NOT NULL ,
    databricks_run_id VARCHAR(100) UNIQUE,

    status VARCHAR(50) CHECK(status IN ('running', 'succeeded', 'failed', 'cancelled', 'skipped')),

    started_at TEXT,
    completed_at TEXT,
    duration_seconds INT,

    trigger_type VARCHAR(50) CHECK(trigger_type IN ('manual', 'scheduled', 'api', 'retry')),

    error_message TEXT,
    run_url TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS databricks_meta.idx_job_runs_job ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_job_runs_status ON job_runs(status);
CREATE INDEX IF NOT EXISTS databricks_meta.idx_job_runs_started ON job_runs(started_at DESC);

-- View: Table Access Summary
