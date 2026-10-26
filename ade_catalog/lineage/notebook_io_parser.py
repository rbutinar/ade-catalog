"""
Notebook I/O Parser - Extract input/output data sources from Spark notebook source code.

Ported from ade v1 (ade_app/platforms/databricks/parsers/notebook_io_parser.py) and
extended for Microsoft Fabric notebooks (also Spark/PySpark/SparkSQL): OneLake
abfss paths and MERGE INTO, plus a generalized qualified-name builder (no
client-specific defaults). Used by the Fabric (and future Databricks) lineage
populators to discover notebook -> lakehouse-table reads/writes.
"""

import re
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum


class IODirection(Enum):
    """Direction of data flow."""
    INPUT = "input"
    OUTPUT = "output"


class ConfidenceLevel(Enum):
    """Confidence level for discovered objects."""
    HIGH = "high"        # Explicit table/path name in code
    MEDIUM = "medium"    # Path with known variables
    LOW = "low"          # Dynamic path with runtime variables
    UNCERTAIN = "uncertain"  # Found in comments or ambiguous context


class DiscoveredPlatform(Enum):
    """Platform types for discovered objects."""
    DATABRICKS = "databricks"  # Delta tables in Unity Catalog
    DBFS = "dbfs"              # Databricks File System
    ADLS = "adls"              # Azure Data Lake Storage
    POSTGRESQL = "postgresql"   # PostgreSQL via JDBC
    FILE = "file"              # Generic file reference


@dataclass
class DiscoveredObject:
    """Represents a data source discovered from notebook code."""
    qualified_name: str           # Unique identifier following ADE convention
    platform: str                 # databricks, dbfs, adls, postgresql, file
    object_type: str              # table, file, folder, connection
    direction: str                # input, output
    confidence: str               # high, medium, low, uncertain
    raw_reference: str            # Original string found in code
    line_number: Optional[int] = None
    context: Optional[str] = None  # Surrounding code for reference
    pattern_matched: Optional[str] = None  # Which regex pattern matched
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class NotebookIOParser:
    """
    Parse Databricks notebook source code to extract I/O references.

    Follows the Discovered Objects methodology for consistent lineage building.
    """

    # Input patterns - what the notebook reads from
    INPUT_PATTERNS = {
        # Spark table reads - direct string
        'spark_read_table': {
            'pattern': r'spark\.read\.table\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # Spark table reads - variable reference (will be resolved)
        'spark_read_table_var': {
            'pattern': r'spark\.read\.table\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'medium',
            'resolve_variable': True
        },
        'spark_table': {
            'pattern': r'spark\.table\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # SQL table references (FROM, JOIN) - must have 3-part name to avoid import matches
        'sql_from_table': {
            'pattern': r'(?:FROM|JOIN)\s+(?!pyspark\.|pandas\.|numpy\.)([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # SQL SELECT FROM with schema.table (2-part name, common for PostgreSQL)
        'sql_from_schema_table': {
            'pattern': r'SELECT\s+.*?\s+FROM\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\s',
            'platform': 'postgresql',
            'object_type': 'table',
            'confidence': 'high'
        },
        # JDBC reads (PostgreSQL, etc.)
        'jdbc_read_dbtable': {
            'pattern': r'\.option\s*\(\s*["\']dbtable["\']\s*,\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'postgresql',
            'object_type': 'table',
            'confidence': 'high'
        },
        'jdbc_read_query': {
            'pattern': r'\.option\s*\(\s*["\'](?:dbtable|query)["\']\s*,\s*["\'\s]*\(\s*SELECT.*?FROM\s+([a-zA-Z_][a-zA-Z0-9_.]+)',
            'platform': 'postgresql',
            'object_type': 'table',
            'confidence': 'medium'
        },
        # DBFS file reads
        'dbfs_read_path': {
            'pattern': r'spark\.read\.[^)]+\.load\s*\(\s*["\'](/dbfs/[^"\']+)["\']\s*\)',
            'platform': 'dbfs',
            'object_type': 'file',
            'confidence': 'high'
        },
        'dbfs_path_string': {
            'pattern': r'["\'](/dbfs/mnt/[^"\']+)["\']',
            'platform': 'dbfs',
            'object_type': 'file',
            'confidence': 'medium'
        },
        'mnt_path_string': {
            'pattern': r'["\'](/mnt/[^"\']+)["\']',
            'platform': 'dbfs',
            'object_type': 'file',
            'confidence': 'medium'
        },
        # ADLS paths
        'adls_abfss_path': {
            'pattern': r'abfss://([^@]+)@([^/]+)\.dfs\.core\.windows\.net/([^"\'"\s]+)',
            'platform': 'adls',
            'object_type': 'file',
            'confidence': 'high'
        },
        # Fabric OneLake abfss paths
        'onelake_abfss_path': {
            'pattern': r'abfss://[^@]+@[^/]+\.dfs\.fabric\.microsoft\.com/([^"\'\s]+)',
            'platform': 'fabric',
            'object_type': 'file',
            'confidence': 'high'
        },
        # Pandas reads
        'pd_read_excel': {
            'pattern': r'pd\.read_excel\s*\(\s*["\']([^"\']+)["\']\s*',
            'platform': 'file',
            'object_type': 'file',
            'confidence': 'high'
        },
        'pd_read_csv': {
            'pattern': r'pd\.read_csv\s*\(\s*["\']([^"\']+)["\']\s*',
            'platform': 'file',
            'object_type': 'file',
            'confidence': 'high'
        },
        # dbutils file operations
        'dbutils_fs_ls': {
            'pattern': r'dbutils\.fs\.ls\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'dbfs',
            'object_type': 'folder',
            'confidence': 'medium'
        },
    }

    # Output patterns - what the notebook writes to
    OUTPUT_PATTERNS = {
        # Delta table writes - direct string
        'save_as_table': {
            'pattern': r'\.saveAsTable\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # Delta table writes - variable reference
        'save_as_table_var': {
            'pattern': r'\.saveAsTable\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'medium',
            'resolve_variable': True
        },
        'insert_into': {
            'pattern': r'\.insertInto\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # SQL writes
        'sql_insert_into': {
            'pattern': r'INSERT\s+(?:INTO|OVERWRITE)\s+(?:TABLE\s+)?([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        'sql_create_table': {
            'pattern': r'CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            'platform': 'databricks',
            'object_type': 'table',
            'confidence': 'high'
        },
        # Delta MERGE INTO target (common in Fabric bronze->silver upserts); 1-3 part name
        'sql_merge_into': {
            'pattern': r'MERGE\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*){0,2})',
            'platform': 'fabric',
            'object_type': 'table',
            'confidence': 'high'
        },
        # JDBC writes
        'jdbc_write': {
            'pattern': r'\.write\.[^)]*\.jdbc\s*\([^)]+["\']([a-zA-Z_][a-zA-Z0-9_.]+)["\']',
            'platform': 'postgresql',
            'object_type': 'table',
            'confidence': 'high'
        },
        'jdbc_write_dbtable': {
            'pattern': r'\.option\s*\(\s*["\']dbtable["\']\s*,\s*["\']([^"\']+)["\']\s*\)[^)]*\.save\s*\(',
            'platform': 'postgresql',
            'object_type': 'table',
            'confidence': 'high'
        },
        # File writes
        'dbfs_write_path': {
            'pattern': r'\.write\.[^)]+\.save\s*\(\s*["\']([^"\']+)["\']\s*\)',
            'platform': 'dbfs',
            'object_type': 'file',
            'confidence': 'high'
        },
        'python_file_write': {
            'pattern': r'open\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\'][wa]b?["\']',
            'platform': 'file',
            'object_type': 'file',
            'confidence': 'high'
        },
        # Pandas writes
        'pd_to_excel': {
            'pattern': r'\.to_excel\s*\(\s*["\']([^"\']+)["\']\s*',
            'platform': 'file',
            'object_type': 'file',
            'confidence': 'high'
        },
        'pd_to_csv': {
            'pattern': r'\.to_csv\s*\(\s*["\']([^"\']+)["\']\s*',
            'platform': 'file',
            'object_type': 'file',
            'confidence': 'high'
        },
    }

    # Variable assignment patterns to resolve references
    VARIABLE_PATTERNS = {
        'simple_assignment': r'(\w+)\s*=\s*["\']([^"\']+)["\']',
        'f_string_pattern': r'(\w+)\s*=\s*f["\']([^"\']+)["\']',
    }

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize the parser."""
        self.logger = logger or logging.getLogger(__name__)
        self.variables: Dict[str, str] = {}

    def parse_notebook(self, notebook_path: str) -> Dict[str, Any]:
        """
        Parse a notebook JSON file and extract I/O references.

        Args:
            notebook_path: Path to notebook JSON extraction file

        Returns:
            Dictionary with notebook info and discovered objects
        """
        path = Path(notebook_path)
        if not path.exists():
            raise FileNotFoundError(f"Notebook not found: {notebook_path}")

        with open(path, 'r', encoding='utf-8') as f:
            notebook_data = json.load(f)

        source_code = notebook_data.get('source_code', '')

        return self.parse_source_code(
            source_code=source_code,
            notebook_name=notebook_data.get('notebook_name', path.stem),
            notebook_path=notebook_data.get('notebook_path', '')
        )

    def parse_source_code(
        self,
        source_code: str,
        notebook_name: str = 'unknown',
        notebook_path: str = ''
    ) -> Dict[str, Any]:
        """
        Parse notebook source code to extract I/O references.

        Args:
            source_code: Python/SQL source code from notebook
            notebook_name: Name of the notebook
            notebook_path: Full path in Databricks workspace

        Returns:
            Dictionary with discovered inputs and outputs
        """
        # First pass: extract variable assignments
        self._extract_variables(source_code)

        # Find inputs
        inputs = self._find_references(source_code, self.INPUT_PATTERNS, IODirection.INPUT)

        # Find outputs
        outputs = self._find_references(source_code, self.OUTPUT_PATTERNS, IODirection.OUTPUT)

        # Deduplicate
        inputs = self._deduplicate(inputs)
        outputs = self._deduplicate(outputs)

        result = {
            'notebook_name': notebook_name,
            'notebook_path': notebook_path,
            'discovered_by': 'databricks',
            'inputs': [obj.to_dict() for obj in inputs],
            'outputs': [obj.to_dict() for obj in outputs],
            'summary': {
                'total_inputs': len(inputs),
                'total_outputs': len(outputs),
                'inputs_by_platform': self._count_by_platform(inputs),
                'outputs_by_platform': self._count_by_platform(outputs),
            }
        }

        return result

    def _extract_variables(self, source_code: str) -> None:
        """Extract variable assignments for reference resolution."""
        self.variables = {}

        for pattern in self.VARIABLE_PATTERNS.values():
            for match in re.finditer(pattern, source_code, re.MULTILINE):
                var_name = match.group(1)
                var_value = match.group(2)
                self.variables[var_name] = var_value

    def _find_references(
        self,
        source_code: str,
        patterns: Dict[str, Dict],
        direction: IODirection
    ) -> List[DiscoveredObject]:
        """Find all references matching the given patterns."""
        found = []
        lines = source_code.split('\n')

        for pattern_name, pattern_info in patterns.items():
            regex = re.compile(pattern_info['pattern'], re.IGNORECASE | re.MULTILINE)

            for match in regex.finditer(source_code):
                # Get raw reference
                raw_ref = match.group(1) if match.groups() else match.group(0)

                # Skip if in comment
                line_start = source_code.rfind('\n', 0, match.start()) + 1
                line_content = source_code[line_start:match.end()]
                if self._is_in_comment(line_content, match.start() - line_start):
                    continue

                # Check if this is a variable that needs resolution
                resolve_var = pattern_info.get('resolve_variable', False)
                resolved_ref = raw_ref
                actual_confidence = pattern_info['confidence']

                if resolve_var and raw_ref in self.variables:
                    resolved_ref = self.variables[raw_ref]
                    actual_confidence = 'high'  # Upgrade confidence if resolved
                elif resolve_var and raw_ref not in self.variables:
                    # Variable not found, mark as low confidence
                    actual_confidence = 'low'
                    resolved_ref = f"${{{raw_ref}}}"  # Mark as unresolved variable

                # Calculate line number
                line_num = source_code[:match.start()].count('\n') + 1

                # Get context (surrounding lines)
                context = self._get_context(lines, line_num - 1)

                # Build qualified name using resolved reference
                qualified_name = self._build_qualified_name(
                    resolved_ref,
                    pattern_info['platform'],
                    pattern_info['object_type']
                )

                # Determine actual confidence
                confidence = self._assess_confidence(resolved_ref, actual_confidence)

                obj = DiscoveredObject(
                    qualified_name=qualified_name,
                    platform=pattern_info['platform'],
                    object_type=pattern_info['object_type'],
                    direction=direction.value,
                    confidence=confidence,
                    raw_reference=raw_ref if not resolve_var else f"{raw_ref} -> {resolved_ref}",
                    line_number=line_num,
                    context=context,
                    pattern_matched=pattern_name
                )

                found.append(obj)

        return found

    def _is_in_comment(self, line: str, position: int) -> bool:
        """Check if position in line is within a comment."""
        # Check for # comment
        hash_pos = line.find('#')
        if hash_pos != -1 and hash_pos < position:
            return True

        # Check for triple quote docstring (simplified)
        if '"""' in line or "'''" in line:
            return True

        return False

    def _get_context(self, lines: List[str], line_idx: int, context_size: int = 2) -> str:
        """Get surrounding lines for context."""
        start = max(0, line_idx - context_size)
        end = min(len(lines), line_idx + context_size + 1)
        return '\n'.join(lines[start:end])

    def _build_qualified_name(self, raw_ref: str, platform: str, object_type: str) -> str:
        """
        Build qualified name following ADE convention.

        Format: {platform}.{path_or_table}
        Examples:
            - databricks.silver_sales_cert.retail.baseline
            - file.dbfs./mnt/files/Retail/Baseline/INPUT/*.xlsx
            - postgresql.contoso.mvw_currentspend
        """
        # Clean up the reference
        clean_ref = raw_ref.strip()

        # Handle different platforms
        if platform == 'databricks':
            # Unity Catalog format: catalog.schema.table
            return f"databricks.{clean_ref}"

        elif platform == 'dbfs':
            # Normalize DBFS paths
            if clean_ref.startswith('/dbfs'):
                clean_ref = clean_ref[5:]  # Remove /dbfs prefix
            return f"file.dbfs.{clean_ref}"

        elif platform == 'adls':
            return f"file.adls.{clean_ref}"

        elif platform == 'postgresql':
            return f"postgresql.{clean_ref}"

        elif platform == 'fabric':
            return f"fabric.{clean_ref}"

        elif platform == 'file':
            return f"file.local.{clean_ref}"

        return f"{platform}.{clean_ref}"

    def _assess_confidence(self, raw_ref: str, base_confidence: str) -> str:
        """Assess actual confidence based on reference content."""
        # Check for variables or f-strings
        if '{' in raw_ref or '$' in raw_ref:
            return 'low'

        # Check for wildcards
        if '*' in raw_ref or '?' in raw_ref:
            return 'medium'

        # Check for config references
        if 'config[' in raw_ref.lower() or 'config.' in raw_ref.lower():
            return 'low'

        return base_confidence

    def _deduplicate(self, objects: List[DiscoveredObject]) -> List[DiscoveredObject]:
        """Remove duplicate discoveries, keeping highest confidence."""
        seen: Dict[str, DiscoveredObject] = {}
        confidence_order = {'high': 3, 'medium': 2, 'low': 1, 'uncertain': 0}

        for obj in objects:
            key = obj.qualified_name
            if key not in seen:
                seen[key] = obj
            else:
                # Keep higher confidence
                existing_conf = confidence_order.get(seen[key].confidence, 0)
                new_conf = confidence_order.get(obj.confidence, 0)
                if new_conf > existing_conf:
                    seen[key] = obj

        return list(seen.values())

    def _count_by_platform(self, objects: List[DiscoveredObject]) -> Dict[str, int]:
        """Count objects by platform."""
        counts: Dict[str, int] = {}
        for obj in objects:
            counts[obj.platform] = counts.get(obj.platform, 0) + 1
        return counts

    def parse_all_notebooks(self, extraction_dir: str) -> Dict[str, Any]:
        """
        Parse all notebook extractions in a directory.

        Args:
            extraction_dir: Directory containing notebook JSON files

        Returns:
            Aggregated results for all notebooks
        """
        dir_path = Path(extraction_dir)
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {extraction_dir}")

        results = {
            'notebooks': [],
            'all_discovered_objects': [],
            'summary': {
                'notebooks_parsed': 0,
                'total_inputs': 0,
                'total_outputs': 0,
                'unique_objects': 0
            }
        }

        seen_objects: Dict[str, Dict] = {}

        for json_file in dir_path.glob('*.json'):
            try:
                notebook_result = self.parse_notebook(str(json_file))
                results['notebooks'].append(notebook_result)
                results['summary']['notebooks_parsed'] += 1
                results['summary']['total_inputs'] += notebook_result['summary']['total_inputs']
                results['summary']['total_outputs'] += notebook_result['summary']['total_outputs']

                # Aggregate discovered objects
                for obj in notebook_result['inputs'] + notebook_result['outputs']:
                    qn = obj['qualified_name']
                    if qn not in seen_objects:
                        seen_objects[qn] = {
                            **obj,
                            'discovered_in': [notebook_result['notebook_name']],
                            'discovery_count': 1
                        }
                    else:
                        seen_objects[qn]['discovered_in'].append(notebook_result['notebook_name'])
                        seen_objects[qn]['discovery_count'] += 1

            except Exception as e:
                self.logger.warning(f"Failed to parse {json_file}: {e}")

        results['all_discovered_objects'] = list(seen_objects.values())
        results['summary']['unique_objects'] = len(seen_objects)

        return results


def main():
    """CLI entry point for testing."""
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python notebook_io_parser.py <notebook.json|directory>")
        sys.exit(1)

    path = sys.argv[1]
    parser = NotebookIOParser()

    if Path(path).is_dir():
        results = parser.parse_all_notebooks(path)
    else:
        results = parser.parse_notebook(path)

    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
