"""
ade-catalog configuration system.

Loads configuration from:
  1. Environment variables (highest priority)
  2. Instance config.yaml
  3. Defaults

Usage:
    from ade_catalog.core.config import load_config
    config = load_config()  # uses ADE_ENVIRONMENT + ADE_INSTANCE_PATH
    config = load_config(instance_path="/path/to/instance")
"""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DatabaseConfig:
    host: str = "localhost"
    port: int = 1433
    database: str = "ade_catalog_demo"
    user: str = ""
    password: str = ""
    driver: str = ""
    encrypt: str = "yes"
    trust_cert: str = "yes"
    use_windows_auth: bool = False


@dataclass
class StorageConfig:
    """Configurable storage paths. Can point to local dirs, OneDrive, NAS, etc."""
    extractions: str = "extractions"
    business_docs: str = "business_docs"
    exports: str = "exports"


@dataclass
class PlatformConfig:
    enabled: bool = False
    extraction_mode: str = "local"  # "local" (JSON files) or "live" (API)
    settings: dict = field(default_factory=dict)


@dataclass
class SupabaseConfig:
    url: str = ""
    anon_key: str = ""
    service_key: str = ""
    db_url: str = ""  # Direct PostgreSQL connection string
    jwt_secret: str = ""


@dataclass
class EnrichmentConfig:
    """AI enrichment (Phase 3). Config-driven credentials — no hardcoding.

    ``api_key`` comes from ANTHROPIC_API_KEY (env) or config.yaml. ``enabled`` is
    derived: the schema + web edit + display all work WITHOUT a key; only the
    ``enrich`` LLM pass needs one.
    """
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"  # cost-efficient bulk tier
    api_key: str = ""
    max_tokens: int = 1024

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class AssistantConfig:
    """In-app AI assistant (TICK-008). A Claude tool-use loop over the existing
    MCP tools. Config-driven — no hardcoding.

    Shares ANTHROPIC_API_KEY with enrichment. ``enabled`` is derived: the whole
    web app works WITHOUT a key; only the chat needs one (the UI degrades to a
    disabled banner). ``max_tool_iterations`` caps the agentic loop (cost guard).
    """
    provider: str = "anthropic"
    model: str = "claude-sonnet-5"  # stronger multi-tool reasoning for the loop
    api_key: str = ""
    max_tokens: int = 1024
    max_tool_iterations: int = 4  # hard cap on tool round-trips per turn (latency guard)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class CatalogConfig:
    environment: str = "demo"
    environment_name: str = "Demo Environment"
    instance_path: Path = field(default_factory=lambda: Path("."))
    product_path: Path = field(default_factory=lambda: Path("."))

    db_backend: str = "sqlserver"  # "sqlserver" | "supabase" | "sqlite"
    # SQLite backend (public self-host edition): directory holding one .sqlite file
    # per schema (ATTACHed as the schema name at connection open). Relative paths
    # resolve from data_root. Only used when db_backend == "sqlite".
    sqlite_path: str = "catalog_sqlite"
    # Local-first single-tenant mode: skips the SaaS auth layer (MCP/web).
    # Default False — SaaS/multi-tenant deployments are unaffected.
    local_mode: bool = False
    # Self-service signup. Default False = closed (secure by default); the
    # signup route is gated unless explicitly enabled.
    allow_signup: bool = False
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    supabase: SupabaseConfig = field(default_factory=SupabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    enrichment: EnrichmentConfig = field(default_factory=EnrichmentConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    # Local multi-org registry (data-plane only): a config-defined list of
    # {id, name, backend, database} used by the local org switcher. This is NOT
    # the SaaS control plane — no identity/membership/RLS. Empty by default.
    organizations: list[dict] = field(default_factory=list)

    # The directory containing config.yaml (set during load)
    data_root: Path = field(default_factory=lambda: Path("."))

    def resolve_storage_path(self, category: str) -> Path:
        """Resolve a storage path. Absolute paths used as-is, relative resolved from data_root."""
        raw = getattr(self.storage, category, category)
        path = Path(raw)
        if path.is_absolute():
            return path
        return self.data_root / path

    @property
    def extractions_dir(self) -> Path:
        return self.resolve_storage_path("extractions")

    @property
    def business_docs_dir(self) -> Path:
        return self.resolve_storage_path("business_docs")

    @property
    def exports_dir(self) -> Path:
        return self.resolve_storage_path("exports")

    @property
    def enabled_platforms(self) -> list[str]:
        return [name for name, cfg in self.platforms.items() if cfg.enabled]


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def detect_runtime() -> str:
    """Detect runtime: 'windows', 'wsl', or 'linux'."""
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "linux":
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except OSError:
            pass
        return "linux"
    return system


def _get_default_driver() -> str:
    """Get default ODBC driver for the current platform."""
    runtime = detect_runtime()
    if runtime == "windows":
        return "ODBC Driver 17 for SQL Server"
    return "ODBC Driver 18 for SQL Server"


def _get_default_host() -> str:
    """Get default SQL Server host for the current platform."""
    runtime = detect_runtime()
    if runtime == "wsl":
        try:
            import subprocess
            result = subprocess.run(["ip", "route"], capture_output=True, text=True)
            for line in result.stdout.split("\n"):
                if line.startswith("default"):
                    return line.split()[2]
        except Exception:
            return "172.26.112.1"
    return "localhost"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Load a YAML file, return empty dict if missing."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _build_database_config(yaml_cfg: dict, env_name: str) -> DatabaseConfig:
    """Build DatabaseConfig from yaml + env vars."""
    db_yaml = yaml_cfg.get("database", {})
    default_db_name = db_yaml.get("name", f"ade_catalog_{env_name}")

    use_windows_auth = os.getenv(
        "SQLSERVER_USE_WINDOWS_AUTH", ""
    ).lower() in ("true", "1", "yes")

    password = "" if use_windows_auth else os.getenv("SQLSERVER_PASSWORD", "")

    return DatabaseConfig(
        host=os.getenv("SQLSERVER_HOST") or _get_default_host(),
        port=int(os.getenv("SQLSERVER_PORT", "1433")),
        database=os.getenv("SQLSERVER_DATABASE") or default_db_name,
        user=os.getenv("SQLSERVER_USER", ""),
        password=password,
        driver=os.getenv("SQLSERVER_DRIVER") or _get_default_driver(),
        encrypt=os.getenv("SQLSERVER_ENCRYPT", "yes"),
        trust_cert=os.getenv("SQLSERVER_TRUST_CERT", "yes"),
        use_windows_auth=use_windows_auth,
    )


def _build_storage_config(yaml_cfg: dict) -> StorageConfig:
    """Build StorageConfig from yaml."""
    storage_yaml = yaml_cfg.get("storage", {})
    return StorageConfig(
        extractions=storage_yaml.get("extractions", "extractions"),
        business_docs=storage_yaml.get("business_docs", "business_docs"),
        exports=storage_yaml.get("exports", "exports"),
    )


def _build_supabase_config(yaml_cfg: dict) -> SupabaseConfig:
    """Build SupabaseConfig from yaml + env vars."""
    sb_yaml = yaml_cfg.get("supabase", {})
    return SupabaseConfig(
        url=os.getenv("SUPABASE_URL") or sb_yaml.get("url", ""),
        anon_key=os.getenv("SUPABASE_ANON_KEY") or sb_yaml.get("anon_key", ""),
        service_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or sb_yaml.get("service_key", ""),
        db_url=os.getenv("SUPABASE_DB_URL") or sb_yaml.get("db_url", ""),
        jwt_secret=os.getenv("SUPABASE_JWT_SECRET") or sb_yaml.get("jwt_secret", ""),
    )


def _build_enrichment_config(yaml_cfg: dict) -> EnrichmentConfig:
    """Build EnrichmentConfig from yaml + env vars (env wins for the secret)."""
    en_yaml = yaml_cfg.get("enrichment", {}) or {}
    return EnrichmentConfig(
        provider=en_yaml.get("provider", "anthropic"),
        model=os.getenv("CATALOG_ENRICH_MODEL") or en_yaml.get("model", "claude-haiku-4-5-20251001"),
        api_key=os.getenv("ANTHROPIC_API_KEY") or en_yaml.get("api_key", ""),
        max_tokens=int(os.getenv("CATALOG_ENRICH_MAX_TOKENS", "") or en_yaml.get("max_tokens", 1024)),
    )


def _build_assistant_config(yaml_cfg: dict) -> AssistantConfig:
    """Build AssistantConfig from yaml + env vars (env wins for the secret).

    Shares ANTHROPIC_API_KEY with enrichment; own model / token / iteration knobs.
    """
    as_yaml = yaml_cfg.get("assistant", {}) or {}
    return AssistantConfig(
        provider=as_yaml.get("provider", "anthropic"),
        model=os.getenv("CATALOG_ASSISTANT_MODEL") or as_yaml.get("model", "claude-sonnet-5"),
        api_key=os.getenv("ANTHROPIC_API_KEY") or as_yaml.get("api_key", ""),
        max_tokens=int(os.getenv("CATALOG_ASSISTANT_MAX_TOKENS", "") or as_yaml.get("max_tokens", 1024)),
        max_tool_iterations=int(
            os.getenv("CATALOG_ASSISTANT_MAX_ITERS", "") or as_yaml.get("max_tool_iterations", 6)
        ),
    )


def _build_platform_configs(yaml_cfg: dict) -> dict[str, PlatformConfig]:
    """Build platform configs from yaml."""
    platforms = {}
    for name, pcfg in yaml_cfg.get("platforms", {}).items():
        if isinstance(pcfg, dict):
            platforms[name] = PlatformConfig(
                enabled=pcfg.get("enabled", False),
                extraction_mode=pcfg.get("extraction_mode", "local"),
                settings=pcfg,
            )
    return platforms


def load_config(
    instance_path: Optional[str | Path] = None,
    environment: Optional[str] = None,
) -> CatalogConfig:
    """
    Load ade-catalog configuration.

    Args:
        instance_path: Path to instance repo (or demo/ in product repo).
                       Defaults to ADE_INSTANCE_PATH env var, then ./demo
        environment:   Environment name. Defaults to ADE_ENVIRONMENT env var, then "demo"
    """
    # Determine product root (where this code lives)
    product_path = Path(__file__).parent.parent.parent

    # Load .env from product root
    load_dotenv(product_path / ".env", override=False)

    # Resolve environment
    env_name = environment or os.getenv("ADE_ENVIRONMENT", "demo")

    # Resolve instance path
    if instance_path:
        inst_path = Path(instance_path)
    else:
        env_inst = os.getenv("ADE_INSTANCE_PATH")
        if env_inst:
            inst_path = Path(env_inst)
        else:
            inst_path = product_path / "demo"

    # Load instance config.yaml
    config_file = inst_path / "data" / "config.yaml"
    if not config_file.exists():
        # Try direct path (for demo/ which has config.yaml at root)
        config_file = inst_path / "config.yaml"

    yaml_cfg = _load_yaml(config_file)

    # Also load .env from instance path if it exists
    load_dotenv(inst_path / ".env", override=False)

    # Build environment name
    env_display = (
        yaml_cfg.get("environment", {}).get("name")
        or env_name.replace("_", " ").title()
    )

    # data_root = directory containing config.yaml (where storage paths resolve from)
    data_root = config_file.parent

    db_backend = os.getenv("CATALOG_DB_BACKEND") or yaml_cfg.get("db_backend", "sqlserver")
    sqlite_path = os.getenv("CATALOG_SQLITE_PATH") or yaml_cfg.get("sqlite_path", "catalog_sqlite")

    _truthy = ("1", "true", "yes", "on")
    local_mode = (os.getenv("CATALOG_LOCAL_MODE") or str(yaml_cfg.get("local_mode", ""))).lower() in _truthy
    allow_signup = (os.getenv("CATALOG_ALLOW_SIGNUP") or str(yaml_cfg.get("allow_signup", ""))).lower() in _truthy

    # Local org registry: env JSON (CATALOG_LOCAL_ORGS) wins over config.yaml,
    # so client DB names never need to be committed to the repo.
    organizations: list[dict] = []
    orgs_env = os.getenv("CATALOG_LOCAL_ORGS")
    if orgs_env:
        import json
        try:
            organizations = json.loads(orgs_env) or []
        except ValueError:
            organizations = []
    else:
        organizations = yaml_cfg.get("organizations", []) or []

    return CatalogConfig(
        environment=env_name,
        environment_name=env_display,
        instance_path=inst_path,
        product_path=product_path,
        data_root=data_root,
        db_backend=db_backend,
        sqlite_path=sqlite_path,
        local_mode=local_mode,
        allow_signup=allow_signup,
        database=_build_database_config(yaml_cfg, env_name),
        supabase=_build_supabase_config(yaml_cfg),
        storage=_build_storage_config(yaml_cfg),
        enrichment=_build_enrichment_config(yaml_cfg),
        assistant=_build_assistant_config(yaml_cfg),
        platforms=_build_platform_configs(yaml_cfg),
        organizations=organizations,
    )
