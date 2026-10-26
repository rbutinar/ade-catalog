"""
Database backend abstraction for ade-catalog.

Supports SQL Server (pyodbc) and Supabase (PostgreSQL via psycopg2).
All database access should go through get_backend() to get the
appropriate implementation.

Usage:
    from ade_catalog.core.config import load_config
    from ade_catalog.core.db_backend import get_backend

    config = load_config()
    backend = get_backend(config)
    rows = backend.query("SELECT * FROM catalog_meta.catalog_views WHERE view_id = %s", (1,))
"""

import logging
import re
from typing import Optional, Protocol, runtime_checkable

from ade_catalog.core.config import CatalogConfig, DatabaseConfig, SupabaseConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL dialect translation
# ---------------------------------------------------------------------------

def _tsql_to_postgres(sql: str) -> str:
    """Convert T-SQL syntax to PostgreSQL.

    Handles the most common differences:
    - Parameter markers: ? → %s
    - SELECT TOP (N) → ... LIMIT N (moved to end)
    - OUTPUT INSERTED.col → RETURNING col
    - GETDATE() → NOW()
    - BIT literals: 1/0 in boolean context → TRUE/FALSE
    - DATEADD → interval arithmetic
    """
    out = sql

    # Parameter markers: ? → %s
    out = out.replace("?", "%s")

    # SELECT TOP (N) ... → SELECT ... LIMIT N
    top_match = re.search(r'SELECT\s+TOP\s*\(?\s*(%s|\d+)\s*\)?', out, re.IGNORECASE)
    if top_match:
        limit_val = top_match.group(1)
        out = re.sub(r'SELECT\s+TOP\s*\(?\s*(?:%s|\d+)\s*\)?', 'SELECT', out, count=1, flags=re.IGNORECASE)
        # Add LIMIT at the end (before any trailing whitespace)
        out = out.rstrip() + f" LIMIT {limit_val}"

    # OUTPUT INSERTED.col → RETURNING col
    out = re.sub(
        r'OUTPUT\s+INSERTED\.(\w+)',
        r'RETURNING \1',
        out,
        flags=re.IGNORECASE,
    )

    # GETDATE() → NOW()
    out = re.sub(r'GETDATE\s*\(\)', 'NOW()', out, flags=re.IGNORECASE)

    # is_active = 1 → is_active = TRUE (for boolean columns)
    # is_default = 1 → is_default = TRUE
    # This is a heuristic — only for known boolean column patterns
    for col in ('is_active', 'is_default', 'is_calculated_table', 'is_hidden',
                'is_key', 'is_calculated', 'is_nullable', 'has_widgets', 'has_sql'):
        out = re.sub(rf'{col}\s*=\s*1\b', f'{col} = TRUE', out, flags=re.IGNORECASE)
        out = re.sub(rf'{col}\s*=\s*0\b', f'{col} = FALSE', out, flags=re.IGNORECASE)

    return out


def _postgres_to_tsql(sql: str) -> str:
    """Convert PostgreSQL syntax to T-SQL.

    Reverse of _tsql_to_postgres for cases where queries are written
    in PostgreSQL style and need to run on SQL Server.
    """
    out = sql

    # %s → ?
    out = out.replace("%s", "?")

    # LIMIT N → TOP (handled differently — need to move to SELECT)
    limit_match = re.search(r'\bLIMIT\s+(\?|\d+)\s*$', out, re.IGNORECASE)
    if limit_match:
        limit_val = limit_match.group(1)
        out = re.sub(r'\bLIMIT\s+(?:\?|\d+)\s*$', '', out, flags=re.IGNORECASE)
        out = re.sub(r'SELECT\b', f'SELECT TOP ({limit_val})', out, count=1, flags=re.IGNORECASE)

    # RETURNING col[, col2] → OUTPUT INSERTED.col placed BEFORE VALUES.
    # T-SQL requires the OUTPUT clause to precede VALUES (it cannot trail the
    # statement like PostgreSQL's RETURNING), so we relocate it.
    ret_match = re.search(r'\bRETURNING\s+(.+?)\s*$', out, re.IGNORECASE | re.DOTALL)
    if ret_match:
        cols = [c.strip() for c in ret_match.group(1).split(',') if c.strip()]
        output_cols = ', '.join(f'INSERTED.{c}' for c in cols)
        out = out[:ret_match.start()].rstrip()
        if re.search(r'\bVALUES\b', out, re.IGNORECASE):
            out = re.sub(r'\bVALUES\b', f'OUTPUT {output_cols} VALUES',
                         out, count=1, flags=re.IGNORECASE)
        else:
            # INSERT ... SELECT / UPDATE / DELETE — append (best effort)
            out = f'{out} OUTPUT {output_cols}'

    # NOW() → GETDATE()
    out = re.sub(r'NOW\s*\(\)', 'GETDATE()', out, flags=re.IGNORECASE)

    # ILIKE → LIKE (SQL Server LIKE is case-insensitive under default collation)
    out = re.sub(r'\bILIKE\b', 'LIKE', out, flags=re.IGNORECASE)

    # WITH RECURSIVE → WITH (T-SQL recursive CTEs omit the RECURSIVE keyword)
    out = re.sub(r'\bWITH\s+RECURSIVE\b', 'WITH', out, flags=re.IGNORECASE)

    # Operator/literal translation, applied only outside single-quoted strings
    # (so string values containing these tokens are left untouched).
    out = _translate_pg_tokens_outside_quotes(out)

    return out


def _translate_pg_tokens_outside_quotes(sql: str) -> str:
    """Translate Postgres-only tokens to T-SQL, skipping quoted string literals.

    Covers the operators that must not touch string contents:
    - ``x::text`` / ``x::varchar`` / ``x::int`` casts → ``CAST(x AS ...)``
    - ``||`` string concatenation → ``+``
    - ``TRUE`` / ``FALSE`` boolean literals → ``1`` / ``0``
    """
    # Split keeping quoted segments; even indices are outside quotes.
    parts = re.split(r"('(?:[^']|'')*')", sql)
    for i in range(0, len(parts), 2):
        seg = parts[i]
        seg = re.sub(r'([\w.\[\]"]+)::(?:text|varchar)\b',
                     r'CAST(\1 AS VARCHAR(MAX))', seg, flags=re.IGNORECASE)
        seg = re.sub(r'([\w.\[\]"]+)::int(?:eger)?\b',
                     r'CAST(\1 AS INT)', seg, flags=re.IGNORECASE)
        seg = seg.replace('||', '+')
        seg = re.sub(r'\bTRUE\b', '1', seg, flags=re.IGNORECASE)
        seg = re.sub(r'\bFALSE\b', '0', seg, flags=re.IGNORECASE)
        parts[i] = seg
    return ''.join(parts)


def _postgres_to_sqlite(sql: str) -> str:
    """Convert PostgreSQL syntax to SQLite.

    SQLite is close to Postgres for our query surface — ``||`` concat, ``TRUE``/
    ``FALSE`` literals, ``WITH RECURSIVE``, ``RETURNING`` (3.35+) and ``LIMIT`` are
    all native — so this is a light touch compared with the T-SQL translation:

    - Parameter markers: ``%s`` → ``?``
    - ``ILIKE`` → ``LIKE`` (SQLite ``LIKE`` is case-insensitive for ASCII)
    - ``NOW()`` / ``GETDATE()`` → ``CURRENT_TIMESTAMP``
    - ``x::text`` / ``x::int`` casts → ``CAST(x AS ...)`` (outside quoted strings)

    Schema qualification (``schema.table``) is NOT rewritten: the SqliteBackend
    ATTACHes one file per schema under the schema name, so ``lineage_meta.nodes``
    resolves natively.
    """
    out = sql

    # Parameter markers: %s → ? (only if not already qmark style)
    if "%s" in out:
        out = out.replace("%s", "?")

    # ILIKE → LIKE
    out = re.sub(r'\bILIKE\b', 'LIKE', out, flags=re.IGNORECASE)

    # NOW() / GETDATE() → CURRENT_TIMESTAMP
    out = re.sub(r'\b(?:NOW|GETDATE)\s*\(\)', 'CURRENT_TIMESTAMP', out, flags=re.IGNORECASE)

    # ::type casts → CAST(...), only outside quoted string literals
    parts = re.split(r"('(?:[^']|'')*')", out)
    for i in range(0, len(parts), 2):
        seg = parts[i]
        seg = re.sub(r'([\w.\[\]"]+)::(?:text|varchar)\b',
                     r'CAST(\1 AS TEXT)', seg, flags=re.IGNORECASE)
        seg = re.sub(r'([\w.\[\]"]+)::int(?:eger)?\b',
                     r'CAST(\1 AS INTEGER)', seg, flags=re.IGNORECASE)
        parts[i] = seg
    out = ''.join(parts)

    return out


# Schemas the SQLite backend ATTACHes (one .sqlite file each) — the public
# self-host edition scope. Order is not significant for ATTACH. Files are created
# on first connect if absent; only the DDL-deployed ones carry tables.
SQLITE_SCHEMAS = (
    "catalog_meta",
    "databricks_meta",
    "powerbi_meta",
    "lineage_meta",
    "enrichment_meta",
)


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DatabaseBackend(Protocol):
    """Protocol for database backends."""

    dialect: str  # "sqlserver" or "postgres"

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT query, return rows as list of dicts."""
        ...

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute INSERT/UPDATE/DELETE, return affected row count."""
        ...

    def execute_returning(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute with RETURNING/OUTPUT clause."""
        ...

    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity. Returns (success, message)."""
        ...


# ---------------------------------------------------------------------------
# SQL Server backend
# ---------------------------------------------------------------------------

class SqlServerBackend:
    """SQL Server backend using pyodbc."""

    dialect = "sqlserver"

    def __init__(self, db_config: DatabaseConfig):
        self._db = db_config

    def _get_connection(self):
        from ade_catalog.core.database import get_connection
        return get_connection(self._db)

    def _adapt(self, sql: str, params: tuple = ()) -> tuple[str, tuple]:
        """Return (T-SQL, params), realigning params for any relocated LIMIT.

        Translate unless the query is explicitly written in T-SQL param style
        (`?` and no `%s`). This covers PostgreSQL-style queries both with `%s`
        params AND paramless ones (e.g. `WHERE is_active = TRUE`), which would
        otherwise reach SQL Server untranslated.

        A trailing `... LIMIT %s` is rewritten to `SELECT TOP (?) ...`, moving
        that placeholder from last to first; the matching param must move with
        it, or TOP binds to the first param and the real first predicate binds
        to the limit value (e.g. `WHERE view_id = '%'` → conversion error).
        """
        if "?" in sql and "%s" not in sql:
            return sql, params  # already T-SQL, leave params untouched
        moved_limit = re.search(r'\bLIMIT\s+%s\s*$', sql.rstrip(), re.IGNORECASE) is not None
        adapted = _postgres_to_tsql(sql)
        if moved_limit and params:
            params = (params[-1], *params[:-1])
        return adapted, params

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        adapted, params = self._adapt(sql, params)
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(adapted, params)
            if not cursor.description:
                return []
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        adapted, params = self._adapt(sql, params)
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(adapted, params)
            count = cursor.rowcount
            conn.commit()
            return count
        finally:
            conn.close()

    def execute_returning(self, sql: str, params: tuple = ()) -> list[dict]:
        adapted, params = self._adapt(sql, params)
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(adapted, params)
            if not cursor.description:
                conn.commit()
                return []
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def test_connection(self) -> tuple[bool, str]:
        from ade_catalog.core.database import test_connection
        return test_connection(self._db)


# ---------------------------------------------------------------------------
# Supabase (PostgreSQL) backend
# ---------------------------------------------------------------------------

class SupabaseBackend:
    """Supabase backend using psycopg2.

    Reuses ONE connection per Flask request (stored on ``g``, closed by the app
    teardown) or per instance for non-Flask contexts (CLI/publish/MCP), instead
    of opening a fresh connection per statement. Opening a connection per query
    was the main latency cost on serverless + a remote DB (every statement paid a
    TCP+TLS handshake, often transatlantic) and made the publish engine crawl.

    Connections are autocommit (each statement stands alone — no
    idle-in-transaction, no cross-statement transaction to manage). The
    per-request connection is also the natural hook for a future RLS org context
    (``SET LOCAL app.current_org = ...`` at request start).
    """

    dialect = "postgres"

    def __init__(self, sb_config: SupabaseConfig):
        self._config = sb_config
        self._conn = None  # reusable connection for non-Flask contexts

    def _new_connection(self):
        import psycopg2
        db_url = self._config.db_url
        # Ensure sslmode is set for Supabase
        if "sslmode" not in db_url:
            sep = "&" if "?" in db_url else "?"
            db_url = f"{db_url}{sep}sslmode=require"
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        return conn

    def _acquire(self):
        """Return a live connection: reused within a Flask request (on ``g``,
        closed by the app teardown) or per instance otherwise. Reconnects if the
        cached connection was closed.

        When a request carries an authenticated non-super identity
        (``g.db_auth_uid``, set by the web ``before_request`` or the MCP auth), a
        freshly opened per-request connection is switched to the ``authenticated``
        role with the user's jwt claim so Postgres RLS binds (TICK-015). This is
        FAIL-CLOSED: if the role switch errors, the connection is discarded and
        the error raised — a scoped user is never served an unscoped (postgres /
        bypassrls) connection.
        """
        in_request = False
        try:
            from flask import has_request_context
            in_request = has_request_context()
        except Exception:
            in_request = False  # Flask not importable / no app context

        if in_request:
            from flask import g
            conn = getattr(g, "_supabase_conn", None)
            if conn is None or conn.closed:
                conn = self._new_connection()
                try:
                    self._apply_rls_identity(conn)
                except Exception:
                    try:
                        conn.close()
                    finally:
                        pass
                    raise  # fail-closed: do not hand back an unscoped connection
                g._supabase_conn = conn
            return conn

        # Not in a Flask request (CLI / publish): per-instance connection, no RLS
        # identity — these paths run as the bypassrls postgres role by design.
        if self._conn is None or self._conn.closed:
            self._conn = self._new_connection()
        return self._conn

    def _apply_rls_identity(self, conn):
        """Switch a fresh per-request connection to the ``authenticated`` role and
        inject the jwt ``sub`` claim, so ``auth.uid()`` resolves and RLS policies
        bind for this request. No-op when no scoped identity is set on ``g``
        (super-admin, local mode, and non-Flask contexts stay on postgres).

        Uses session-scoped ``set_config(..., is_local => false)`` because the
        connection is autocommit (no surrounding transaction) and the claim must
        persist across every query of the request on this reused connection.
        """
        try:
            from flask import g
            uid = getattr(g, "db_auth_uid", None)
        except Exception:
            uid = None
        if not uid:
            return
        import json
        claims = json.dumps({"sub": str(uid), "role": "authenticated"})
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
            cur.execute("SET ROLE authenticated")

    def _adapt_sql(self, sql: str) -> str:
        """Ensure SQL is in PostgreSQL dialect."""
        # If the SQL contains ? params, convert to PostgreSQL
        if "?" in sql and "%s" not in sql:
            return _tsql_to_postgres(sql)
        return sql

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        import psycopg2.extras
        conn = self._acquire()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(self._adapt_sql(sql), params)
            return [dict(row) for row in cur.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> int:
        conn = self._acquire()
        with conn.cursor() as cur:
            cur.execute(self._adapt_sql(sql), params)
            return cur.rowcount

    def execute_returning(self, sql: str, params: tuple = ()) -> list[dict]:
        import psycopg2.extras
        conn = self._acquire()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(self._adapt_sql(sql), params)
            return [dict(row) for row in cur.fetchall()]

    def query_ephemeral(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a read on a throwaway connection — NEVER the request-scoped one on
        ``g``. Used for pre-auth lookups (e.g. MCP api-key validation) that must
        run as the base postgres role and must not create the request's reused
        connection before its RLS identity (``g.db_auth_uid``) is known."""
        import psycopg2.extras
        conn = self._new_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(self._adapt_sql(sql), params)
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def execute_ephemeral(self, sql: str, params: tuple = ()) -> int:
        """Write counterpart of :meth:`query_ephemeral` (throwaway connection)."""
        conn = self._new_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(self._adapt_sql(sql), params)
            return cur.rowcount
        finally:
            conn.close()

    def close(self):
        """Close the per-instance connection (non-Flask contexts)."""
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        # Ephemeral connection — never touches the reusable one.
        try:
            conn = self._new_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.close()
            return True, "Connected to Supabase PostgreSQL"
        except Exception as e:
            return False, f"Connection failed: {e}"


# ---------------------------------------------------------------------------
# SQLite backend (public self-host edition)
# ---------------------------------------------------------------------------

class SqliteBackend:
    """SQLite backend for the public, local, single-user self-host edition.

    Reuses ONE autocommit connection per instance (the public edition is local
    single-user; no Flask request scoping / RLS is needed — that is a SaaS-only
    concern). Postgres-style SQL is translated by :func:`_postgres_to_sqlite`.

    Schema qualification is handled by ATTACHing one ``.sqlite`` file per schema
    under the schema name (``lineage_meta.nodes`` resolves against the file
    ATTACHed ``AS lineage_meta``), so the schema-qualified queries written for
    Postgres/SQL Server run unchanged.
    """

    dialect = "sqlite"

    def __init__(self, sqlite_dir):
        from pathlib import Path
        self._dir = Path(sqlite_dir)
        self._conn = None

    def _new_connection(self):
        import sqlite3
        self._dir.mkdir(parents=True, exist_ok=True)
        # A dedicated "main" file keeps every table under a named schema alias;
        # nothing lives in the anonymous main schema.
        conn = sqlite3.connect(
            str(self._dir / "_main.sqlite"),
            check_same_thread=False,  # local dev server may serve on worker threads
            isolation_level=None,     # autocommit — mirrors the Supabase backend
        )
        conn.row_factory = sqlite3.Row
        for schema in SQLITE_SCHEMAS:
            conn.execute(
                f"ATTACH DATABASE ? AS {schema}",
                (str(self._dir / f"{schema}.sqlite"),),
            )
        return conn

    def _acquire(self):
        if self._conn is None:
            self._conn = self._new_connection()
        return self._conn

    @staticmethod
    def _rows(cursor) -> list[dict]:
        if cursor.description is None:
            return []
        return [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._acquire()
        cur = conn.execute(_postgres_to_sqlite(sql), params)
        try:
            return self._rows(cur)
        finally:
            cur.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        conn = self._acquire()
        cur = conn.execute(_postgres_to_sqlite(sql), params)
        try:
            return cur.rowcount
        finally:
            cur.close()

    def execute_returning(self, sql: str, params: tuple = ()) -> list[dict]:
        # SQLite 3.35+ supports RETURNING natively; fetch the returned rows.
        conn = self._acquire()
        cur = conn.execute(_postgres_to_sqlite(sql), params)
        try:
            return self._rows(cur)
        finally:
            cur.close()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            conn = self._new_connection()
            conn.execute("SELECT 1")
            conn.close()
            return True, f"Connected to SQLite at {self._dir}"
        except Exception as e:
            return False, f"Connection failed: {e}"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_backend_cache: dict[str, DatabaseBackend] = {}


def get_backend(config: CatalogConfig) -> DatabaseBackend:
    """Get the appropriate database backend based on config."""
    cache_key = config.db_backend

    if cache_key not in _backend_cache:
        if config.db_backend == "supabase":
            _backend_cache[cache_key] = SupabaseBackend(config.supabase)
        elif config.db_backend == "sqlite":
            from pathlib import Path
            p = Path(config.sqlite_path)
            if not p.is_absolute():
                p = config.data_root / p
            _backend_cache[cache_key] = SqliteBackend(p)
        else:
            _backend_cache[cache_key] = SqlServerBackend(config.database)

    return _backend_cache[cache_key]


def get_active_backend(config: CatalogConfig) -> DatabaseBackend:
    """Return the request-scoped org backend if one is set on Flask's ``g``,
    otherwise the default backend. This is the single resolver all data-access
    should use so the local org switcher (and SaaS per-org backends) take effect
    everywhere. Falls back cleanly outside a request context (e.g. MCP, CLI)."""
    try:
        from flask import g
        ob = getattr(g, "org_backend", None)
        if ob is not None:
            return ob
    except RuntimeError:
        pass  # no application/request context
    return get_backend(config)


def get_local_org_backend(org: dict, config: CatalogConfig) -> DatabaseBackend:
    """Backend for a LOCAL config-defined org: clone the base DB config, override
    the database name. Preserves host/port/auth (e.g. Windows auth). SQL Server
    only for now; anything else falls back to the default backend."""
    import dataclasses
    if (org.get("backend") or "sqlserver") != "sqlserver":
        return get_backend(config)
    cache_key = f"localorg:{org.get('id', '')}:{org.get('database', '')}"
    if cache_key not in _backend_cache:
        db_config = dataclasses.replace(config.database, database=org["database"])
        _backend_cache[cache_key] = SqlServerBackend(db_config)
    return _backend_cache[cache_key]


def get_backend_for_org(org: dict, config: CatalogConfig) -> DatabaseBackend:
    """Get a database backend for a specific organization.

    Organizations can override the default backend with their own
    db_backend and db_connection_string settings.
    """
    org_backend = org.get("db_backend", "supabase")
    conn_str = org.get("db_connection_string")
    cache_key = f"org:{org.get('org_id', '')}:{org_backend}"

    if cache_key not in _backend_cache:
        if org_backend == "sqlserver" and conn_str:
            # Parse connection string into DatabaseConfig
            db_config = _parse_sqlserver_connection(conn_str)
            _backend_cache[cache_key] = SqlServerBackend(db_config)
        elif org_backend == "sqlserver":
            # Use default SQL Server config
            _backend_cache[cache_key] = SqlServerBackend(config.database)
        else:
            # Default to Supabase
            _backend_cache[cache_key] = SupabaseBackend(config.supabase)

    return _backend_cache[cache_key]


def _parse_sqlserver_connection(conn_str: str):
    """Parse a SQL Server connection string into DatabaseConfig."""
    from ade_catalog.core.config import DatabaseConfig
    import re

    parts = {}
    for part in conn_str.split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            parts[key.strip().upper()] = val.strip()

    host_port = parts.get("SERVER", "localhost,1433")
    if "," in host_port:
        host, port = host_port.rsplit(",", 1)
    else:
        host, port = host_port, "1433"

    return DatabaseConfig(
        host=host,
        port=int(port),
        database=parts.get("DATABASE", ""),
        user=parts.get("UID", parts.get("USER", "")),
        password=parts.get("PWD", parts.get("PASSWORD", "")),
        encrypt=parts.get("ENCRYPT", "yes"),
        trust_cert=parts.get("TRUSTSERVERCERTIFICATE", "yes"),
    )


def reset_backend_cache():
    """Clear the backend cache."""
    _backend_cache.clear()
