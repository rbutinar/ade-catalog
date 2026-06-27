"""
ADE MCP Server - SQLite Backend

Exposes ADE metadata catalog to AI agents via Model Context Protocol.
Uses SQLite (CatalogDB) for persistent, portable storage.

Usage:
    python -m ade_app.mcp_server.server

Or configure in Claude Code's MCP settings.
"""

import sys
import json
import logging
from pathlib import Path
from typing import Optional

# Setup logging
LOG_FILE = Path(__file__).parent.parent.parent / 'ade_mcp.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger('ade_mcp')

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("ade")

# =============================================================================
# CONFIGURATION
# =============================================================================

# Repository root (ade-catalog/)
REPO_ROOT = Path(__file__).parent.parent.parent
ADE_DATA_ROOT = REPO_ROOT / "ade_data"

# Current environment (can be changed at runtime)
_current_environment: str = "demo"

# Cached CatalogDB instances per environment
_catalog_cache: dict = {}


def _get_environment_path(env_name: str = None) -> Path:
    """Get path to environment data folder."""
    env = env_name or _current_environment
    return ADE_DATA_ROOT / env


def _get_catalog(env_name: str = None):
    """Get (or create) a CatalogDB for the environment."""
    from ade_app.core.catalog import CatalogDB

    env = env_name or _current_environment
    if env not in _catalog_cache:
        db_path = _get_environment_path(env) / "catalog.db"
        if not db_path.exists():
            return None
        _catalog_cache[env] = CatalogDB(db_path)
    return _catalog_cache[env]


# =============================================================================
# ENVIRONMENT MANAGEMENT
# =============================================================================

def _get_available_environments() -> list[dict]:
    """Scan ade_data directory for available environments."""
    environments = []

    if not ADE_DATA_ROOT.exists():
        return environments

    for env_dir in sorted(ADE_DATA_ROOT.iterdir()):
        if env_dir.is_dir() and not env_dir.name.startswith('_'):
            config_file = env_dir / "config.yaml"
            catalog_db = env_dir / "catalog.db"
            extractions_dir = env_dir / "extractions"

            has_data = config_file.exists() or catalog_db.exists() or extractions_dir.exists()

            if has_data:
                env_info = {
                    "id": env_dir.name,
                    "name": env_dir.name.upper(),
                    "path": str(env_dir),
                    "has_config": config_file.exists(),
                    "has_catalog_db": catalog_db.exists(),
                    "has_extractions": extractions_dir.exists(),
                }

                if config_file.exists():
                    try:
                        import yaml
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = yaml.safe_load(f)
                        env_info["name"] = config.get('environment', {}).get('name', env_dir.name.upper())
                        env_info["description"] = config.get('environment', {}).get('description', '')
                    except Exception:
                        pass

                environments.append(env_info)

    return environments


@mcp.tool()
async def list_environments() -> dict:
    """List all available ADE environments.

    Scans the ade_data directory for configured environments.
    Each environment contains extracted metadata from various platforms.

    Returns:
        List of environments with their details
    """
    environments = _get_available_environments()
    return {
        "current_environment": _current_environment,
        "count": len(environments),
        "environments": environments
    }


@mcp.tool()
async def set_environment(environment_id: str) -> dict:
    """Switch to a different ADE environment.

    Changes the active environment for all subsequent catalog queries.

    Args:
        environment_id: The environment ID to switch to (e.g., 'demo')

    Returns:
        Confirmation of environment switch
    """
    global _current_environment

    env_path = ADE_DATA_ROOT / environment_id
    if not env_path.exists():
        available = [e["id"] for e in _get_available_environments()]
        return {
            "success": False,
            "error": f"Environment '{environment_id}' not found",
            "available_environments": available
        }

    old_environment = _current_environment
    _current_environment = environment_id

    logger.info(f"Environment switched: {old_environment} -> {environment_id}")

    return {
        "success": True,
        "previous_environment": old_environment,
        "current_environment": environment_id,
        "message": f"Switched to environment '{environment_id}'"
    }


@mcp.tool()
async def get_environment_info() -> dict:
    """Get information about the current ADE environment.

    Returns the active environment and available platforms/data.
    """
    catalog = _get_catalog()

    platforms = []
    if catalog:
        platform_names = catalog.get_platforms()
        for pname in platform_names:
            stats = catalog.get_stats(pname)
            platforms.append({
                "name": pname,
                "object_types": list(stats.keys()),
                "total_objects": sum(stats.values()),
            })

    return {
        "environment_id": _current_environment,
        "path": str(_get_environment_path()),
        "backend": "SQLite",
        "platforms_with_data": platforms,
        "available_environments": [e["id"] for e in _get_available_environments()]
    }


# =============================================================================
# CATALOG SEARCH
# =============================================================================

@mcp.tool()
async def search_catalog(
    query: str = "",
    platform: str = None,
    object_type: str = None,
    limit: int = 20
) -> dict:
    """Search the ADE metadata catalog for objects.

    Args:
        query: Search term (searches in object names). Use '*' or empty for all.
        platform: Filter by platform (databricks, postgresql, powerbi, etc.)
        object_type: Filter by type (notebook, job, table, measure, etc.)
        limit: Maximum results (default 20, max 100)

    Returns:
        List of matching objects with basic metadata
    """
    limit = min(limit, 100)
    catalog = _get_catalog()

    if not catalog:
        return {
            "query": query,
            "error": f"No catalog.db found for environment '{_current_environment}'",
            "results": []
        }

    results = catalog.search(query, platform, object_type, limit)

    simplified = []
    for item in results:
        simplified.append({
            "name": item.get("name", ""),
            "platform": item.get("platform", ""),
            "type": item.get("object_type", ""),
            "path": item.get("path", ""),
            "description": (item.get("description", "") or "")[:100],
        })

    return {
        "query": query,
        "filters": {"platform": platform, "object_type": object_type},
        "count": len(simplified),
        "results": simplified
    }


@mcp.tool()
async def get_object_details(
    name: str,
    platform: str,
    object_type: str = None
) -> dict:
    """Get detailed information about a specific object.

    Args:
        name: Name or path of the object
        platform: The platform (databricks, postgresql, powerbi, etc.)
        object_type: Optional type filter (notebook, job, table, etc.)

    Returns:
        Full object details including all metadata
    """
    catalog = _get_catalog()

    if not catalog:
        return {"found": False, "error": "No catalog.db found"}

    obj = catalog.get_object(name, platform, object_type)
    if not obj:
        return {
            "found": False,
            "error": f"Object '{name}' not found in {platform}",
            "suggestion": "Use search_catalog() to find available objects"
        }

    # Attach children (columns/measures for tables)
    obj_id = obj.get("id")
    if obj_id:
        children = catalog.get_children(obj_id)
        if children:
            obj["children"] = children

    return {"found": True, "object": obj}


@mcp.tool()
async def get_platform_stats(platform: str = None) -> dict:
    """Get statistics about objects in the metadata catalog.

    Args:
        platform: Optional - filter stats for a specific platform

    Returns:
        Object counts by platform and type
    """
    catalog = _get_catalog()

    if not catalog:
        return {"error": "No catalog.db found", "stats": {}}

    stats = catalog.get_stats(platform)

    return {
        "environment": _current_environment,
        "platform_filter": platform,
        "stats": stats
    }


# =============================================================================
# LINEAGE (Simplified - based on notebook parsing)
# =============================================================================

def _resolve_table_variables(source_code: str) -> dict[str, str]:
    """Extract variable assignments that look like table names (schema.table)."""
    import re
    assignments = {}
    pattern = r'(\w+)\s*=\s*["\']([a-zA-Z_]\w*\.[a-zA-Z_]\w*)["\']'
    for var_name, table_name in re.findall(pattern, source_code):
        assignments[var_name] = table_name
    return assignments


def _is_table_name(name: str) -> bool:
    """Filter out Python module paths and keep only real table references."""
    exclude = ['pyspark', 'spark', 'delta', 'org.', 'com.', 'io.', 'java.']
    return not any(pkg in name.lower() for pkg in exclude)


def _extract_table_references(source_code: str) -> list[str]:
    """Extract table references from notebook source code.

    Handles both direct string literals and variable references:
      - spark.table("schema.table") and spark.table(VAR)
      - .saveAsTable("schema.table") and .saveAsTable(VAR)
      - SQL FROM/JOIN/INTO schema.table (excludes Python imports)
    """
    import re

    tables = set()
    var_map = _resolve_table_variables(source_code)

    # --- Direct string literal patterns ---
    direct_patterns = [
        r'spark\.table\(["\']([^"\']+)["\']\)',    # spark.table("schema.table")
        r'\.saveAsTable\(["\']([^"\']+)["\']\)',    # .saveAsTable("table")
        r'\.insertInto\(["\']([^"\']+)["\']\)',     # .insertInto("table")
    ]
    for pattern in direct_patterns:
        tables.update(re.findall(pattern, source_code))

    # --- Variable reference patterns ---
    var_patterns = [
        r'spark\.table\((\w+)\)',       # spark.table(VAR)
        r'\.saveAsTable\((\w+)\)',      # .saveAsTable(VAR)
        r'\.insertInto\((\w+)\)',       # .insertInto(VAR)
    ]
    for pattern in var_patterns:
        for var_name in re.findall(pattern, source_code):
            if var_name in var_map:
                tables.add(var_map[var_name])

    # --- SQL patterns (schema.table only, excludes Python imports) ---
    sql_patterns = [
        r'(?:FROM|JOIN)\s+(`?[a-zA-Z_]\w*\.[a-zA-Z_]\w*`?)',
        r'INTO\s+(`?[a-zA-Z_]\w*\.[a-zA-Z_]\w*`?)',
    ]
    for pattern in sql_patterns:
        for match in re.findall(pattern, source_code, re.IGNORECASE):
            clean = match.strip('`')
            if _is_table_name(clean):
                tables.add(clean)

    return sorted(list(tables))


@mcp.tool()
async def get_notebook_lineage(notebook_name: str) -> dict:
    """Analyze a Databricks notebook to find table dependencies.

    Parses the notebook source code to identify:
    - Tables read (upstream dependencies)
    - Tables written (downstream outputs)

    Args:
        notebook_name: Name or path of the notebook

    Returns:
        Lineage information with upstream and downstream tables
    """
    # Find the notebook
    result = await get_object_details(notebook_name, "databricks", "notebook")

    if not result.get("found"):
        return {
            "notebook": notebook_name,
            "error": "Notebook not found",
            "upstream": [],
            "downstream": []
        }

    notebook = result["object"]
    source_code = notebook.get("source_code", "") or notebook.get("content", "")

    if not source_code:
        return {
            "notebook": notebook_name,
            "warning": "No source code available for analysis",
            "upstream": [],
            "downstream": []
        }

    import re

    var_map = _resolve_table_variables(source_code)
    all_tables = _extract_table_references(source_code)

    upstream = set()
    downstream = set()

    # --- Upstream: spark.table(), FROM, JOIN ---
    # Direct string literals
    for match in re.findall(r'spark\.table\(["\']([^"\']+)["\']\)', source_code):
        upstream.add(match)
    # Variable references
    for var_name in re.findall(r'spark\.table\((\w+)\)', source_code):
        if var_name in var_map:
            upstream.add(var_map[var_name])
    # SQL FROM/JOIN
    for match in re.findall(r'(?:FROM|JOIN)\s+(`?[a-zA-Z_]\w*\.[a-zA-Z_]\w*`?)', source_code, re.IGNORECASE):
        clean = match.strip('`')
        if _is_table_name(clean):
            upstream.add(clean)

    # --- Downstream: saveAsTable(), insertInto(), INTO ---
    # Direct string literals
    for pattern in [r'\.saveAsTable\(["\']([^"\']+)["\']\)', r'\.insertInto\(["\']([^"\']+)["\']\)']:
        for match in re.findall(pattern, source_code):
            downstream.add(match)
    # Variable references
    for pattern in [r'\.saveAsTable\((\w+)\)', r'\.insertInto\((\w+)\)']:
        for var_name in re.findall(pattern, source_code):
            if var_name in var_map:
                downstream.add(var_map[var_name])
    # SQL INTO
    for match in re.findall(r'INTO\s+(`?[a-zA-Z_]\w*\.[a-zA-Z_]\w*`?)', source_code, re.IGNORECASE):
        clean = match.strip('`')
        if _is_table_name(clean):
            downstream.add(clean)

    return {
        "notebook": notebook.get("name") or notebook.get("path"),
        "path": notebook.get("path"),
        "upstream": sorted(list(upstream)),
        "downstream": sorted(list(downstream)),
        "all_table_references": all_tables
    }


# =============================================================================
# RESOURCES - Documentation
# =============================================================================

@mcp.resource("ade://guide")
def get_ade_guide() -> str:
    """Complete guide to ADE - what it is and how to use it."""
    return """# ADE Core - Analytics Data Environment

## What is ADE?

ADE is an open-source framework for **agentic data engineering**. It provides:

- **Metadata extraction** from data platforms (Databricks, Power BI, etc.)
- **Unified catalog** searchable via MCP tools
- **AI agent integration** via Model Context Protocol

## Quick Start

1. Check environment: `get_environment_info()`
2. Search catalog: `search_catalog("sales")`
3. Get details: `get_object_details("notebook_name", "databricks")`
4. Analyze lineage: `get_notebook_lineage("etl_notebook")`

## Available Tools

| Tool | Description |
|------|-------------|
| `list_environments()` | See available environments |
| `set_environment(id)` | Switch environment |
| `get_environment_info()` | Current environment details |
| `search_catalog(query)` | Find objects by name |
| `get_object_details(name, platform)` | Full object metadata |
| `get_platform_stats()` | Object counts |
| `get_notebook_lineage(name)` | Notebook dependencies |

## Supported Platforms

- **Databricks**: notebooks, jobs
- **Power BI**: tables, columns, measures (DAX), relationships
- **PostgreSQL**: tables, views (coming soon)

## Example Workflow

```
1. get_environment_info()           # Check what's available
2. search_catalog("etl")            # Find ETL notebooks
3. get_object_details("etl_sales", "databricks")  # See code
4. get_notebook_lineage("etl_sales")  # Find dependencies
```
"""


@mcp.tool()
async def get_ade_overview() -> dict:
    """Get a description of what ADE is and its capabilities.

    Returns:
        Overview of ADE including description, tools, and quick start
    """
    return {
        "name": "ADE Core - Analytics Data Environment",
        "version": "0.3.0",
        "description": (
            "Open-source framework for agentic data engineering. "
            "Extracts metadata from data platforms and exposes it to AI agents via MCP."
        ),
        "backend": "SQLite",
        "supported_platforms": [
            "databricks (notebooks, jobs)",
            "powerbi (tables, columns, measures, relationships)",
            "postgresql (coming soon)"
        ],
        "available_tools": [
            "list_environments()",
            "set_environment(id)",
            "get_environment_info()",
            "search_catalog(query, platform?, type?)",
            "get_object_details(name, platform)",
            "get_platform_stats(platform?)",
            "get_notebook_lineage(name)"
        ],
        "quick_start": [
            "1. get_environment_info() - See current environment",
            "2. search_catalog('*') - Browse all objects",
            "3. get_object_details('name', 'databricks') - Get full details"
        ],
        "current_environment": _current_environment,
        "repository": "https://github.com/rbutinar/ade-catalog"
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run the MCP server."""
    logger.info("Starting ADE MCP Server (SQLite backend)")
    logger.info(f"Data root: {ADE_DATA_ROOT}")
    logger.info(f"Default environment: {_current_environment}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
