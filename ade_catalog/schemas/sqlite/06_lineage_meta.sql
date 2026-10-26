-- ============================================================================
-- ade-catalog - Lineage Meta Schema (SQLite / public self-host edition)
-- ============================================================================
-- Generalized property graph: entity_types (node classes) + relationship_types
-- (predicates) + nodes + edges. ATTACHed AS lineage_meta by the SqliteBackend.
--
-- Public-edition simplifications vs the Supabase/SQL Server schema:
--   * SERIAL -> INTEGER PRIMARY KEY (SQLite rowid autoincrement).
--   * TIMESTAMPTZ/TIMESTAMP -> TEXT; NOW() -> CURRENT_TIMESTAMP.
--   * BOOLEAN -> INTEGER; DECIMAL(p,s) -> REAL; VARCHAR(n) -> TEXT.
--   * No plpgsql updated_at triggers / stored functions / helper VIEWs: the read
--     path (MCP get_lineage, get_graph_stats) traverses nodes/edges with a
--     WITH RECURSIVE CTE — no stored procedures needed. Efficient large-graph
--     handling stays out of the public edition (honest boundary).
--   * view_id is folded into nodes here (it is a migration on the other backends).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ENTITY TYPES (node classes)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lineage_meta.entity_types (
    type_id       INTEGER PRIMARY KEY,
    type_name     TEXT NOT NULL UNIQUE,
    type_category TEXT NOT NULL,      -- data, process, consumption, organizational
    platform      TEXT,              -- NULL = cross-platform
    icon          TEXT,
    color         TEXT,
    description   TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS lineage_meta.idx_entity_types_category ON entity_types(type_category);

-- DATA ASSETS
INSERT OR IGNORE INTO lineage_meta.entity_types (type_name, type_category, platform, icon, color, description) VALUES
('file', 'data', NULL, 'file', '#6B7280', 'Generic file (Excel, CSV, Parquet, JSON)'),
('excel_file', 'data', NULL, 'file-spreadsheet', '#217346', 'Microsoft Excel file'),
('csv_file', 'data', NULL, 'file-text', '#6B7280', 'CSV/text delimited file'),
('parquet_file', 'data', NULL, 'file-binary', '#4A90D9', 'Apache Parquet file'),
('json_file', 'data', NULL, 'file-code', '#F7DF1E', 'JSON data file'),
('database', 'data', NULL, 'database', '#336791', 'Database instance'),
('schema', 'data', NULL, 'folder-database', '#336791', 'Database schema'),
('table', 'data', NULL, 'table', '#336791', 'Physical database table'),
('view', 'data', NULL, 'table-view', '#5A9BD5', 'Database view'),
('materialized_view', 'data', NULL, 'table-cached', '#2E7D32', 'Materialized view'),
('column', 'data', NULL, 'column', '#9E9E9E', 'Table/view column'),
('stored_procedure', 'data', NULL, 'code', '#FF6B35', 'Stored procedure'),
('function', 'data', NULL, 'function', '#FF6B35', 'Database function'),
('unity_catalog', 'data', 'databricks', 'catalog', '#FF3621', 'Unity Catalog'),
('unity_schema', 'data', 'databricks', 'folder', '#FF3621', 'Unity Catalog schema'),
('unity_table', 'data', 'databricks', 'table', '#FF3621', 'Unity Catalog table'),
('unity_volume', 'data', 'databricks', 'folder-files', '#FF3621', 'Unity Catalog volume'),
('delta_table', 'data', 'databricks', 'table-delta', '#00ADD8', 'Delta Lake table'),
('streaming_table', 'data', 'databricks', 'table-stream', '#00ADD8', 'Streaming table');

-- PROCESS ASSETS
INSERT OR IGNORE INTO lineage_meta.entity_types (type_name, type_category, platform, icon, color, description) VALUES
('job', 'process', NULL, 'workflow', '#FF6B35', 'Orchestrated job/workflow'),
('task', 'process', NULL, 'task', '#FFA726', 'Task within a job'),
('subtask', 'process', NULL, 'subtask', '#FFD54F', 'Subtask (e.g., for-each iteration)'),
('notebook', 'process', 'databricks', 'notebook', '#FF3621', 'Databricks notebook'),
('dlt_pipeline', 'process', 'databricks', 'pipeline', '#00ADD8', 'Delta Live Tables pipeline'),
('pipeline', 'process', NULL, 'pipeline', '#0078D4', 'Data pipeline (ADF, Synapse, etc.)'),
('activity', 'process', NULL, 'activity', '#50E6FF', 'Pipeline activity'),
('dataflow', 'process', NULL, 'dataflow', '#0078D4', 'Data transformation flow'),
('ssis_package', 'process', 'ssis', 'package', '#CC2927', 'SSIS Package'),
('ssis_task', 'process', 'ssis', 'task', '#CC2927', 'SSIS Control Flow Task'),
('ssis_dataflow', 'process', 'ssis', 'dataflow', '#CC2927', 'SSIS Data Flow Task'),
('ssis_component', 'process', 'ssis', 'component', '#CC2927', 'SSIS Data Flow Component'),
('talend_job', 'process', 'talend', 'job', '#FF6D70', 'Talend Job'),
('talend_component', 'process', 'talend', 'component', '#FF6D70', 'Talend Component'),
('synapse_pipeline', 'process', 'synapse', 'pipeline', '#0078D4', 'Synapse Pipeline'),
('synapse_notebook', 'process', 'synapse', 'notebook', '#0078D4', 'Synapse Notebook'),
('synapse_spark_pool', 'process', 'synapse', 'spark', '#E8590C', 'Synapse Spark Pool'),
('adf_pipeline', 'process', 'adf', 'pipeline', '#0078D4', 'Azure Data Factory Pipeline'),
('adf_activity', 'process', 'adf', 'activity', '#50E6FF', 'ADF Activity'),
('adf_dataflow', 'process', 'adf', 'dataflow', '#0078D4', 'ADF Mapping Data Flow');

-- CONSUMPTION ASSETS
INSERT OR IGNORE INTO lineage_meta.entity_types (type_name, type_category, platform, icon, color, description) VALUES
('dataset', 'consumption', 'powerbi', 'dataset', '#F2C811', 'Power BI Dataset/Semantic Model'),
('semantic_table', 'consumption', 'powerbi', 'table', '#F2C811', 'Table in Power BI semantic model'),
('measure', 'consumption', 'powerbi', 'measure', '#E87400', 'DAX Measure'),
('calculated_column', 'consumption', 'powerbi', 'column-calc', '#E87400', 'DAX Calculated Column'),
('calculated_table', 'consumption', 'powerbi', 'table-calc', '#E87400', 'DAX Calculated Table'),
('hierarchy', 'consumption', 'powerbi', 'hierarchy', '#F2C811', 'Power BI Hierarchy'),
('report', 'consumption', 'powerbi', 'report', '#F2C811', 'Power BI Report'),
('page', 'consumption', 'powerbi', 'page', '#F2C811', 'Report Page'),
('visual', 'consumption', 'powerbi', 'chart', '#F2C811', 'Report Visual'),
('dashboard', 'consumption', 'powerbi', 'dashboard', '#F2C811', 'Power BI Dashboard'),
('tile', 'consumption', 'powerbi', 'tile', '#F2C811', 'Dashboard Tile'),
('tableau_workbook', 'consumption', 'tableau', 'workbook', '#E97627', 'Tableau Workbook'),
('tableau_datasource', 'consumption', 'tableau', 'datasource', '#E97627', 'Tableau Data Source'),
('tableau_worksheet', 'consumption', 'tableau', 'worksheet', '#E97627', 'Tableau Worksheet'),
('tableau_dashboard', 'consumption', 'tableau', 'dashboard', '#E97627', 'Tableau Dashboard'),
('tableau_calculated_field', 'consumption', 'tableau', 'field-calc', '#E97627', 'Tableau Calculated Field'),
('fabric_lakehouse', 'consumption', 'fabric', 'lakehouse', '#0078D4', 'Microsoft Fabric Lakehouse'),
('fabric_warehouse', 'consumption', 'fabric', 'warehouse', '#0078D4', 'Microsoft Fabric Warehouse'),
('fabric_semantic_model', 'consumption', 'fabric', 'dataset', '#F2C811', 'Fabric Semantic Model');

-- ORGANIZATIONAL
INSERT OR IGNORE INTO lineage_meta.entity_types (type_name, type_category, platform, icon, color, description) VALUES
('workspace', 'organizational', NULL, 'workspace', '#6B7280', 'Workspace (Databricks, Power BI, Fabric)'),
('folder', 'organizational', NULL, 'folder', '#6B7280', 'Logical folder'),
('catalog', 'organizational', 'databricks', 'catalog', '#FF3621', 'Unity Catalog'),
('environment', 'organizational', NULL, 'environment', '#4CAF50', 'Environment (dev, test, prod)'),
('resource_group', 'organizational', 'azure', 'resource-group', '#0078D4', 'Azure Resource Group'),
('subscription', 'organizational', 'azure', 'subscription', '#0078D4', 'Azure Subscription'),
('server', 'organizational', NULL, 'server', '#6B7280', 'Database server instance'),
('cluster', 'organizational', 'databricks', 'cluster', '#FF3621', 'Databricks Cluster');

-- ----------------------------------------------------------------------------
-- RELATIONSHIP TYPES (predicates)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lineage_meta.relationship_types (
    rel_type_id    INTEGER PRIMARY KEY,
    rel_type_name  TEXT NOT NULL UNIQUE,
    rel_category   TEXT NOT NULL,     -- lineage, composition, containment, reference, orchestration
    inverse_name   TEXT,
    is_directional INTEGER DEFAULT 1,
    description    TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS lineage_meta.idx_relationship_types_category ON relationship_types(rel_category);

-- LINEAGE (data flow)
INSERT OR IGNORE INTO lineage_meta.relationship_types (rel_type_name, rel_category, inverse_name, is_directional, description) VALUES
('produces', 'lineage', 'produced_by', 1, 'Process creates/writes data asset'),
('consumes', 'lineage', 'consumed_by', 1, 'Process reads data asset'),
('derives_from', 'lineage', 'source_of', 1, 'Data asset derived from another (e.g., view from table)'),
('feeds', 'lineage', 'fed_by', 1, 'Data flows from source to target'),
('transforms_to', 'lineage', 'transformed_from', 1, 'Data transformed into another form'),
('copies_to', 'lineage', 'copied_from', 1, 'Data copied without transformation'),
('aggregates', 'lineage', 'aggregated_in', 1, 'Data aggregated into target'),
('joins', 'lineage', 'joined_with', 0, 'Tables joined together (bidirectional)'),
('filters', 'lineage', 'filtered_from', 1, 'Data filtered from source'),
('unions', 'lineage', 'unioned_in', 1, 'Data unioned into target');

-- COMPOSITION
INSERT OR IGNORE INTO lineage_meta.relationship_types (rel_type_name, rel_category, inverse_name, is_directional, description) VALUES
('contains', 'composition', 'contained_in', 1, 'Container holds component (Job->Task)'),
('executes', 'composition', 'executed_by', 1, 'Task executes process (Task->Notebook)'),
('implements', 'composition', 'implemented_by', 1, 'Process implements transformation logic'),
('calls', 'composition', 'called_by', 1, 'Process calls another process'),
('includes', 'composition', 'included_in', 1, 'Process includes another as sub-process'),
('depends_on_task', 'composition', 'dependency_of_task', 1, 'Task depends on another task completing');

-- CONTAINMENT
INSERT OR IGNORE INTO lineage_meta.relationship_types (rel_type_name, rel_category, inverse_name, is_directional, description) VALUES
('belongs_to', 'containment', 'owns', 1, 'Entity belongs to container (Table->Schema)'),
('hosted_in', 'containment', 'hosts', 1, 'Entity hosted in infrastructure (Dataset->Workspace)'),
('member_of', 'containment', 'has_member', 1, 'Entity is member of group'),
('deployed_to', 'containment', 'deployment_of', 1, 'Process deployed to environment'),
('stored_in', 'containment', 'stores', 1, 'Data stored in location');

-- REFERENCE
INSERT OR IGNORE INTO lineage_meta.relationship_types (rel_type_name, rel_category, inverse_name, is_directional, description) VALUES
('uses', 'reference', 'used_by', 1, 'Entity uses another (Measure->Column)'),
('references', 'reference', 'referenced_by', 1, 'Entity references another (Measure->Measure)'),
('relates_to', 'reference', 'related_from', 0, 'Semantic relationship (PBI table relationships)'),
('based_on', 'reference', 'basis_for', 1, 'Entity based on another'),
('visualizes', 'reference', 'visualized_by', 1, 'Visual displays data from source'),
('displays', 'reference', 'displayed_in', 1, 'Report page displays visual'),
('calculates', 'reference', 'calculated_by', 1, 'Measure calculates from columns');

-- ORCHESTRATION
INSERT OR IGNORE INTO lineage_meta.relationship_types (rel_type_name, rel_category, inverse_name, is_directional, description) VALUES
('triggers', 'orchestration', 'triggered_by', 1, 'Process triggers another process'),
('schedules', 'orchestration', 'scheduled_by', 1, 'Schedule activates process'),
('depends_on', 'orchestration', 'dependency_of', 1, 'Process depends on another completing'),
('notifies', 'orchestration', 'notified_by', 1, 'Process sends notification'),
('precedes', 'orchestration', 'follows', 1, 'Process runs before another'),
('runs_after', 'orchestration', 'runs_before', 1, 'Process runs after another completes'),
('retries', 'orchestration', 'retried_by', 1, 'Process retries on failure');

-- ----------------------------------------------------------------------------
-- NODES (graph vertices)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lineage_meta.nodes (
    node_id           INTEGER PRIMARY KEY,

    node_type         TEXT NOT NULL,              -- logical FK to entity_types.type_name
    qualified_name    TEXT NOT NULL UNIQUE,       -- globally unique (view-namespaced by the builder)
    display_name      TEXT NOT NULL,

    platform          TEXT NOT NULL,
    source_system     TEXT,
    external_id       TEXT,

    parent_node_id    INTEGER,
    hierarchy_path    TEXT,
    hierarchy_level   INTEGER DEFAULT 0,

    upstream_depth    INTEGER DEFAULT 0,
    downstream_depth  INTEGER DEFAULT 0,
    upstream_count    INTEGER DEFAULT 0,
    downstream_count  INTEGER DEFAULT 0,

    properties        TEXT,                       -- JSON (stored as text; not queried via JSON operators)

    owner             TEXT,
    steward           TEXT,
    classification    TEXT,
    tags              TEXT,

    description       TEXT,
    ai_description    TEXT,
    documentation_url TEXT,

    processing_mode   TEXT DEFAULT 'static'
                      CHECK (processing_mode IN ('static', 'parametric')),
    config_dependency TEXT,

    quality_score     REAL,
    status            TEXT DEFAULT 'active'
                      CHECK (status IN ('active', 'deprecated', 'archived', 'draft', 'deleted')),

    created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TEXT,
    last_extracted_at TEXT,
    extraction_source TEXT,

    is_active         INTEGER DEFAULT 1,
    deleted_at        TEXT,

    -- View scoping (folded-in migration): tenant/perspective isolation.
    view_id           INTEGER
);

CREATE INDEX IF NOT EXISTS lineage_meta.idx_nodes_type ON nodes(node_type);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_nodes_platform ON nodes(platform);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_nodes_view ON nodes(view_id);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_nodes_parent ON nodes(parent_node_id);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_nodes_external_id ON nodes(platform, external_id);

-- ----------------------------------------------------------------------------
-- EDGES (graph relationships)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lineage_meta.edges (
    edge_id               INTEGER PRIMARY KEY,

    source_node_id        INTEGER NOT NULL,
    target_node_id        INTEGER NOT NULL,
    relationship_type     TEXT NOT NULL,
    relationship_category TEXT NOT NULL
                          CHECK (relationship_category IN ('lineage', 'composition', 'containment', 'reference', 'orchestration')),

    source_columns        TEXT,                   -- JSON array
    target_columns        TEXT,                   -- JSON array
    transformation_logic  TEXT,

    lineage_type          TEXT DEFAULT 'explicit'
                          CHECK (lineage_type IN ('explicit', 'parametric', 'inferred')),
    config_node_id        INTEGER,
    parameter_expression  TEXT,

    confidence            TEXT DEFAULT 'high'
                          CHECK (confidence IN ('high', 'medium', 'low', 'inferred')),
    confidence_score      REAL,
    extraction_method     TEXT,

    properties            TEXT,

    created_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    last_validated_at     TEXT,
    validation_status     TEXT DEFAULT 'valid'
                          CHECK (validation_status IN ('valid', 'stale', 'broken', 'unvalidated')),

    is_active             INTEGER DEFAULT 1,
    deleted_at            TEXT,

    CHECK (source_node_id <> target_node_id),
    UNIQUE (source_node_id, target_node_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS lineage_meta.idx_edges_source ON edges(source_node_id);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_edges_target ON edges(target_node_id);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_edges_type ON edges(relationship_type);
CREATE INDEX IF NOT EXISTS lineage_meta.idx_edges_category ON edges(relationship_category);
