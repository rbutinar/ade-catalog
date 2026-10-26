#!/usr/bin/env python3
"""
TMDL (Tabular Model Definition Language) Parser

Parses .tmdl files from Power BI PBIP projects to extract:
- Table definitions
- Column definitions
- Measure definitions
- Relationships
- Hierarchies
- Partitions and expressions

TMDL is the new text-based format for Power BI semantic models.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# Keywords that terminate a (possibly multi-line) measure expression: the next
# entity, or one of the measure's own trailing properties. Used as a lookahead so
# the DAX body is captured in full but the properties are not swallowed into it.
_TMDL_MEASURE_STOP = (
    "measure|column|partition|table|hierarchy|calculationGroup|relationship|"
    "formatStringDefinition|formatString|displayFolder|description|lineageTag|"
    "isHidden|changedProperty|annotation|dataCategory|detailRows|summarizeBy|"
    "sortByColumn|kpi|extendedProperty"
)


@dataclass
class TMDLColumn:
    """Represents a column in a TMDL table"""
    name: str
    data_type: str
    source_column: Optional[str] = None
    is_hidden: bool = False
    format_string: Optional[str] = None
    summarize_by: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)


@dataclass
class TMDLMeasure:
    """Represents a measure in a TMDL table"""
    name: str
    expression: str
    format_string: Optional[str] = None
    description: Optional[str] = None
    is_hidden: bool = False
    annotations: Dict[str, str] = field(default_factory=dict)


@dataclass
class TMDLPartition:
    """Represents a partition/data source in a TMDL table"""
    name: str
    mode: str  # import, directQuery, dual
    source_type: str  # m, calculated, entity
    expression: Optional[str] = None


@dataclass
class TMDLTable:
    """Represents a table in a TMDL model"""
    name: str
    columns: List[TMDLColumn] = field(default_factory=list)
    measures: List[TMDLMeasure] = field(default_factory=list)
    partitions: List[TMDLPartition] = field(default_factory=list)
    is_hidden: bool = False
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)


@dataclass
class TMDLRelationship:
    """Represents a relationship between tables"""
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cross_filtering: str = "oneDirection"  # oneDirection, bothDirections
    is_active: bool = True


@dataclass
class TMDLModel:
    """Represents a complete TMDL model"""
    name: str
    tables: List[TMDLTable] = field(default_factory=list)
    relationships: List[TMDLRelationship] = field(default_factory=list)
    culture: str = "en-US"
    annotations: Dict[str, str] = field(default_factory=dict)


class TMDLParser:
    """
    Parser for TMDL (Tabular Model Definition Language) files.

    TMDL files are organized in a directory structure:
    - model.tmdl (root model definition)
    - definition/
      - tables/
        - TableName.tmdl
      - relationships.tmdl
      - cultures/
    """

    def __init__(self, tmdl_path: Path):
        """
        Initialize parser with path to TMDL directory or file.

        Args:
            tmdl_path: Path to .tmdl file or directory containing TMDL files
        """
        self.tmdl_path = Path(tmdl_path)
        self.model = None

    def parse(self) -> TMDLModel:
        """
        Parse TMDL files and return model structure.

        Returns:
            TMDLModel with all parsed elements
        """
        if self.tmdl_path.is_file():
            return self._parse_single_file(self.tmdl_path)
        else:
            return self._parse_directory(self.tmdl_path)

    def _parse_directory(self, dir_path: Path) -> TMDLModel:
        """Parse a TMDL directory structure"""
        model = TMDLModel(name=dir_path.name)

        # Parse model.tmdl if exists
        model_file = dir_path / "model.tmdl"
        if model_file.exists():
            self._parse_model_file(model_file, model)

        # Parse tables
        tables_dir = dir_path / "definition" / "tables"
        if tables_dir.exists():
            for table_file in tables_dir.glob("*.tmdl"):
                table = self._parse_table_file(table_file)
                if table:
                    model.tables.append(table)

        # Parse relationships
        rel_file = dir_path / "definition" / "relationships.tmdl"
        if rel_file.exists():
            model.relationships = self._parse_relationships_file(rel_file)

        self.model = model
        return model

    def _parse_single_file(self, file_path: Path) -> TMDLModel:
        """Parse a single TMDL file"""
        content = file_path.read_text(encoding='utf-8')
        model = TMDLModel(name=file_path.stem)

        # Parse content based on type
        if 'table ' in content.lower():
            table = self._parse_table_content(content)
            if table:
                model.tables.append(table)

        self.model = model
        return model

    def _parse_model_file(self, file_path: Path, model: TMDLModel):
        """Parse model.tmdl for model-level settings"""
        content = file_path.read_text(encoding='utf-8')

        # Extract culture
        culture_match = re.search(r'culture:\s*(\S+)', content)
        if culture_match:
            model.culture = culture_match.group(1)

    def _parse_table_file(self, file_path: Path) -> Optional[TMDLTable]:
        """Parse a table .tmdl file"""
        content = file_path.read_text(encoding='utf-8')
        return self._parse_table_content(content)

    def _parse_table_content(self, content: str) -> Optional[TMDLTable]:
        """Parse table definition from TMDL content"""
        # Extract table name — anchor to the start of a line and exclude newlines,
        # so a `/// description` comment above the declaration (or the word "table"
        # inside a comment) can't be captured as the name.
        table_match = re.search(r"(?im)^[ \t]*table\s+['\"]?([^'\"\r\n]+?)['\"]?[ \t]*$", content)
        if not table_match:
            return None

        table = TMDLTable(name=table_match.group(1).strip())

        # Parse columns (name is a single-line identifier)
        column_pattern = r"(?im)^[ \t]*column\s+['\"]?([^'\"\r\n]+?)['\"]?[ \t]*\r?\n[ \t]*dataType:\s*(\w+)"
        for match in re.finditer(column_pattern, content):
            column = TMDLColumn(
                name=match.group(1).strip(),
                data_type=match.group(2)
            )
            table.columns.append(column)

        # Parse measures. The name is a single-line identifier before '='; the
        # expression may span many lines, so it runs until the next entity keyword
        # OR the measure's own trailing properties (formatString, lineageTag, ...)
        # OR a `///` doc-comment OR end of file. NB: the terminator uses \Z (end of
        # string), NOT $ — with re.M, $ matches every line-end, which truncated a
        # multi-line DAX expression to just its first line.
        measure_pattern = (
            r"(?ims)^[ \t]*measure\s+['\"]?([^'\"\r\n=]+?)['\"]?\s*=\s*(.+?)"
            r"(?=\n[ \t]*(?:" + _TMDL_MEASURE_STOP + r")\b|\n[ \t]*///|\Z)"
        )
        for match in re.finditer(measure_pattern, content):
            measure = TMDLMeasure(
                name=match.group(1).strip(),
                expression=match.group(2).strip()
            )
            table.measures.append(measure)

        return table

    def _parse_relationships_file(self, file_path: Path) -> List[TMDLRelationship]:
        """Parse relationships.tmdl file"""
        content = file_path.read_text(encoding='utf-8')
        relationships = []

        # Pattern for relationship definition
        rel_pattern = r'relationship\s+(\S+).*?fromColumn:\s*(\S+)\.(\S+).*?toColumn:\s*(\S+)\.(\S+)'

        for match in re.finditer(rel_pattern, content, re.IGNORECASE | re.DOTALL):
            rel = TMDLRelationship(
                name=match.group(1),
                from_table=match.group(2),
                from_column=match.group(3),
                to_table=match.group(4),
                to_column=match.group(5)
            )
            relationships.append(rel)

        return relationships

    def get_all_measures(self) -> List[TMDLMeasure]:
        """Get all measures across all tables"""
        if not self.model:
            return []

        measures = []
        for table in self.model.tables:
            measures.extend(table.measures)
        return measures

    def get_all_columns(self) -> List[TMDLColumn]:
        """Get all columns across all tables"""
        if not self.model:
            return []

        columns = []
        for table in self.model.tables:
            columns.extend(table.columns)
        return columns


def parse_tmdl_directory(tmdl_path: str) -> TMDLModel:
    """
    Convenience function to parse a TMDL directory.

    Args:
        tmdl_path: Path to TMDL directory

    Returns:
        Parsed TMDLModel
    """
    parser = TMDLParser(Path(tmdl_path))
    return parser.parse()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python tmdl_parser.py <path_to_tmdl>")
        sys.exit(1)

    tmdl_path = sys.argv[1]
    model = parse_tmdl_directory(tmdl_path)

    print(f"Model: {model.name}")
    print(f"Tables: {len(model.tables)}")
    for table in model.tables:
        print(f"  - {table.name}: {len(table.columns)} columns, {len(table.measures)} measures")
    print(f"Relationships: {len(model.relationships)}")
