-- ============================================================================
-- GENERATED from 04_powerbi_meta.sql by _translate_from_supabase.py — DO NOT EDIT BY HAND.
-- SQLite dialect for the public self-host edition (tables + indexes only;
-- helper views/functions dropped, FKs stripped, ATTACH-per-schema resolves
-- the schema qualifier). Regenerate instead of editing.
-- ============================================================================

-- ============================================================================
-- ade-catalog - Power BI Meta Schema (Supabase / PostgreSQL)
-- ============================================================================
-- Purpose: Track Power BI reports, datasets, and their data source dependencies
-- Schema: powerbi_meta
-- Includes: transformations table (from 04_powerbi_meta_transformations.sql)
-- ============================================================================
-- Reports: Power BI report definitions
CREATE TABLE IF NOT EXISTS powerbi_meta.reports (
    report_id INTEGER PRIMARY KEY,
    view_id INT NOT NULL ,
    report_name VARCHAR(500) NOT NULL,
    report_type VARCHAR(50) CHECK(report_type IN ('pbix', 'pbip', 'thin_report')),

    -- Workflow linkage
    workflow_id INT,

    -- Wave categorization
    wave VARCHAR(50),
    category VARCHAR(100),

    -- Metrics
    page_count INT DEFAULT 0,
    visual_count INT DEFAULT 0,
    measure_count INT DEFAULT 0,
    table_count INT DEFAULT 0,

    -- File info
    file_path TEXT,
    file_size_bytes BIGINT,

    -- Source artifact (report is a first-class item, distinct from its model)
    web_url VARCHAR(1000),
    source_report_id VARCHAR(100),
    source_workspace_id VARCHAR(100),

    -- Status
    is_active INTEGER DEFAULT TRUE,
    last_published_at TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_analyzed_at TEXT,

    UNIQUE(view_id, report_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_reports_view ON reports(view_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_reports_name ON reports(report_name);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_reports_workflow ON reports(workflow_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_reports_wave ON reports(wave);

-- Datasets: Power BI datasets/semantic models
CREATE TABLE IF NOT EXISTS powerbi_meta.datasets (
    dataset_id INTEGER PRIMARY KEY,
    report_id INT ,
    dataset_name VARCHAR(500) NOT NULL,

    -- Connection mode
    connection_mode VARCHAR(50) CHECK(connection_mode IN ('import', 'direct_query', 'live_connection', 'composite')),

    -- Metadata
    table_count INT DEFAULT 0,
    relationship_count INT DEFAULT 0,
    measure_count INT DEFAULT 0,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(report_id, dataset_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_datasets_report ON datasets(report_id);

-- Tables: Tables within Power BI datasets
CREATE TABLE IF NOT EXISTS powerbi_meta.tables (
    table_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,
    table_name VARCHAR(500) NOT NULL,

    -- Source table reference
    source_type VARCHAR(50) CHECK(source_type IN ('postgres', 'sql_server', 'databricks', 'fabric', 'odbc', 'excel', 'sharepoint', 'calculated', 'import', 'entity', 'directquery', 'view', 'unknown', 'other')),
    source_database VARCHAR(200),
    source_schema VARCHAR(200),
    source_table VARCHAR(200),

    -- Metadata
    description TEXT,
    is_hidden INTEGER DEFAULT FALSE,

    -- Metrics
    column_count INT DEFAULT 0,
    row_count BIGINT,
    estimated_size_bytes BIGINT,

    -- Refresh
    is_calculated_table INTEGER DEFAULT FALSE,
    refresh_policy VARCHAR(100),

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(dataset_id, table_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_tables_dataset ON tables(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_tables_source_type ON tables(source_type);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_tables_source_table ON tables(source_table);

-- Measures: DAX measures
CREATE TABLE IF NOT EXISTS powerbi_meta.measures (
    measure_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,
    table_name VARCHAR(500),
    measure_name VARCHAR(1000) NOT NULL,

    dax_expression TEXT,

    -- Metadata
    display_folder VARCHAR(500),
    format_string VARCHAR(200),
    description TEXT,
    is_hidden INTEGER DEFAULT FALSE,
    data_type VARCHAR(100),

    -- Classification
    measure_type VARCHAR(100),
    complexity_score INT,

    -- Dependencies (TEXT arrays)
    depends_on_tables TEXT,
    depends_on_columns TEXT,
    depends_on_measures TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(dataset_id, table_name, measure_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_measures_dataset ON measures(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_measures_table ON measures(table_name);

-- Columns: Columns in Power BI tables
CREATE TABLE IF NOT EXISTS powerbi_meta.columns (
    column_id INTEGER PRIMARY KEY,
    table_id INT NOT NULL ,
    dataset_id INT NOT NULL ,
    column_name VARCHAR(1000) NOT NULL,

    data_type VARCHAR(100),
    is_hidden INTEGER DEFAULT FALSE,
    is_calculated INTEGER DEFAULT FALSE,

    -- Source column reference
    source_column VARCHAR(1000),

    -- Formatting and metadata
    format_string VARCHAR(200),
    is_key INTEGER DEFAULT FALSE,
    sort_by_column VARCHAR(1000),
    description TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(table_id, column_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_columns_table ON columns(table_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_columns_dataset ON columns(dataset_id);

-- Relationships: Relationships between tables in datasets
CREATE TABLE IF NOT EXISTS powerbi_meta.relationships (
    relationship_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,

    from_table_id INT ,
    from_column VARCHAR(500),

    to_table_id INT ,
    to_column VARCHAR(500),

    cardinality VARCHAR(50) CHECK(cardinality IN ('one_to_one', 'one_to_many', 'many_to_one', 'many_to_many')),
    cross_filter_direction VARCHAR(50) CHECK(cross_filter_direction IN ('single', 'both', 'none')),

    is_active INTEGER DEFAULT TRUE,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(dataset_id, from_table_id, from_column, to_table_id, to_column)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_relationships_dataset ON relationships(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_relationships_from ON relationships(from_table_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_relationships_to ON relationships(to_table_id);

-- Data Sources: Connection strings and data sources
CREATE TABLE IF NOT EXISTS powerbi_meta.data_sources (
    source_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,

    source_name VARCHAR(500),
    source_type VARCHAR(100) NOT NULL,
    connection_string TEXT,
    server VARCHAR(500),
    "database" VARCHAR(200),
    database_name VARCHAR(200),
    connection_mode VARCHAR(50),

    -- Security (no actual credentials stored!)
    authentication_method VARCHAR(100),

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(dataset_id, source_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_data_sources_dataset ON data_sources(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_data_sources_type ON data_sources(source_type);

-- Report Pages: Pages within reports
CREATE TABLE IF NOT EXISTS powerbi_meta.report_pages (
    page_id INTEGER PRIMARY KEY,
    report_id INT NOT NULL ,

    page_name VARCHAR(500) NOT NULL,
    page_number INT,
    page_order INT,

    -- Metadata
    is_hidden INTEGER DEFAULT FALSE,
    page_type VARCHAR(100),

    -- Dimensions
    width INT,
    height INT,

    visual_count INT DEFAULT 0,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(report_id, page_name)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_pages_report ON report_pages(report_id);

-- Visuals: Visualizations on report pages
CREATE TABLE IF NOT EXISTS powerbi_meta.visuals (
    visual_id INTEGER PRIMARY KEY,
    page_id INT NOT NULL ,
    report_id INT NOT NULL ,
    dataset_id INT ,

    visual_name VARCHAR(500),
    visual_type VARCHAR(100),

    -- Data bindings (TEXT arrays)
    tables_used TEXT,
    measures_used TEXT,
    columns_used TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_visuals_page ON visuals(page_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_visuals_report ON visuals(report_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_visuals_dataset ON visuals(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_visuals_type ON visuals(visual_type);

-- Refresh History: Dataset refresh tracking
CREATE TABLE IF NOT EXISTS powerbi_meta.refresh_history (
    refresh_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,

    refresh_type VARCHAR(50) CHECK(refresh_type IN ('manual', 'scheduled', 'api')),
    status VARCHAR(50) CHECK(status IN ('running', 'succeeded', 'failed', 'cancelled')),

    started_at TEXT,
    completed_at TEXT,
    duration_seconds INT,

    error_message TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_refresh_dataset ON refresh_history(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_refresh_status ON refresh_history(status);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_refresh_started ON refresh_history(started_at DESC);

-- Transformations: Power Query M code and DAX logic
-- (merged from 04_powerbi_meta_transformations.sql)
CREATE TABLE IF NOT EXISTS powerbi_meta.transformations (
    transformation_id INTEGER PRIMARY KEY,
    dataset_id INT NOT NULL ,

    transformation_name VARCHAR(500),
    location VARCHAR(50),                     -- PowerQuery, Model, DAX
    transformation_type VARCHAR(100),          -- Merge, Join, Filter, Aggregate, Calculate, etc.
    object_name VARCHAR(500),                 -- Query/Measure/Column name

    logic TEXT,                               -- M code or DAX
    input_objects TEXT,                        -- TEXT array of input objects
    output_objects TEXT,                       -- TEXT array of output objects

    process_id VARCHAR(200),                  -- Link to business process
    impact_level VARCHAR(50),                 -- Low, Medium, High

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(dataset_id, object_name, transformation_type)
);

CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_transformations_dataset ON transformations(dataset_id);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_transformations_location ON transformations(location);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_transformations_type ON transformations(transformation_type);
CREATE INDEX IF NOT EXISTS powerbi_meta.idx_powerbi_transformations_object ON transformations(object_name);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- View: Reports with Dataset Summary
