"""
ade-catalog Flask application factory.

Usage:
    flask --app ade_catalog.web run
    flask --app ade_catalog.web run --debug --port 5001
"""

import os

from flask import Flask, g, redirect, request, session, url_for

from ade_catalog.core.config import load_config
from ade_catalog.core.views import get_all_views, get_default_view_id


def get_data_backend():
    """Get the database backend for data queries.

    Returns the org-specific backend if available (e.g. SQL Server for Acme),
    otherwise the default backend (Supabase).
    """
    from flask import g, current_app
    if g.get("org_backend"):
        return g.org_backend
    from ade_catalog.core.db_backend import get_backend
    return get_backend(current_app.config["CATALOG"])


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-key-change-in-production")

    # Load catalog config and store on app
    config = load_config()
    app.config["CATALOG"] = config

    # Auth (Supabase login + multi-tenant) is active only for a hosted SaaS
    # deployment: requires the Supabase backend AND not local mode.
    # local_mode is the explicit off-switch (works for local Postgres too),
    # decoupling the auth decision from the backend type.
    use_auth = (config.db_backend == "supabase") and not config.local_mode

    @app.context_processor
    def _inject_flags():
        # Expose local_mode to templates (gates the local-operator /publish nav link).
        return {"local_mode": getattr(config, "local_mode", False)}

    @app.teardown_appcontext
    def _close_supabase_conn(exc=None):
        """Close the per-request Supabase connection that SupabaseBackend reused
        across all queries of this request (opened lazily on ``g``)."""
        conn = g.pop("_supabase_conn", None)
        if conn is not None:
            try:
                if not conn.closed:
                    conn.close()
            except Exception:
                pass

    @app.before_request
    def load_context():
        """Load auth context and view context."""
        # Skip auth for auth routes and static files
        if request.endpoint and (
            request.endpoint.startswith("auth.")
            or request.endpoint.startswith("mcp.")
            or request.endpoint == "static"
        ):
            return

        # Auth enforcement (Supabase mode only). Anonymous visitors of the root
        # land on the public product page; deep links go to login (with a
        # `next` so they resume where they intended after signing in).
        if use_auth and not session.get("user_id"):
            if request.path == "/":
                return redirect(url_for("auth.landing"))
            return redirect(url_for("auth.login", next=request.path))

        # Set user context
        g.user_id = session.get("user_id")
        g.user_email = session.get("user_email")
        g.org_id = session.get("active_org_id")
        g.org_name = session.get("active_org_name")
        g.org_backend = None
        g.org_details = None
        g.orgs = []

        # View context
        view_param = request.args.get("view_id", type=int)
        if view_param:
            session["view_id"] = view_param

        try:
            is_super = session.get("is_super_admin", False)
            g.org_backend = None

            # RLS identity (TICK-015): scope a non-super authenticated user at the
            # DB layer. Set BEFORE the first query below so SupabaseBackend switches
            # its per-request connection to the `authenticated` role + jwt claim on
            # first use — auth.uid() then resolves and RLS binds, including for the
            # membership/view queries in this hook. Super-admin, local mode, and CLI
            # stay on the bypassrls postgres role (g.db_auth_uid = None).
            g.db_auth_uid = g.user_id if (use_auth and g.user_id and not is_super) else None

            # LOCAL multi-org switcher (config-defined, data-plane only): pick the
            # active org from the config registry and repoint the data backend.
            # No identity/membership/RLS — that control plane stays SaaS-only.
            if config.local_mode and getattr(config, "organizations", None):
                from ade_catalog.core.db_backend import get_local_org_backend
                orgs = config.organizations
                g.orgs = orgs
                active = next(
                    (o for o in orgs if str(o.get("id")) == str(session.get("active_org_id"))),
                    None,
                ) or orgs[0]
                session["active_org_id"] = str(active.get("id"))
                g.org_id = str(active.get("id"))
                g.org_name = active.get("name")
                try:
                    g.org_backend = get_local_org_backend(active, config)
                except Exception:
                    g.org_backend = None
            else:
                # SaaS multi-org: populate the orgs the user can switch between.
                from ade_catalog.core.db_backend import get_backend
                main_backend = get_backend(config)
                try:
                    if is_super:
                        rows = main_backend.query(
                            "SELECT org_id, org_name FROM auth_meta.organizations "
                            "WHERE is_active = TRUE ORDER BY org_name")
                        g.orgs = ([{"id": "__all__", "name": "All organizations"}]
                                  + [{"id": str(r["org_id"]), "name": r["org_name"]} for r in rows])
                    else:
                        rows = main_backend.query(
                            "SELECT o.org_id, o.org_name FROM auth_meta.organizations o "
                            "JOIN auth_meta.user_org_memberships m ON m.org_id = o.org_id "
                            "WHERE m.user_id = %s AND m.is_active = TRUE AND o.is_active = TRUE "
                            "ORDER BY o.org_name", (g.user_id,))
                        g.orgs = [{"id": str(r["org_id"]), "name": r["org_name"]} for r in rows]
                except Exception:
                    g.orgs = []

                # Resolve the active org: keep the session choice if still valid;
                # super defaults to "__all__" (see everything), users to their first org.
                valid_ids = {o["id"] for o in g.orgs}
                active = session.get("active_org_id")
                if active not in valid_ids:
                    active = "__all__" if is_super else (g.orgs[0]["id"] if g.orgs else None)
                session["active_org_id"] = active
                g.org_id = active
                g.org_name = next((o["name"] for o in g.orgs if o["id"] == active), g.get("org_name"))
                session["active_org_name"] = g.org_name

                # Load org details (backend routing) only for a concrete org.
                if active and active != "__all__":
                    try:
                        org_rows = main_backend.query(
                            "SELECT org_id, org_name, db_backend, db_connection_string FROM auth_meta.organizations WHERE org_id = %s",
                            (active,)
                        )
                        if org_rows:
                            g.org_details = org_rows[0]
                            # If org uses a different backend, set it for data queries
                            if org_rows[0].get("db_backend") == "sqlserver" and org_rows[0].get("db_connection_string"):
                                from ade_catalog.core.db_backend import get_backend_for_org, _parse_sqlserver_connection
                                g.org_backend = get_backend_for_org(org_rows[0], config)
                                # Store org-specific DB config for queries.py
                                g.org_db_config = _parse_sqlserver_connection(org_rows[0]["db_connection_string"])
                    except Exception:
                        pass

            # "__all__" (super) sees everything; otherwise scope to the active org.
            filter_org = None if g.get("org_id") in (None, "__all__") else g.get("org_id")

            # Use org-specific backend for view queries if available
            if g.get("org_backend"):
                # Read views from org's own database
                try:
                    g.views = g.org_backend.query("""
                        SELECT view_id, view_name, description, view_type,
                               is_default, created_at, created_by
                        FROM catalog_meta.catalog_views
                        WHERE is_active = 1
                        ORDER BY is_default DESC, view_name
                    """)
                    view_param = request.args.get("view_id", type=int)
                    if view_param:
                        session["view_id"] = view_param
                    g.view_id = session.get("view_id") or (g.views[0]["view_id"] if g.views else 1)
                except Exception:
                    g.views = []
                    g.view_id = session.get("view_id") or 1
            else:
                g.view_id = session.get("view_id") or get_default_view_id(config, org_id=filter_org)
                g.views = get_all_views(config, org_id=filter_org)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("DB error in before_request: %s", e)
            g.view_id = session.get("view_id") or 1
            g.views = []

    # Register blueprints. Resilient by design: a route module absent from this
    # edition (the public edition drops assistant / export / publish / admin /
    # data_product / enrich) is skipped, while a PRESENT route whose own import
    # chain is broken still fails loudly — only the route's own absence is
    # tolerated, not a transitive ModuleNotFoundError. Order is preserved.
    import importlib
    _route_modules = (
        "auth", "home", "catalog", "detail", "data_product", "enrich",
        "assistant", "export", "lineage", "admin", "mcp", "docs", "publish",
    )
    for _name in _route_modules:
        _full = f"ade_catalog.web.routes.{_name}"
        try:
            _mod = importlib.import_module(_full)
        except ModuleNotFoundError as e:
            if e.name == _full or (e.name and _full.startswith(e.name)):
                continue  # route not shipped in this edition
            raise  # a shipped route with a broken dependency must surface
        app.register_blueprint(_mod.bp)

    return app
