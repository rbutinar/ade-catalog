"""Dashboard / home page."""

from flask import Blueprint, current_app, g, redirect, request, session, render_template

from ade_catalog.core.queries import (
    get_dashboard_stats,
    get_fabric_stats,
    get_measures_by_dataset,
    get_notebooks_by_layer,
)

bp = Blueprint("home", __name__)


@bp.app_context_processor
def inject_view_manifest():
    """Expose the active view's scan completeness manifest (TICK-021) to every
    template so base.html can show a "partial by choice" banner. Fully defensive:
    any failure (no active view, backend down, pre-manifest DB) yields no banner."""
    try:
        view_id = g.get("view_id")
        if view_id is None:
            return {"view_manifest": None}
        from ade_catalog.core.db_backend import get_active_backend
        from ade_catalog.core import scan_manifest

        backend = get_active_backend(current_app.config["CATALOG"])
        manifest = scan_manifest.read_manifest(backend, view_id)
    except Exception:
        manifest = None
    return {"view_manifest": manifest}


@bp.route("/switch-org")
def switch_org():
    """Set the active organization. Local mode validates against the config
    registry; SaaS validates against the orgs the user can access (g.orgs,
    populated in before_request; includes '__all__' for super-admins). Resets
    the view since views are per-org."""
    config = current_app.config["CATALOG"]
    org = request.args.get("org")
    valid = {str(o.get("id")) for o in (getattr(config, "organizations", None) or [])}
    valid |= {str(o.get("id")) for o in (g.get("orgs") or [])}
    if org in valid:
        session["active_org_id"] = org
        session.pop("view_id", None)
        session.pop("active_org_name", None)
    return redirect(request.referrer or "/")


@bp.route("/")
def index():
    config = current_app.config["CATALOG"]
    view_id = g.view_id

    stats = {}
    notebooks_by_layer = []
    measures_by_dataset = []

    try:
        stats = get_dashboard_stats(config, view_id=view_id)
        try:
            fabric_stats = get_fabric_stats(config, view_id=view_id)
            stats.update(fabric_stats)
        except Exception:
            pass
        notebooks_by_layer = get_notebooks_by_layer(config, view_id=view_id)
        measures_by_dataset = get_measures_by_dataset(config, view_id=view_id)
    except Exception:
        pass

    return render_template(
        "home.html",
        config=config,
        stats=stats,
        notebooks_by_layer=notebooks_by_layer,
        measures_by_dataset=measures_by_dataset,
    )
