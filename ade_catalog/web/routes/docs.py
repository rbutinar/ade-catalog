"""Documentation and how-to pages."""

from flask import Blueprint, render_template

bp = Blueprint("docs", __name__, url_prefix="/docs")


# --- User docs (visible to all) ---

@bp.route("/")
def index():
    return render_template("docs/index.html")


@bp.route("/getting-started")
def getting_started():
    return render_template("docs/getting_started.html")


@bp.route("/using-catalog")
def using_catalog():
    return render_template("docs/using_catalog.html")


@bp.route("/using-lineage")
def using_lineage():
    return render_template("docs/using_lineage.html")


@bp.route("/using-assistant")
def using_assistant():
    return render_template("docs/using_assistant.html")


# --- Admin docs (visible via UI only to admin, but not enforced server-side) ---

@bp.route("/onboarding")
def onboarding():
    return render_template("docs/onboarding.html")


@bp.route("/architecture")
def architecture():
    return render_template("docs/architecture.html")


@bp.route("/mcp-guide")
def mcp_guide():
    return render_template("docs/mcp_guide.html")
