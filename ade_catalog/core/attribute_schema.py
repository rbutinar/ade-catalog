"""Attribute descriptors for graph nodes — the single source of truth for how a
node's ``properties`` are labelled and typed.

Rationale (data-modelling decision, 2026-07-02): attributes stay attributes (we
keep the mixed model — nodes/edges for what you traverse, properties for what you
display/filter), but we *describe* them here instead of hardcoding it in the
frontend. One registry, reused by the web Details panel, MCP, exports and a
future grid view. Each semantic type also maps to an RDF datatype, so exporting
``lineage_meta`` to RDF/Stardog later is a mechanical projection.

Semantic types:
  label  — a short human-readable value (names, enums)      -> xsd:string
  url    — a clickable link                                  -> xsd:anyURI
  code   — a code/expression block (DAX, SQL, M)             -> xsd:string
  number — an integer/decimal count or metric               -> xsd:integer
  date   — a timestamp                                       -> xsd:dateTime
  id     — an opaque external identifier (GUID)              -> xsd:string
  text   — free text (descriptions)                          -> xsd:string
"""

RDF_DATATYPES = {
    "label": "xsd:string",
    "url": "xsd:anyURI",
    "code": "xsd:string",
    "number": "xsd:integer",
    "date": "xsd:dateTime",
    "id": "xsd:string",
    "text": "xsd:string",
}

# Per node_type: ordered attribute descriptors {key, label, type}. Order is the
# display order. Keys not listed here still surface (under a generic fold) but
# untyped — add them here to give them a label + type.
ATTRIBUTE_SCHEMA: dict[str, list[dict]] = {
    "report": [
        {"key": "web_url", "label": "URL", "type": "url"},
        {"key": "dataset", "label": "Dataset", "type": "label"},
        {"key": "report_type", "label": "Type", "type": "label"},
        {"key": "page_count", "label": "Pages", "type": "number"},
        {"key": "visual_count", "label": "Visuals", "type": "number"},
        {"key": "measure_count", "label": "Measures", "type": "number"},
        {"key": "table_count", "label": "Tables", "type": "number"},
        {"key": "source_workspace_id", "label": "Workspace id", "type": "id"},
        {"key": "source_report_id", "label": "Report id", "type": "id"},
    ],
    "dataset": [
        {"key": "connection_mode", "label": "Connection", "type": "label"},
        {"key": "report", "label": "Report", "type": "label"},
        {"key": "table_count", "label": "Tables", "type": "number"},
        {"key": "measure_count", "label": "Measures", "type": "number"},
        {"key": "relationship_count", "label": "Relationships", "type": "number"},
    ],
    "measure": [
        {"key": "home_table", "label": "Home table", "type": "label"},
        {"key": "display_folder", "label": "Folder", "type": "label"},
        {"key": "format_string", "label": "Format", "type": "label"},
        {"key": "description", "label": "Description", "type": "text"},
        {"key": "dax_expression", "label": "DAX", "type": "code"},
    ],
    "page": [
        {"key": "report", "label": "Report", "type": "label"},
        {"key": "page_number", "label": "Page #", "type": "number"},
        {"key": "visual_count", "label": "Visuals", "type": "number"},
    ],
    "visual": [
        {"key": "visual_type", "label": "Type", "type": "label"},
        {"key": "page", "label": "Page", "type": "label"},
    ],
    "semantic_table": [
        {"key": "source_table", "label": "Source table", "type": "label"},
        {"key": "source_type", "label": "Source type", "type": "label"},
    ],
    "table": [
        {"key": "container", "label": "Container", "type": "label"},
        {"key": "schema", "label": "Schema", "type": "label"},
    ],
    "column": [
        {"key": "home_table", "label": "Table", "type": "label"},
        {"key": "data_type", "label": "Data type", "type": "label"},
        {"key": "is_key", "label": "Key", "type": "label"},
        {"key": "is_calculated", "label": "Calculated", "type": "label"},
        {"key": "description", "label": "Description", "type": "text"},
    ],
    "notebook": [
        {"key": "language", "label": "Language", "type": "label"},
        {"key": "description", "label": "Description", "type": "text"},
        {"key": "code", "label": "Code", "type": "code"},
    ],
    "data_product": [
        {"key": "domain", "label": "Domain", "type": "label"},
        {"key": "environment", "label": "Environment", "type": "label"},
        {"key": "description", "label": "Description", "type": "text"},
        {"key": "member_count", "label": "Members", "type": "number"},
    ],
    "workspace": [
        {"key": "capacity_region", "label": "Region", "type": "label"},
        {"key": "fabric_workspace_id", "label": "Workspace id", "type": "id"},
    ],
}


def get_attribute_schema() -> dict[str, list[dict]]:
    """The full registry (node_type -> ordered attribute descriptors)."""
    return ATTRIBUTE_SCHEMA


def rdf_datatype(semantic_type: str) -> str:
    """RDF datatype for a semantic attribute type (for future RDF/Stardog export)."""
    return RDF_DATATYPES.get(semantic_type, "xsd:string")
