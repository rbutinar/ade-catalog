"""Scan completeness manifest for catalog views (TICK-021).

A catalog view is not just a saved filter — it is a *scoped collection* that
declares its own boundaries. When we collect a deliberately partial slice of a
large estate (e.g. a high-level inventory of a subset of Acme's ~738 prod
workspaces), the resulting graph is partial *by choice*. The manifest records
that choice on the view row so a partial graph reads as partial-by-design rather
than being mistaken for incomplete or broken.

Two dimensions the environment filter (c2ec153) did not cover:

- **depth** — how deep we collected, as a TIER: inventory (what exists) →
  structural (containment) → lineage (edges) → column (column-level). The answer
  to "grandi moli di dati" is: collect broad+shallow by default, deepen on demand
  for the perimeter that matters.
- **completeness** — and crucially its *cause*: ``partial_by_design`` (we stopped
  on purpose) vs ``partial_incomplete`` (access/failure, e.g. a PROD workspace we
  can only read as Viewer) vs ``complete``.

The manifest lives as columns on ``catalog_meta.catalog_views`` (minimal, additive).
It is the first scan-run descriptor; when scheduling lands it evolves into a
``scan_runs`` table the view points at (not built yet).

The functions take a ``backend`` exposing ``.query(sql, params)`` and
``.execute(sql, params)`` (Postgres-style ``%s`` params; the SQL Server backend
translates). No global state — safe across orgs/tenants.
"""

from __future__ import annotations

from datetime import datetime

# Collection depth, shallow -> deep. Each tier is cheaper+broader than the next.
DEPTHS = ("inventory", "structural", "lineage", "column")

# Completeness, and the CAUSE is the point of the whole manifest.
COMPLETE = "complete"
PARTIAL_BY_DESIGN = "partial_by_design"
PARTIAL_INCOMPLETE = "partial_incomplete"
COMPLETENESS = (COMPLETE, PARTIAL_BY_DESIGN, PARTIAL_INCOMPLETE)

# The manifest columns, in one place (read/write both use it).
FIELDS = (
    "scan_depth",
    "completeness",
    "scope_unit",
    "scope_included",
    "scope_known",
    "perimeter_note",
    "scanned_at",
)

_DEPTH_LABEL = {
    "inventory": "Inventory",
    "structural": "Structural",
    "lineage": "Lineage",
    "column": "Column-level",
}
_COMPLETENESS_LABEL = {
    COMPLETE: "complete",
    PARTIAL_BY_DESIGN: "partial by design",
    PARTIAL_INCOMPLETE: "partial (incomplete)",
}


def read_manifest(backend, view_id: int) -> dict | None:
    """Return the manifest for a view as a plain dict, or ``None`` if the view is
    absent. Legacy views (pre-TICK-021 columns all NULL) return a dict with NULL
    fields — ``is_declared`` distinguishes them."""
    cols = ", ".join(FIELDS)
    try:
        rows = backend.query(
            f"SELECT view_id, view_name, {cols} "
            "FROM catalog_meta.catalog_views WHERE view_id = %s",
            (view_id,),
        )
    except Exception:
        # Columns not deployed yet (DB predates the migration) — treat as undeclared.
        return None
    if not rows:
        return None
    m = dict(rows[0])
    m["is_declared"] = m.get("completeness") is not None
    m["is_partial"] = m.get("completeness") in (PARTIAL_BY_DESIGN, PARTIAL_INCOMPLETE)
    m["label"] = describe(m)  # self-contained (MCP, standalone)
    m["detail"] = describe(m, include_completeness=False)  # for a UI that states the cause itself
    return m


def write_manifest(
    backend,
    view_id: int,
    *,
    scan_depth: str | None = None,
    completeness: str | None = None,
    scope_unit: str | None = None,
    scope_included: int | None = None,
    scope_known: int | None = None,
    perimeter_note: str | None = None,
    scanned_at: datetime | None = "__now__",
) -> int:
    """Set the manifest on a view. Only the passed fields are written (the rest are
    left untouched), except ``scanned_at`` which defaults to now — pass
    ``scanned_at=None`` explicitly to leave it. Returns affected row count.

    Validates the two enumerated fields against DEPTHS / COMPLETENESS so a typo
    fails here rather than tripping the DB CHECK with an opaque error.
    """
    if scan_depth is not None and scan_depth not in DEPTHS:
        raise ValueError(f"scan_depth must be one of {DEPTHS}, got {scan_depth!r}")
    if completeness is not None and completeness not in COMPLETENESS:
        raise ValueError(f"completeness must be one of {COMPLETENESS}, got {completeness!r}")

    updates: dict = {}
    if scan_depth is not None:
        updates["scan_depth"] = scan_depth
    if completeness is not None:
        updates["completeness"] = completeness
    if scope_unit is not None:
        updates["scope_unit"] = scope_unit
    if scope_included is not None:
        updates["scope_included"] = scope_included
    if scope_known is not None:
        updates["scope_known"] = scope_known
    if perimeter_note is not None:
        updates["perimeter_note"] = perimeter_note
    if scanned_at == "__now__":
        updates["scanned_at"] = datetime.now()
    elif scanned_at is not None:
        updates["scanned_at"] = scanned_at

    if not updates:
        return 0

    set_clause = ", ".join(f"{col} = %s" for col in updates)
    params = tuple(updates.values()) + (view_id,)
    return backend.execute(
        f"UPDATE catalog_meta.catalog_views SET {set_clause} WHERE view_id = %s",
        params,
    )


def describe(manifest: dict | None, include_completeness: bool = True) -> str:
    """One-line human label for the MCP payload / UI, e.g.
    ``"Inventory · partial by design · 12/738 workspaces · as of 2026-07-04"``.
    Pass ``include_completeness=False`` where the surface already states the cause
    (e.g. a banner heading) to avoid repeating "partial by design".
    Returns ``""`` for an undeclared/complete manifest (nothing to warn about)."""
    if not manifest or manifest.get("completeness") is None:
        return ""
    parts: list[str] = []
    depth = manifest.get("scan_depth")
    if depth:
        parts.append(_DEPTH_LABEL.get(depth, depth))
    if include_completeness:
        parts.append(_COMPLETENESS_LABEL.get(manifest["completeness"], manifest["completeness"]))

    included = manifest.get("scope_included")
    known = manifest.get("scope_known")
    unit = manifest.get("scope_unit") or "items"
    if included is not None and known:
        parts.append(f"{included}/{known} {unit}")
    elif included is not None:
        parts.append(f"{included} {unit}")

    scanned = manifest.get("scanned_at")
    if scanned:
        # scanned_at may be a datetime (SQL Server) or an ISO string (Postgres/JSON).
        if isinstance(scanned, datetime):
            scanned = scanned.strftime("%Y-%m-%d")
        else:
            scanned = str(scanned)[:10]
        parts.append(f"as of {scanned}")
    return " · ".join(parts)
