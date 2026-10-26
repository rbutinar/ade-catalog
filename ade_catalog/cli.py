"""
ade-catalog CLI — command-line interface for metadata extraction and ingestion.

Usage:
    ade-catalog auth --tenant acme.com --browser
    ade-catalog extract --tenant acme.com --all --extract-sql --output ./extraction
    ade-catalog ingest --source ./extraction --org acme --view "Acme Fabric"
    ade-catalog serve --port 5001
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="ade-catalog",
        description="Cross-platform analytics metadata catalog",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- extract ---
    p_extract = subparsers.add_parser("extract", help="Extract metadata from a platform")
    p_extract.add_argument("--tenant", required=True, help="Azure AD tenant (e.g. acme.com)")
    p_extract.add_argument("--platform", default="fabric", choices=["fabric", "databricks"],
                           help="Platform to extract from (default: fabric)")
    p_extract.add_argument("--output", required=True, help="Output directory for JSON files")
    p_extract.add_argument("--all", action="store_true", help="Extract all workspaces")
    p_extract.add_argument("--workspace", help="Extract only this workspace")
    p_extract.add_argument("--list", action="store_true", help="List workspaces only")
    p_extract.add_argument("--extract-sql", action="store_true",
                           help="Also extract SQL schemas from warehouses/lakehouses")
    p_extract.add_argument("--browser", action="store_true",
                           help="Use browser auth (required for conditional access)")
    p_extract.add_argument("--credentials", help="Path to credentials YAML file")

    # --- ingest ---
    p_ingest = subparsers.add_parser("ingest", help="Import extracted JSON into the catalog database")
    p_ingest.add_argument("--source", default="demo",
                          help="Source: 'demo' or path to extractions directory")
    p_ingest.add_argument("--view", help="View name to import into")
    p_ingest.add_argument("--org", help="Organization slug (e.g. 'acme')")

    # --- serve ---
    p_serve = subparsers.add_parser("serve", help="Start the web UI")
    p_serve.add_argument("--port", type=int, default=5001, help="Port (default: 5001)")
    p_serve.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    p_serve.add_argument("--debug", action="store_true", help="Enable debug mode")

    # --- migrate ---
    p_migrate = subparsers.add_parser("migrate", help="Migrate data from SQL Server to Supabase")
    p_migrate.add_argument("--org", help="Target organization slug")
    p_migrate.add_argument("--view", help="Filter views by name")
    p_migrate.add_argument("--all", action="store_true", help="Migrate all views")

    # --- lineage ---
    p_lineage = subparsers.add_parser("lineage", help="Build/inspect the lineage graph")
    p_lineage.add_argument("action", choices=["build", "stats"],
                           help="build: populate lineage for a view; stats: show counts")
    p_lineage.add_argument("--view-id", type=int, help="Catalog view id to build lineage for")
    p_lineage.add_argument("--view", help="Catalog view name (resolved to view_id)")
    p_lineage.add_argument("--platform", default="databricks",
                           choices=["databricks", "fabric", "powerbi",
                                    "oracle", "businessobjects", "postgresql",
                                    "cloudera", "tableau", "talend", "all"],
                           help="Platform to build lineage from (default: databricks). "
                                "oracle/businessobjects/postgresql/cloudera/tableau/talend "
                                "are experimental (TICK-003 / TICK-016 / TICK-017).")
    p_lineage.add_argument("--clear", action="store_true",
                           help="Clear this view's lineage before rebuilding (tenant-safe)")
    p_lineage.add_argument("--skip-metrics", action="store_true",
                           help="Skip per-node metrics (upstream/downstream counts+depths). "
                                "They are recomputable and non-essential for traversal; skipping "
                                "avoids the slow sp_compute_node_metrics on large graphs "
                                "(which holds locks and can block the web app). Recompute later "
                                "with 'lineage stats' tooling or a dedicated pass.")

    # --- enrich ---
    p_enrich = subparsers.add_parser("enrich", help="Generate AI descriptions for a view's objects")
    p_enrich.add_argument("--view-id", type=int, help="Catalog view id to enrich")
    p_enrich.add_argument("--view", help="Catalog view name (resolved to view_id)")
    p_enrich.add_argument("--classes", help="Comma-separated node types to enrich "
                          "(default: report,dataset,semantic_table,table,measure,column,notebook)")
    p_enrich.add_argument("--limit", type=int, help="Max objects to process")
    p_enrich.add_argument("--force", action="store_true",
                          help="Regenerate even if unchanged (never overwrites human edits)")
    p_enrich.add_argument("--dry-run", action="store_true",
                          help="Print the prompts without calling the LLM or writing")

    # --- export ---
    p_export = subparsers.add_parser("export", help="Export catalog reports to a multi-sheet Excel workbook")
    p_export.add_argument("--view-id", type=int, help="Catalog view id to export")
    p_export.add_argument("--view", help="Catalog view name (resolved to view_id)")
    p_export.add_argument("--reports", help="Comma-separated report keys "
                          "(reports,kpis,data_products); default: all")
    p_export.add_argument("--output", default="catalog_export.xlsx",
                          help="Output .xlsx path (default: catalog_export.xlsx)")

    # --- mcp-key --- (hosted /mcp authentication, TICK-018)
    p_mcpkey = subparsers.add_parser("mcp-key", help="Manage MCP API keys for the hosted /mcp endpoint")
    p_mcpkey.add_argument("action", choices=["create", "list", "revoke"],
                          help="create: mint a key (shown once); list: show keys; revoke: deactivate")
    p_mcpkey.add_argument("--org", help="Organization slug or org_id the key is scoped to (create)")
    p_mcpkey.add_argument("--user", help="User email or auth.users id the key acts as (create)")
    p_mcpkey.add_argument("--name", default="", help="Human label for the key (create)")
    p_mcpkey.add_argument("--key-id", help="key_id to deactivate (revoke)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "extract":
        _run_extract(args)
    elif args.command == "ingest":
        _run_ingest(args)
    elif args.command == "serve":
        _run_serve(args)
    elif args.command == "migrate":
        _run_migrate(args)
    elif args.command == "lineage":
        _run_lineage(args)
    elif args.command == "enrich":
        _run_enrich(args)
    elif args.command == "export":
        _run_export(args)
    elif args.command == "mcp-key":
        _run_mcp_key(args)


def _run_extract(args):
    if args.platform == "fabric":
        # Build argv for the existing extractor module
        extract_args = ["--tenant", args.tenant]
        if args.all:
            extract_args.append("--all")
        if args.workspace:
            extract_args.extend(["--workspace", args.workspace])
        if args.list:
            extract_args.append("--list")
        if args.extract_sql:
            extract_args.append("--extract-sql")
        if args.browser:
            extract_args.append("--browser")
        if args.output:
            extract_args.extend(["--output", args.output])
        if args.credentials:
            extract_args.extend(["--credentials", args.credentials])

        sys.argv = ["ade-catalog-extract"] + extract_args
        from ade_catalog.platforms.fabric.extractors import api_extractor  # noqa: F401
    else:
        print(f"Extractor for '{args.platform}' not yet implemented via CLI")
        sys.exit(1)


def _run_ingest(args):
    from ade_catalog.ingest import ingest
    ingest(source=args.source, view_name=args.view, org_slug=args.org)


def _run_serve(args):
    from ade_catalog.web import create_app
    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug)


def _run_migrate(args):
    from ade_catalog.migrate_to_supabase import migrate_all
    migrate_all(org_slug=args.org, view_filter=args.view)


def _run_lineage(args):
    from ade_catalog.core.config import load_config
    from ade_catalog.core.db_backend import get_backend
    from ade_catalog.lineage.builder import LineageGraphBuilder

    backend = get_backend(load_config())
    builder = LineageGraphBuilder(backend)

    if args.action == "stats":
        print("Lineage graph:", builder.stats())
        return

    # action == "build" — resolve the target view
    view_id = args.view_id
    if view_id is None and args.view:
        rows = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s",
            (args.view,),
        )
        if not rows:
            print(f"No view found with name '{args.view}'")
            sys.exit(1)
        view_id = rows[0]["view_id"]

    if view_id is None:
        print("lineage build requires --view-id or --view")
        sys.exit(1)

    if args.clear:
        builder.clear_view(view_id)

    platforms = ["databricks", "fabric", "powerbi"] if args.platform == "all" else [args.platform]
    for platform in platforms:
        if platform == "databricks":
            from ade_catalog.lineage.databricks import DatabricksLineageExtractor
            DatabricksLineageExtractor(builder, view_id).build()
        elif platform == "fabric":
            from ade_catalog.lineage.fabric import FabricLineageExtractor
            FabricLineageExtractor(builder, view_id).build()
        elif platform == "powerbi":
            from ade_catalog.lineage.powerbi import PowerBILineageExtractor
            PowerBILineageExtractor(builder, view_id).build()
        elif platform == "oracle":
            # experimental (TICK-003 ACT-004)
            from ade_catalog.lineage.oracle import OracleLineageExtractor
            OracleLineageExtractor(builder, view_id).build()
        elif platform == "businessobjects":
            # experimental (TICK-003 ACT-002). Build 'oracle' first so object
            # SELECTs resolve to existing Oracle nodes (else external fallback).
            from ade_catalog.lineage.businessobjects import BusinessObjectsLineageExtractor
            BusinessObjectsLineageExtractor(builder, view_id).build()
        elif platform == "postgresql":
            # experimental (TICK-016). Relational-source lineage: structure +
            # object-level (native deps) + column-level (view text via resolver).
            from ade_catalog.lineage.postgres import PostgresLineageExtractor
            PostgresLineageExtractor(builder, view_id).build()
        elif platform == "cloudera":
            # experimental (TICK-017). Hive tables + Spark jobs (static analysis):
            # table--feeds-->job (read), job--produces-->table (write).
            from ade_catalog.lineage.cloudera import ClouderaLineageExtractor
            ClouderaLineageExtractor(builder, view_id).build()
        elif platform == "tableau":
            # experimental (TICK-017). Workbook/datasource/source_table/worksheet/
            # dashboard + custom-SQL resolution; source_table -> Hive via linker.
            from ade_catalog.lineage.tableau import TableauLineageExtractor
            TableauLineageExtractor(builder, view_id).build()
        elif platform == "talend":
            # experimental (TICK-017). Jobs + tRunJob triggers + context-resolved
            # table refs; hive refs share the cloudera node convention (dedup).
            from ade_catalog.lineage.talend import TalendLineageExtractor
            TalendLineageExtractor(builder, view_id).build()
        else:
            print(f"Lineage for '{platform}' not yet implemented")
            sys.exit(1)

    # Cross-platform edges (the value beyond single-platform lineage)
    from ade_catalog.lineage.crossplatform import CrossPlatformLinker
    xedges = CrossPlatformLinker(builder, view_id).link()

    if args.skip_metrics:
        print("Skipping per-node metrics (--skip-metrics); recompute separately when needed.")
    else:
        builder.compute_metrics()
    print(f"Lineage built for view_id={view_id} ({'+'.join(platforms)}): "
          f"{builder.stats()}; cross-platform edges: {xedges}")


def _run_enrich(args):
    from ade_catalog.core.config import load_config
    from ade_catalog.core.db_backend import get_backend
    from ade_catalog.enrichment.enricher import Enricher

    config = load_config()
    backend = get_backend(config)

    view_id = args.view_id
    if view_id is None and args.view:
        rows = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s",
            (args.view,),
        )
        if not rows:
            print(f"No view found with name '{args.view}'")
            sys.exit(1)
        view_id = rows[0]["view_id"]
    if view_id is None:
        print("enrich requires --view-id or --view")
        sys.exit(1)

    if not args.dry_run and not config.enrichment.enabled:
        print("No ANTHROPIC_API_KEY configured — set it (env or config.yaml) or use --dry-run.")
        sys.exit(1)

    classes = [c.strip() for c in args.classes.split(",")] if args.classes else None
    stats = Enricher(backend, view_id, config).run(
        classes=classes, limit=args.limit, force=args.force, dry_run=args.dry_run,
    )
    print(f"Enrich (view_id={view_id}, model={config.enrichment.model}): {stats}")


def _run_export(args):
    from ade_catalog.core.config import load_config
    from ade_catalog.core.db_backend import get_backend
    from ade_catalog.export.reports import build_workbook

    backend = get_backend(load_config())

    view_id = args.view_id
    if view_id is None and args.view:
        rows = backend.query(
            "SELECT view_id FROM catalog_meta.catalog_views WHERE view_name = %s",
            (args.view,),
        )
        if not rows:
            print(f"No view found with name '{args.view}'")
            sys.exit(1)
        view_id = rows[0]["view_id"]
    if view_id is None:
        print("export requires --view-id or --view")
        sys.exit(1)

    keys = [k.strip() for k in args.reports.split(",")] if args.reports else None
    data = build_workbook(backend, view_id, keys)
    with open(args.output, "wb") as f:
        f.write(data)
    print(f"Export (view_id={view_id}) -> {args.output} ({len(data):,} bytes)")


def _run_mcp_key(args):
    """Manage MCP API keys in auth_meta.mcp_api_keys. Runs as the base postgres
    role (bypassrls), so it can read/write the key store directly (TICK-018)."""
    from ade_catalog.core.config import load_config
    from ade_catalog.core.db_backend import get_backend
    from ade_catalog.mcp_server.auth import generate_api_key

    config = load_config()
    if config.db_backend != "supabase":
        print("mcp-key requires the Supabase backend (the SaaS auth store).")
        sys.exit(1)
    backend = get_backend(config)

    if args.action == "list":
        rows = backend.query(
            "SELECT k.key_id, k.key_prefix, k.name, k.is_active, k.last_used_at, "
            "o.org_name, k.user_id "
            "FROM auth_meta.mcp_api_keys k "
            "JOIN auth_meta.organizations o ON o.org_id = k.org_id "
            "ORDER BY k.created_at DESC")
        if not rows:
            print("No MCP API keys.")
            return
        for r in rows:
            flag = "active" if r["is_active"] else "revoked"
            print(f"{r['key_id']}  {r['key_prefix']}...  [{flag}]  org={r['org_name']}  "
                  f"user={r['user_id']}  name={r['name'] or '-'}  last_used={r['last_used_at'] or 'never'}")
        return

    if args.action == "revoke":
        if not args.key_id:
            print("revoke requires --key-id")
            sys.exit(1)
        n = backend.execute(
            "UPDATE auth_meta.mcp_api_keys SET is_active = FALSE WHERE key_id = %s",
            (args.key_id,))
        print(f"Revoked {n} key(s).")
        return

    # create
    if not args.org or not args.user:
        print("create requires --org and --user")
        sys.exit(1)

    org_rows = backend.query(
        "SELECT org_id, org_name FROM auth_meta.organizations "
        "WHERE org_slug = %s OR org_id::text = %s",
        (args.org, args.org))
    if not org_rows:
        print(f"No org found for '{args.org}'")
        sys.exit(1)
    org_id = str(org_rows[0]["org_id"])
    org_name = org_rows[0]["org_name"]

    user_id = args.user
    if "@" in args.user:
        u = backend.query("SELECT id FROM auth.users WHERE email = %s", (args.user,))
        if not u:
            print(f"No auth.users row for email '{args.user}'")
            sys.exit(1)
        user_id = str(u[0]["id"])

    mem = backend.query(
        "SELECT 1 AS ok FROM auth_meta.user_org_memberships "
        "WHERE user_id = %s AND org_id = %s AND is_active = TRUE",
        (user_id, org_id))
    if not mem:
        print(f"WARNING: user {user_id} is not an active member of org '{org_name}' — "
              "RLS will return no rows for this key until a membership is added.")

    plaintext, prefix, key_hash = generate_api_key()
    backend.execute(
        "INSERT INTO auth_meta.mcp_api_keys (key_prefix, key_hash, user_id, org_id, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        (prefix, key_hash, user_id, org_id, args.name))
    print(f"MCP API key created for org='{org_name}', user={user_id}.")
    print("FULL KEY (shown once - store it now, send via X-API-Key header):")
    print(f"  {plaintext}")


if __name__ == "__main__":
    main()
