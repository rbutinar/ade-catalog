"""Authentication routes: login, signup, logout."""

from flask import (
    Blueprint, current_app, flash, redirect, render_template,
    request, session, url_for,
)

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _get_supabase():
    """Get Supabase client from app config."""
    from ade_catalog.core.auth import get_supabase_client
    return get_supabase_client(current_app.config["CATALOG"])


@bp.route("/welcome")
def landing():
    """Public product landing page. The unauthenticated entry point (the
    before_request gate sends anonymous visitors of ``/`` here); a logged-in
    user is bounced straight into the app."""
    if session.get("user_id"):
        return redirect(url_for("home.index"))
    return render_template("landing.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("auth/login.html", next=request.args.get("next", "/"))

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    next_url = request.form.get("next", "/")

    if not email or not password:
        flash("Email and password are required.", "error")
        return render_template("auth/login.html", next=next_url, email=email)

    # Step 1: Authenticate with Supabase
    try:
        sb = _get_supabase()
        result = sb.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        error_msg = str(e)
        if "Invalid login credentials" in error_msg:
            flash("Invalid email or password.", "error")
        else:
            flash(f"Auth error: {error_msg}", "error")
        return render_template("auth/login.html", next=next_url, email=email)

    session["user_id"] = result.user.id
    session["user_email"] = result.user.email
    session["access_token"] = result.session.access_token
    session["refresh_token"] = result.session.refresh_token

    # Check super admin from user metadata
    metadata = result.user.user_metadata or {}
    session["is_super_admin"] = metadata.get("role") == "super_admin"

    # Step 2: Load org membership (non-blocking — login succeeds even if DB fails)
    try:
        config = current_app.config["CATALOG"]
        from ade_catalog.core.auth import get_or_create_personal_org
        org = get_or_create_personal_org(config, result.user.id, email)
        if org:
            if session.get("is_super_admin"):
                # Super-admin starts on "All organizations" (sees everything);
                # can then narrow to a single org via the switcher.
                session["active_org_id"] = "__all__"
                session["active_org_name"] = "All organizations"
                session["user_role"] = "owner"
            else:
                session["active_org_id"] = str(org["org_id"])
                session["active_org_name"] = org["org_name"]
                session["user_role"] = org.get("role", "viewer")
    except Exception as e:
        # DB connection issue — login still succeeds, org features degraded
        flash(f"Warning: could not load organization ({e})", "warning")

    return redirect(next_url)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    # Self-service signup is closed by default (secure). Enable explicitly via
    # CATALOG_ALLOW_SIGNUP. Defends in depth even if Supabase signups are on.
    if not current_app.config["CATALOG"].allow_signup:
        flash("Self-service signup is disabled. Contact your administrator for access.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("auth/signup.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not email or not password:
        flash("Email and password are required.", "error")
        return render_template("auth/signup.html", email=email)

    if password != confirm:
        flash("Passwords do not match.", "error")
        return render_template("auth/signup.html", email=email)

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return render_template("auth/signup.html", email=email)

    try:
        sb = _get_supabase()
        result = sb.auth.sign_up({"email": email, "password": password})

        if result.user and result.session:
            # Auto-login after signup
            session["user_id"] = result.user.id
            session["user_email"] = result.user.email
            session["access_token"] = result.session.access_token
            session["refresh_token"] = result.session.refresh_token

            config = current_app.config["CATALOG"]
            from ade_catalog.core.auth import get_or_create_personal_org
            org = get_or_create_personal_org(config, result.user.id, email)
            if org:
                session["active_org_id"] = str(org["org_id"])
                session["active_org_name"] = org["org_name"]

            return redirect("/")
        else:
            flash("Check your email to confirm your account.", "info")
            return redirect(url_for("auth.login"))

    except Exception as e:
        flash(f"Signup failed: {e}", "error")
        return render_template("auth/signup.html", email=email)


@bp.route("/logout")
def logout():
    try:
        sb = _get_supabase()
        sb.auth.sign_out()
    except Exception:
        pass

    session.clear()
    return redirect(url_for("auth.login"))
