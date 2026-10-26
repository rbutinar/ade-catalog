"""
Lineage rendering helpers — turn a node/edge set into a Mermaid diagram.

Shared by the MCP `get_lineage` tool (live rendering inside a Claude
conversation) and the Flask lineage viewer (Mermaid via CDN). One renderer,
two consumers, so the visual stays identical across channels.
"""

# Node fill by OBJECT TYPE, matching the catalog type-chip palette (Tailwind-500)
# so the graph reads with the same visual language as the catalog list.
_TYPE_FILL = {
    "report": "#6366f1",            # indigo
    "dataset": "#f59e0b",           # amber
    "semantic_table": "#0ea5e9",    # sky (tables)
    "table": "#0ea5e9",
    "delta_table": "#0ea5e9",
    "unity_table": "#0ea5e9",
    "view": "#0ea5e9",
    "materialized_view": "#0ea5e9",
    "measure": "#ec4899",           # pink
    "visual": "#d946ef",            # fuchsia
    "page": "#8b5cf6",              # violet
    "report_page": "#8b5cf6",
    "column": "#64748b",            # slate
    "notebook": "#f97316",          # orange
    "pipeline": "#a855f7",          # purple
    "activity": "#a855f7",
    "fabric_warehouse": "#3b82f6",  # blue
    "warehouse": "#3b82f6",
    "fabric_lakehouse": "#10b981",  # emerald
    "lakehouse": "#10b981",
    "workspace": "#64748b",         # slate
    "data_product": "#0d9488",      # teal (cross-platform aggregator)
    "external": "#9ca3af",          # gray
}
_DEFAULT_FILL = "#9CA3AF"


def _type_fill(ntype) -> str:
    return _TYPE_FILL.get((ntype or "").lower(), _DEFAULT_FILL)


def _esc(text: str) -> str:
    """Escape a label for a Mermaid node."""
    if text is None:
        return ""
    return (
        str(text)
        .replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def to_mermaid(
    nodes_by_id: dict,
    edges: list,
    direction: str = "LR",
) -> str:
    """Build a Mermaid flowchart string.

    Args:
        nodes_by_id: {node_id: {"name", "type", "platform"}}
        edges: list of (source_id, target_id, relationship)
        direction: Mermaid flow direction (LR, TB, ...)
    """
    lines = [f"flowchart {direction}"]

    for nid, n in nodes_by_id.items():
        ntype = n.get("type", "")
        label = _esc(n.get("name", str(nid)))
        if ntype:
            label = f"{label}<br/><i>{_esc(ntype)}</i>"
        lines.append(f'    n{nid}["{label}"]')

    for src, tgt, rel in edges:
        if src in nodes_by_id and tgt in nodes_by_id:
            lines.append(f"    n{src} -->|{_esc(rel)}| n{tgt}")

    # Per-type styling (matches the catalog type-chip colours)
    types = {n.get("type") for n in nodes_by_id.values()}
    for ntype in types:
        cls = _css_class(ntype)
        lines.append(f"    classDef {cls} fill:{_type_fill(ntype)},stroke:#333,color:#fff;")

    by_class: dict[str, list[str]] = {}
    for nid, n in nodes_by_id.items():
        by_class.setdefault(_css_class(n.get("type")), []).append(f"n{nid}")
    for cls, ids in by_class.items():
        lines.append(f"    class {','.join(ids)} {cls};")

    return "\n".join(lines)


def _css_class(ntype) -> str:
    return "t_" + (ntype or "unknown").replace("-", "_").replace(" ", "_")
