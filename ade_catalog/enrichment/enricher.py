"""The ``enrich`` pass: walk a view's lineage nodes, build a per-object prompt
from its metadata, ask a Claude model for business + technical descriptions, and
store them in enrichment_meta (view-scoped).

Design (ADR 2026-07-01, Phase 3):
- Cross-object + generic: enumerate lineage_meta.nodes (the consumption layer),
  so one code path covers report/dataset/table/measure/notebook/column/...
- Provenance-safe: skip is_human_edited rows; skip unchanged rows (source_hash)
  unless --force. The schema/web/edit all work without an API key; only this
  pass needs one.
- Anthropic SDK is an optional dependency; --dry-run builds+prints prompts and
  never imports it / calls the API / writes.
"""

from __future__ import annotations

import json

from ade_catalog.core.attribute_schema import get_attribute_schema
from ade_catalog.core.enrichment import compute_source_hash, get_enrichment, save_llm_enrichment

# Default classes worth enriching (skip decorative/high-count page/visual).
DEFAULT_CLASSES = [
    "report", "dataset", "semantic_table", "table",
    "measure", "column", "notebook",
]

_CODE_TYPES = {"code"}          # attribute_schema semantic types to truncate
_CODE_MAX = 800                 # chars of DAX/SQL/M kept in the prompt

_SYSTEM_PROMPT = (
    "You are a data-catalog assistant. Given an analytics object's technical "
    "metadata, write two short descriptions. Return ONLY minified JSON with keys "
    '"business" and "technical" — no markdown, no prose around it. '
    '"business": 1-3 plain-language sentences for a business user (what it '
    "represents and why it matters; avoid jargon). "
    '"technical": 1-3 sentences for a data engineer (how it is built/derived, key '
    "sources, logic). Be specific to the metadata; do not invent facts."
)


class Enricher:
    def __init__(self, backend, view_id: int, config):
        self.backend = backend
        self.view_id = view_id
        self.config = config
        self._schema = get_attribute_schema()
        self._client = None

    # -- data ---------------------------------------------------------------
    def enumerate_nodes(self, classes: list[str]) -> list[dict]:
        placeholders = ", ".join(["%s"] * len(classes))
        rows = self.backend.query(
            "SELECT node_id, display_name, node_type, platform, qualified_name, properties "
            "FROM lineage_meta.nodes "
            f"WHERE view_id = %s AND node_type IN ({placeholders}) "
            "ORDER BY node_type, display_name",
            (self.view_id, *classes),
        )
        for r in rows:
            props = r.get("properties")
            if isinstance(props, str):
                try:
                    props = json.loads(props)
                except (ValueError, TypeError):
                    props = {}
            r["properties"] = props if isinstance(props, dict) else {}
        return rows

    def _neighbors(self, node_id: int) -> tuple[list[str], list[str]]:
        rows = self.backend.query(
            "SELECT e.source_node_id AS sid, e.target_node_id AS tid, "
            "ns.display_name AS s_name, ns.node_type AS s_type, "
            "nt.display_name AS t_name, nt.node_type AS t_type "
            "FROM lineage_meta.edges e "
            "JOIN lineage_meta.nodes ns ON e.source_node_id = ns.node_id "
            "JOIN lineage_meta.nodes nt ON e.target_node_id = nt.node_id "
            "WHERE e.source_node_id = %s OR e.target_node_id = %s",
            (node_id, node_id),
        )
        up, down = [], []
        for r in rows:
            if r["tid"] == node_id:
                up.append(f"{r['s_name']} ({r['s_type']})")
            elif r["sid"] == node_id:
                down.append(f"{r['t_name']} ({r['t_type']})")
        return up[:15], down[:15]

    def _attributes(self, node: dict) -> list[tuple[str, str]]:
        """Typed, present attributes for the prompt (code fields truncated)."""
        out = []
        props = node["properties"]
        for a in self._schema.get(node["node_type"], []):
            val = props.get(a["key"])
            if val in (None, ""):
                continue
            text = str(val)
            if a["type"] in _CODE_TYPES and len(text) > _CODE_MAX:
                text = text[:_CODE_MAX] + " …(truncated)"
            out.append((a["label"], text))
        return out

    def _payload(self, node: dict) -> dict:
        up, down = self._neighbors(node["node_id"])
        return {
            "class": node["node_type"],
            "name": node["display_name"],
            "platform": node.get("platform"),
            "qualified_name": node.get("qualified_name"),
            "attributes": self._attributes(node),
            "upstream": up,
            "downstream": down,
        }

    # -- prompt -------------------------------------------------------------
    @staticmethod
    def build_prompt(p: dict) -> str:
        lines = [
            f"Object: {p['name']}",
            f"Class: {p['class']}",
            f"Platform: {p.get('platform') or 'n/a'}",
            f"Qualified name: {p.get('qualified_name') or 'n/a'}",
        ]
        if p["attributes"]:
            lines.append("Attributes:")
            lines += [f"  - {label}: {val}" for label, val in p["attributes"]]
        if p["upstream"]:
            lines.append("Upstream (feeds it): " + ", ".join(p["upstream"]))
        if p["downstream"]:
            lines.append("Downstream (it feeds): " + ", ".join(p["downstream"]))
        return "\n".join(lines)

    # -- llm ----------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            import anthropic  # optional dependency
            key = self.config.enrichment.api_key
            if not key:
                raise RuntimeError(
                    "No ANTHROPIC_API_KEY configured — set it (env or config.yaml) "
                    "or use --dry-run."
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def call_llm(self, prompt: str) -> tuple[str, str]:
        client = self._get_client()
        msg = client.messages.create(
            model=self.config.enrichment.model,
            max_tokens=self.config.enrichment.max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        return _parse_json_pair(text)

    # -- run ----------------------------------------------------------------
    def run(self, classes=None, limit=None, force=False, dry_run=False) -> dict:
        classes = classes or DEFAULT_CLASSES
        nodes = self.enumerate_nodes(classes)
        stats = {"candidates": len(nodes), "generated": 0, "skipped_human": 0,
                 "skipped_unchanged": 0, "failed": 0, "dry_run": bool(dry_run)}
        done = 0
        for node in nodes:
            if limit and done >= limit:
                break
            payload = self._payload(node)
            src_hash = compute_source_hash(payload)
            existing = get_enrichment(self.backend, self.view_id, node["node_type"], node["node_id"])
            if existing and existing.get("is_human_edited"):
                stats["skipped_human"] += 1
                continue
            if existing and not force and existing.get("source_hash") == src_hash:
                stats["skipped_unchanged"] += 1
                continue

            prompt = self.build_prompt(payload)
            if dry_run:
                print(f"\n=== {node['node_type']} / {node['display_name']} (node {node['node_id']}) ===")
                print(prompt)
                done += 1
                continue
            try:
                business, technical = self.call_llm(prompt)
                save_llm_enrichment(
                    self.backend, self.view_id, node["node_type"], node["node_id"],
                    node["display_name"], business=business, technical=technical,
                    model=self.config.enrichment.model, source_hash=src_hash,
                )
                stats["generated"] += 1
            except Exception as e:  # noqa: BLE001 — one bad object shouldn't stop the pass
                stats["failed"] += 1
                print(f"  ! {node['node_type']}/{node['display_name']}: {e}")
            done += 1
        return stats


def _parse_json_pair(text: str) -> tuple[str, str]:
    """Parse the model's JSON reply, tolerating code fences / stray prose."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        # Fallback: whole reply becomes the business description.
        return text.strip(), ""
    return (obj.get("business") or "").strip(), (obj.get("technical") or "").strip()
