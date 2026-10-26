"""
Centralized database connection management for ade-catalog.

All database access goes through this module. No other module should
build connection strings or create engines directly.

Usage:
    from ade_catalog.core.config import load_config
    from ade_catalog.core.database import get_engine, get_connection

    config = load_config()
    engine = get_engine(config.database)
    conn = get_connection(config.database)
"""

import logging
from typing import Optional
from urllib.parse import quote_plus

try:
    import pyodbc
except ImportError:
    pyodbc = None

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Engine
except ImportError:
    create_engine = None
    Engine = None

from ade_catalog.core.config import DatabaseConfig

logger = logging.getLogger(__name__)

# Engine cache: keyed by (host, database)
_engine_cache: dict[tuple[str, str], Engine] = {}


def _build_connection_string(db: DatabaseConfig) -> str:
    """Build ODBC connection string from config."""
    parts = [
        f"DRIVER={{{db.driver}}}",
        f"SERVER={db.host},{db.port}",
        f"DATABASE={db.database}",
    ]

    if db.use_windows_auth:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={db.user}")
        parts.append(f"PWD={db.password}")

    parts.append(f"Encrypt={db.encrypt}")
    parts.append(f"TrustServerCertificate={db.trust_cert}")
    parts.append("Connection Timeout=30")
    parts.append("Command Timeout=60")

    return ";".join(parts)


def get_engine(db: DatabaseConfig) -> Engine:
    """
    Get a cached SQLAlchemy engine for the given database config.

    Uses connection pooling with sensible defaults.
    """
    cache_key = (db.host, db.database)

    if cache_key in _engine_cache:
        return _engine_cache[cache_key]

    conn_str = _build_connection_string(db)

    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}",
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

    _engine_cache[cache_key] = engine
    logger.info("Created database engine for %s/%s", db.host, db.database)
    return engine


def get_connection(db: DatabaseConfig):
    """
    Get a raw pyodbc connection for the given database config.

    Use this for bulk imports and schema deployment where SQLAlchemy
    overhead is not needed. Caller is responsible for closing.
    """
    conn_str = _build_connection_string(db)
    return pyodbc.connect(conn_str, autocommit=False)


def test_connection(db: DatabaseConfig) -> tuple[bool, str]:
    """
    Test database connectivity. Returns (success, message).
    """
    try:
        engine = get_engine(db)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            result.fetchone()
        return True, f"Connected to {db.host}/{db.database}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def ensure_database_exists(db: DatabaseConfig) -> tuple[bool, str]:
    """
    Check if the target database exists, create it if not.

    Connects to 'master' to check/create, then switches to target.
    Returns (created, message).
    """
    master_config = DatabaseConfig(
        host=db.host,
        port=db.port,
        database="master",
        user=db.user,
        password=db.password,
        driver=db.driver,
        encrypt=db.encrypt,
        trust_cert=db.trust_cert,
        use_windows_auth=db.use_windows_auth,
    )

    try:
        conn = get_connection(master_config)
        conn.autocommit = True
        cursor = conn.cursor()

        # Check if database exists
        cursor.execute(
            "SELECT DB_ID(?)", (db.database,)
        )
        row = cursor.fetchone()

        if row and row[0] is not None:
            cursor.close()
            conn.close()
            return False, f"Database '{db.database}' already exists"

        # Create database
        cursor.execute(f"CREATE DATABASE [{db.database}]")
        cursor.close()
        conn.close()
        return True, f"Created database '{db.database}'"

    except Exception as e:
        return False, f"Database check/creation failed: {e}"


def reset_engine_cache():
    """Clear the engine cache. Useful when switching environments."""
    for engine in _engine_cache.values():
        engine.dispose()
    _engine_cache.clear()
