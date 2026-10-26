"""
Shared SQL column-level resolver (ACT-003 of TICK-003).

Turns a SELECT (or a SELECT-list expression) into qualified column references
of the form SCHEMA.TABLE.COLUMN. Built ONCE and reused TWICE
(decision 2026-06-29):

  - SAP BO universe objects: an object's SELECT (e.g. ``Customer.cust_name`` or
    ``sum(Shop_facts.amount_sold)``, possibly wrapped in @functions) -> the
    Oracle columns it reads.
  - Oracle views / materialized views: ``ALL_VIEWS.TEXT`` -> the base-table
    columns the view derives from. (On 12c+ the caller can first expand nested
    views with ``DBMS_UTILITY.EXPAND_SQL_TEXT`` before handing the text here.)

Confidence model (per ticket):
  - deterministic single-table / aliased references            -> "high"
  - @Aggregate_Aware (1->N), derived tables, contexts,
    unresolved aliases, BO object indirection (@Select)         -> "medium"
  - extraction_method is always "code_parsing".
The resolver NEVER blocks: it emits a ColumnRef for every reference it finds and
tags the uncertain ones rather than dropping them, so the lineage edge is always
produced (with its confidence) and the uncertainty is auditable.

EXPERIMENTAL first cut (TICK-003 ACT-003): resolves the common deterministic
cases and FLAGS the hard ones. It does not yet fully expand every BO @function
or Oracle construct — hard-case TODOs are marked inline. Testable offline against
eFashion object SELECTs and Oracle SH view TEXT once those demo envs exist
(TICK-003 ACT-006); also unit-testable directly on SQL strings, no DB required.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import sqlparse
from sqlparse.tokens import DML, Keyword

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BO @function patterns (no-ops for plain Oracle SQL).
# ---------------------------------------------------------------------------
# @Select(Class\Object) / @Where / @Dimension -> reference to ANOTHER bo object.
_RE_AT_SELECT = re.compile(r"@(?:Select|Where|Dimension|Measure)\s*\(([^)]*)\)", re.IGNORECASE)
# @Aggregate_Aware(e1, e2, ...) -> N candidate expressions (most->least aggregated).
_RE_AT_AGG = re.compile(r"@Aggregate_Aware\s*\((.*)\)", re.IGNORECASE | re.DOTALL)
# @Prompt(...) / @Variable(...) -> runtime / param values, not column lineage.
_RE_AT_RUNTIME = re.compile(r"@(?:Prompt|Variable)\s*\([^)]*\)", re.IGNORECASE)

# schema.table.column | table.column  (Oracle idents may contain $ and #).
_RE_QUALIFIED = re.compile(
    r'(?:"?([A-Za-z_][\w$#]*)"?\.)?'   # optional schema
    r'"?([A-Za-z_][\w$#]*)"?\.'        # table / alias
    r'"?([A-Za-z_][\w$#]*)"?'          # column
)

# FROM/JOIN target:  schema.table [AS] alias   |   table [AS] alias   |   table
_RE_FROM_ITEM = re.compile(
    r'(?:from|join)\s+'
    r'(?:"?(?P<schema>[A-Za-z_][\w$#]*)"?\.)?'
    r'"?(?P<table>[A-Za-z_][\w$#]*)"?'
    r'(?:\s+(?:as\s+)?"?(?P<alias>[A-Za-z_][\w$#]*)"?)?',
    re.IGNORECASE,
)

# SQL keywords that must never be mistaken for an alias after a table name.
_NON_ALIAS = {
    "on", "where", "group", "order", "having", "join", "inner", "left", "right",
    "full", "outer", "cross", "union", "connect", "start", "with", "and", "or",
}


@dataclass
class ColumnRef:
    """One resolved column reference produced by the resolver."""
    column: str
    table: Optional[str] = None
    schema: Optional[str] = None
    confidence: str = "high"
    extraction_method: str = "code_parsing"
    note: Optional[str] = None

    @property
    def qualified(self) -> str:
        parts = [p for p in (self.schema, self.table, self.column) if p]
        return ".".join(parts)


@dataclass
class ResolveResult:
    """Outcome of resolving one SELECT / expression."""
    refs: list[ColumnRef] = field(default_factory=list)
    # @Select(...) targets we could not resolve to columns here (need the bo
    # object catalog to dereference) — caller may resolve them object-to-object.
    object_refs: list[str] = field(default_factory=list)
    # True if any uncertainty was introduced (aggregate-aware, derived, etc.).
    uncertain: bool = False


# ---------------------------------------------------------------------------
# Alias map (FROM / JOIN)
# ---------------------------------------------------------------------------

def _build_alias_map(sql: str) -> dict[str, tuple[Optional[str], str]]:
    """Map alias (and bare table name) -> (schema, table) from FROM/JOIN items.

    Best-effort and dialect-tolerant. Derived tables / subqueries in FROM are
    not aliased here (they surface as unresolved -> medium confidence).
    """
    alias_map: dict[str, tuple[Optional[str], str]] = {}
    for m in _RE_FROM_ITEM.finditer(sql or ""):
        schema = m.group("schema")
        table = m.group("table")
        alias = m.group("alias")
        if not table or table.lower() in _NON_ALIAS:
            continue
        # the table is always reachable by its own bare name
        alias_map.setdefault(table.lower(), (schema, table))
        if alias and alias.lower() not in _NON_ALIAS:
            alias_map[alias.lower()] = (schema, table)
    return alias_map


def _looks_like_select(sql: str) -> bool:
    return bool(re.search(r"\bselect\b", sql or "", re.IGNORECASE) and
                re.search(r"\bfrom\b", sql or "", re.IGNORECASE))


def _select_list(sql: str) -> str:
    """Return the text between SELECT and FROM (top level), best-effort."""
    try:
        stmt = sqlparse.parse(sql)[0]
    except Exception:
        return sql
    seen_select = False
    chunks: list[str] = []
    for tok in stmt.flatten():
        if tok.ttype is DML and tok.value.upper() == "SELECT":
            seen_select = True
            continue
        if seen_select and tok.ttype is Keyword and tok.value.upper() == "FROM":
            break
        if seen_select:
            chunks.append(tok.value)
    text = "".join(chunks).strip()
    return text or sql


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------

def _refs_from_expression(
    expr: str,
    alias_map: dict[str, tuple[Optional[str], str]],
    default_schema: Optional[str],
    base_confidence: str,
    note: Optional[str],
    result: ResolveResult,
) -> None:
    """Extract qualified column refs from one expression fragment into result."""
    # 1) BO object indirection: record but do not invent columns for it.
    for m in _RE_AT_SELECT.finditer(expr):
        target = m.group(1).strip()
        if target:
            result.object_refs.append(target)
            result.uncertain = True

    # 2) Strip runtime @functions (no column lineage from prompts/variables).
    cleaned = _RE_AT_RUNTIME.sub(" ", expr)
    # @Select etc. already captured; remove so their inner text is not re-parsed.
    cleaned = _RE_AT_SELECT.sub(" ", cleaned)

    # 3) Qualified column references alias.col / schema.table.col.
    for m in _RE_QUALIFIED.finditer(cleaned):
        schema, tbl_or_alias, column = m.group(1), m.group(2), m.group(3)
        conf, n = base_confidence, note
        if schema:
            # already schema.table.column — deterministic
            result.refs.append(ColumnRef(column=column, table=tbl_or_alias,
                                         schema=schema, confidence=conf, note=n))
            continue
        resolved = alias_map.get(tbl_or_alias.lower())
        if resolved:
            r_schema, r_table = resolved
            result.refs.append(ColumnRef(
                column=column, table=r_table,
                schema=r_schema or default_schema, confidence=conf, note=n))
        elif not alias_map:
            # Expression mode (no FROM): a BO object SELECT like
            # ``Outlet_Lookup.SHOP_NAME`` references the data-foundation table
            # directly — table.column is deterministic, not an ambiguous alias.
            result.refs.append(ColumnRef(
                column=column, table=tbl_or_alias,
                schema=default_schema, confidence=conf, note=n))
        else:
            # A real SELECT had a FROM/JOIN, but this alias/table is not in it
            # -> genuinely uncertain (derived table, missing join, etc.).
            result.refs.append(ColumnRef(
                column=column, table=tbl_or_alias, schema=default_schema,
                confidence="medium",
                note=(n + "; " if n else "") + "unresolved_alias"))
            result.uncertain = True


def resolve(sql: str, default_schema: Optional[str] = None) -> ResolveResult:
    """Resolve a SELECT statement OR a SELECT-list expression to column refs.

    Args:
        sql: a full ``SELECT ... FROM ...`` statement (Oracle view/MV TEXT) or a
            bare expression (a BO universe object's SELECT, e.g.
            ``sum(Shop_facts.amount_sold)`` or ``@Select(Store\\Name)``).
        default_schema: schema to attribute to tables with no explicit schema
            (e.g. the BO data-foundation connection schema, or the Oracle owner).

    Returns:
        ResolveResult with resolved ColumnRefs (confidence-tagged), any
        unresolved BO object references, and an ``uncertain`` flag.
    """
    result = ResolveResult()
    if not sql or not sql.strip():
        return result

    alias_map: dict[str, tuple[Optional[str], str]] = {}
    target = sql
    if _looks_like_select(sql):
        alias_map = _build_alias_map(sql)
        target = _select_list(sql)

    # @Aggregate_Aware(e1, e2, ...): 1 object -> N candidate expressions.
    agg = _RE_AT_AGG.search(target)
    if agg:
        candidates = _split_top_level(agg.group(1))
        for i, cand in enumerate(candidates):
            _refs_from_expression(
                cand, alias_map, default_schema,
                base_confidence="medium",
                note=f"aggregate_aware_candidate {i + 1}/{len(candidates)}",
                result=result,
            )
        result.uncertain = True
        return result

    _refs_from_expression(target, alias_map, default_schema,
                          base_confidence="high", note=None, result=result)
    return result


def _split_top_level(args: str) -> list[str]:
    """Split a comma-separated arg list at top level (ignoring nested parens)."""
    out, depth, buf = [], 0, []
    for ch in args:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [a for a in out if a]
