"""
Deploy ade-catalog schemas to a SQLite catalog (public self-host edition).

Creates one .sqlite file per schema under the configured sqlite_path directory
and applies the SQLite DDL. Idempotent (CREATE TABLE IF NOT EXISTS + INSERT OR
IGNORE seeds), so it is safe to re-run.

Usage:
    CATALOG_DB_BACKEND=sqlite python -m ade_catalog.schemas.deploy_sqlite
    python -m ade_catalog.schemas.deploy_sqlite --path C:/path/to/catalog_sqlite
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from ade_catalog.core.config import load_config
from ade_catalog.core.db_backend import SQLITE_SCHEMAS

SCHEMAS_DIR = Path(__file__).parent / "sqlite"

# Ordered DDL files. catalog_meta MUST be first (other schemas reference the
# default view). Public-edition scope: catalog + Databricks + Power BI + lineage +
# enrichment. Fabric / extra platforms stay out (enterprise/lab).
DDL_FILES = [
    "01_catalog_meta.sql",
    "02_databricks_meta.sql",
    "04_powerbi_meta.sql",
    "06_lineage_meta.sql",
    "08_enrichment_meta.sql",
]


def _resolve_sqlite_dir(path_override: str = None) -> Path:
    if path_override:
        return Path(path_override)
    config = load_config()
    p = Path(config.sqlite_path)
    if not p.is_absolute():
        p = config.data_root / p
    return p


def _connect_with_schemas(sqlite_dir: Path) -> sqlite3.Connection:
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sqlite_dir / "_main.sqlite"), isolation_level=None)
    for schema in SQLITE_SCHEMAS:
        conn.execute(
            f"ATTACH DATABASE ? AS {schema}",
            (str(sqlite_dir / f"{schema}.sqlite"),),
        )
    return conn


def deploy(path_override: str = None) -> bool:
    sqlite_dir = _resolve_sqlite_dir(path_override)
    print(f"Deploying SQLite schemas to {sqlite_dir}")
    print()

    conn = _connect_with_schemas(sqlite_dir)
    try:
        for ddl_file in DDL_FILES:
            filepath = SCHEMAS_DIR / ddl_file
            if not filepath.exists():
                print(f"  SKIP {ddl_file} (not found)")
                continue
            try:
                conn.executescript(filepath.read_text(encoding="utf-8"))
                print(f"  [OK] {ddl_file}")
            except Exception as e:
                print(f"  [FAIL] {ddl_file}: {e}")
                return False
    finally:
        conn.close()

    print()
    print(f"SQLite schema deployment complete: {sqlite_dir}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Deploy ade-catalog SQLite schemas")
    parser.add_argument("--path", help="Override the sqlite catalog directory")
    args = parser.parse_args()

    if not deploy(path_override=args.path):
        sys.exit(1)


if __name__ == "__main__":
    main()
