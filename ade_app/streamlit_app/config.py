"""
Configuration for ADE Core - Streamlit Web Interface

Lightweight config using SQLite (CatalogDB) backend.
No SQL Server, no external dependencies beyond Streamlit.
"""
import os
from pathlib import Path

# =============================================================================
# PATH CONFIGURATION
# =============================================================================

ADE_APP_ROOT = Path(__file__).parent.parent
REPO_ROOT = ADE_APP_ROOT.parent
ADE_DATA_ROOT = Path(os.getenv("ADE_DATA_ROOT", REPO_ROOT / "ade_data"))

# Active environment
ADE_ENVIRONMENT = os.getenv("ADE_ENVIRONMENT", "demo")
ENV_PATH = ADE_DATA_ROOT / ADE_ENVIRONMENT
CATALOG_DB_PATH = ENV_PATH / "catalog.db"

# Display
ENVIRONMENT_DISPLAY_NAME = ADE_ENVIRONMENT.upper()

# App Configuration
APP_TITLE = "ADE Core"
APP_SUBTITLE = f"ADE {ENVIRONMENT_DISPLAY_NAME}"
APP_ICON = "🤖"
PAGE_CONFIG = {
    "page_title": APP_TITLE,
    "page_icon": APP_ICON,
    "layout": "wide",
    "initial_sidebar_state": "expanded",
}

# Supported platforms in ade-catalog
SUPPORTED_PLATFORMS = {
    "databricks": {"icon": "⚡", "name": "Databricks"},
    "powerbi": {"icon": "📊", "name": "Power BI"},
}
