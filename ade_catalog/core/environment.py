"""Classify a Fabric workspace into an environment
(prod/test/dev/self-service/personal-copy) from its name. This is a lightweight,
deterministic classification used for the Catalog *Environment filter* — a filter
preset over the single estate view, NOT a materialized view. Rationale: PROD Fabric
workspaces are Viewer-only, so their real content lives in the readable Member
copies; "prod-only" can only be a tag/filter over copy-sourced content, never a
separate scan. See docs/pm + project memory (project_views_vs_filters).

Naming conventions observed at Acme (ALL are used):
    ACME-BI-Logistics            -> prod           (no marker)
    ACME-BI-Logistics[Test]      -> test           (bracket suffix)
    ACME-BI-Operations[Dev]      -> dev
    ACME-BI-Operations[C0001234] -> personal-copy  (per-user code)
    ACME-G-TST-Telemetry         -> test           (infix -TST-/-DEV-, no bracket)
    ACME-G-PRD-Telemetry         -> prod           (explicit infix -PRD-/-PROD-)
    ACME-G-SELF-SALES-FS         -> self-service   (infix -SELF-, user sandboxes)
"""

from __future__ import annotations

import re

# Ordered for stable dropdown display (prod first; the two "personal" buckets last).
ENVIRONMENTS = ["prod", "test", "dev", "self-service", "personal-copy"]

ENV_LABELS = {
    "prod": "Production",
    "test": "Test",
    "dev": "Dev",
    "self-service": "Self-service",
    "personal-copy": "Personal copy",
}

# A bracketed user/code suffix like [C0001234] or [12345].
_PERSONAL_RE = re.compile(r"\[[A-Za-z]?\d{3,}[^\]]*\]")

# Infix env markers (no brackets), e.g. ACME-G-TST-... / ACME-G-DEV-... / ACME-G-SELF-... /
# ACME-G-PRD-... . Anchored on hyphens so a marker inside a real word (e.g. "Development
# Costs", "Self Service Portal") is not matched.
_TEST_INFIX_RE = re.compile(r"-(?:tst|test)-")
_DEV_INFIX_RE = re.compile(r"-dev-")
_SELF_INFIX_RE = re.compile(r"-self-")
_PROD_INFIX_RE = re.compile(r"-(?:prd|prod)-")


def classify_environment(name: str | None) -> str:
    """Return the environment bucket for a workspace name.

    Handles the Acme conventions: bracket suffixes (``[Test]``/``[Dev]``/
    ``[Self]``/``[C####]``) and hyphen-delimited infixes
    (``-TST-``/``-DEV-``/``-SELF-``/``-PRD-``). ``-SELF-`` = self-service user
    sandboxes (their own bucket, not the governed estate). An explicit ``-PRD-``
    marker forces ``prod`` even past the generic bracket fallback. Anything with no
    non-prod marker is ``prod``."""
    if not name:
        return "prod"
    low = name.lower()
    if "[test]" in low or _TEST_INFIX_RE.search(low):
        return "test"
    if "[dev]" in low or _DEV_INFIX_RE.search(low):
        return "dev"
    if "[self]" in low or _SELF_INFIX_RE.search(low):
        return "self-service"
    if _PERSONAL_RE.search(name):
        return "personal-copy"
    # An explicit prod marker wins over the generic "any bracket -> copy" fallback.
    if _PROD_INFIX_RE.search(low):
        return "prod"
    # Any other bracketed variant is a non-prod copy, not the canonical prod ws.
    if "[" in name and "]" in name:
        return "personal-copy"
    return "prod"


def env_label(env: str) -> str:
    return ENV_LABELS.get(env, env.replace("-", " ").title())
