"""
Translate a Supabase (PostgreSQL) schema DDL file into the SQLite dialect used by
the public self-host edition. Bounded to OUR own DDL (not a general translator).

Rules (validated by the TICK-022 spike):
  * Keep only tables + indexes: truncate at the first CREATE VIEW/FUNCTION/TRIGGER
    (the v_* helper views are unused by the read paths; plpgsql has no SQLite form,
    and the graph read path is a WITH RECURSIVE CTE, no stored procs).
  * Drop CREATE SCHEMA (the SqliteBackend ATTACHes one file per schema).
  * Strip FK REFERENCES (inline + named CONSTRAINT, incl. the cross-schema view_id
    FK): SQLite can't FK across attached databases and the public edition doesn't
    enforce FKs.
  * Types: SERIAL->INTEGER, TIMESTAMP(TZ)->TEXT, BOOLEAN->INTEGER,
    DECIMAL/NUMERIC(p,s)->REAL, JSON(B)->TEXT, NOW()->CURRENT_TIMESTAMP.
  * CREATE INDEX: the schema qualifier moves from the table to the INDEX name
    (`CREATE INDEX schema.idx ON table(...)`), the one SQLite syntax gotcha.

Usage:
    python -m ade_catalog.schemas.sqlite._translate_from_supabase \
        --in ade_catalog/schemas/supabase/02_databricks_meta.sql \
        --out ade_catalog/schemas/sqlite/02_databricks_meta.sql
"""

import argparse
import re
from pathlib import Path

_HEADER = (
    "-- ============================================================================\n"
    "-- GENERATED from {src} by _translate_from_supabase.py — DO NOT EDIT BY HAND.\n"
    "-- SQLite dialect for the public self-host edition (tables + indexes only;\n"
    "-- helper views/functions dropped, FKs stripped, ATTACH-per-schema resolves\n"
    "-- the schema qualifier). Regenerate instead of editing.\n"
    "-- ============================================================================\n\n"
)


def translate(sql: str) -> str:
    # 1. Keep only tables + indexes (drop everything from the first view/function on)
    m = re.search(r'(?im)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:VIEW|FUNCTION|TRIGGER)\b', sql)
    if m:
        sql = sql[:m.start()]

    # 2. Drop CREATE SCHEMA
    sql = re.sub(r'(?im)^\s*CREATE\s+SCHEMA[^;]*;\s*', '', sql)

    # 3. Strip FK REFERENCES (inline + named CONSTRAINT), incl. ON DELETE/UPDATE actions
    sql = re.sub(
        r'(?is)(?:\bCONSTRAINT\s+\w+\s+)?\bREFERENCES\s+[\w.]+\s*\([^)]*\)'
        r'(?:\s+ON\s+DELETE\s+(?:CASCADE|NO\s+ACTION|SET\s+NULL|SET\s+DEFAULT|RESTRICT))?'
        r'(?:\s+ON\s+UPDATE\s+(?:CASCADE|NO\s+ACTION|SET\s+NULL|SET\s+DEFAULT|RESTRICT))?',
        '', sql)

    # 4. Type transforms
    sql = re.sub(r'(?i)\bSERIAL\b', 'INTEGER', sql)
    sql = re.sub(r'(?i)\b(?:TIMESTAMPTZ|TIMESTAMP)\b', 'TEXT', sql)
    sql = re.sub(r'(?i)\bBOOLEAN\b', 'INTEGER', sql)
    sql = re.sub(r'(?i)\b(?:DECIMAL|NUMERIC)\s*\([^)]*\)', 'REAL', sql)
    sql = re.sub(r'(?i)\bJSONB\b', 'TEXT', sql)
    sql = re.sub(r'(?i)\bJSON\b', 'TEXT', sql)
    sql = re.sub(r'(?i)\bNOW\s*\(\)', 'CURRENT_TIMESTAMP', sql)

    # 5. CREATE INDEX: move the schema qualifier from the table to the index name
    sql = re.sub(
        r'(?i)CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)\.(\w+)',
        r'CREATE INDEX IF NOT EXISTS \2.\1 ON \3', sql)

    # tidy: collapse 3+ blank lines
    sql = re.sub(r'\n{3,}', '\n\n', sql).strip() + '\n'
    return sql


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst)
    out = _HEADER.format(src=src.name) + translate(src.read_text(encoding="utf-8"))
    dst.write_text(out, encoding="utf-8")
    print(f"wrote {dst} ({out.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
