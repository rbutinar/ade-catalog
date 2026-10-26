#!/usr/bin/env python3
"""
PBIP (Power BI Project) Metadata Extractor

Comprehensive extractor for Power BI PBIP projects that extracts:
- Tables, columns, measures from TMDL files
- Relationships between tables
- M (Power Query) transformations
- Report pages and visuals WITH measure bindings

This module provides the extraction functions used by extract_to_json.py
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)


def extract_tables_from_tmdl(tmdl_dir: Path) -> List[Dict]:
    """
    Extract table metadata from TMDL files.

    Args:
        tmdl_dir: Path to definition/ directory containing tables/

    Returns:
        List of table dictionaries with name, type, description, etc.
    """
    tables = []
    tables_dir = tmdl_dir / "tables"

    if not tables_dir.exists():
        logger.warning(f"Tables directory not found: {tables_dir}")
        return tables

    for table_file in tables_dir.glob("*.tmdl"):
        table_name = table_file.stem

        with open(table_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check for isHidden property
        is_hidden = 'isHidden' in content and 'isHidden: true' in content.lower()

        # Detect source type from partition
        source_type = 'unknown'
        if 'partition ' in content.lower():
            if '= m' in content:
                source_type = 'import'
            elif '= entity' in content:
                source_type = 'entity'
            elif 'mode: directQuery' in content.lower():
                source_type = 'directquery'
        if 'LocalDateTable' in table_name or 'DateTableTemplate' in table_name:
            source_type = 'calculated'

        tables.append({
            'name': table_name,
            'table_type': 'Unknown',  # Will be inferred later
            'description': '',
            'is_hidden': is_hidden,
            'source_type': source_type
        })

    logger.info(f"Extracted {len(tables)} tables")
    return tables


def extract_columns_from_tmdl(tmdl_dir: Path) -> List[Dict]:
    """
    Extract column metadata from TMDL files.

    Args:
        tmdl_dir: Path to definition/ directory

    Returns:
        List of column dictionaries
    """
    columns = []
    tables_dir = tmdl_dir / "tables"

    if not tables_dir.exists():
        return columns

    for table_file in tables_dir.glob("*.tmdl"):
        table_name = table_file.stem

        with open(table_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        current_column = None

        for line in lines:
            line_stripped = line.strip()

            # Detect column start
            if line_stripped.startswith('column '):
                # Save previous column if exists
                if current_column:
                    columns.append(current_column)

                # Extract column name. Calculated columns are defined inline as
                # `column NAME = <DAX expr>` — keep only NAME (the expression can
                # be hundreds of chars and used to leak into the column name).
                col_def = line_stripped.replace('column ', '', 1)
                if '=' in col_def:
                    name_part, expr_part = col_def.split('=', 1)
                    col_name = name_part.strip().strip('\'"')
                    source_column = '=' + expr_part.strip()   # '=' marks calculated
                else:
                    col_name = col_def.strip(' =\'"')
                    source_column = col_name

                current_column = {
                    'name': col_name,
                    'table_name': table_name,
                    'data_type': 'Unknown',
                    'source_column': source_column,
                    'format_string': '',
                    'is_hidden': False,
                    'is_key': False
                }

            elif current_column:
                # Parse column properties
                if line_stripped.startswith('dataType:'):
                    current_column['data_type'] = line_stripped.split(':', 1)[1].strip()
                elif line_stripped.startswith('sourceColumn:'):
                    current_column['source_column'] = line_stripped.split(':', 1)[1].strip().strip('"\'')
                elif line_stripped.startswith('formatString:'):
                    current_column['format_string'] = line_stripped.split(':', 1)[1].strip()
                elif line_stripped.startswith('isHidden'):
                    current_column['is_hidden'] = True
                elif line_stripped.startswith('isKey'):
                    current_column['is_key'] = True
                # End of column definition
                elif line_stripped.startswith(('column ', 'measure ', 'partition ', 'hierarchy ')):
                    columns.append(current_column)
                    current_column = None

        # Save last column
        if current_column:
            columns.append(current_column)

    logger.info(f"Extracted {len(columns)} columns")
    return columns


def extract_measures_from_tmdl(tmdl_dir: Path) -> List[Dict]:
    """
    Extract measure definitions from TMDL files.

    Args:
        tmdl_dir: Path to definition/ directory

    Returns:
        List of measure dictionaries with DAX expressions
    """
    measures = []
    tables_dir = tmdl_dir / "tables"

    if not tables_dir.exists():
        return measures

    for table_file in tables_dir.glob("*.tmdl"):
        table_name = table_file.stem

        with open(table_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        current_measure = None
        collecting_dax = False

        for i, line in enumerate(lines):
            line_stripped = line.strip()

            # Detect measure start
            if line_stripped.startswith('measure '):
                # Save previous measure
                if current_measure and current_measure.get('dax_expression'):
                    current_measure['dax_expression'] = current_measure['dax_expression'].strip()
                    measures.append(current_measure)

                # Parse measure name and initial DAX
                measure_def = line_stripped.replace('measure ', '', 1)
                if '=' in measure_def:
                    measure_name, dax_part = measure_def.split('=', 1)
                    measure_name = measure_name.strip().strip('\'"')
                    current_measure = {
                        'name': measure_name,
                        'table_name': table_name,
                        'dax_expression': dax_part.strip(),
                        'format_string': '',
                        'is_hidden': False,
                        'display_folder': ''
                    }
                    collecting_dax = True
                else:
                    measure_name = measure_def.strip().strip('\'"')
                    current_measure = {
                        'name': measure_name,
                        'table_name': table_name,
                        'dax_expression': '',
                        'format_string': '',
                        'is_hidden': False,
                        'display_folder': ''
                    }
                    collecting_dax = True

            # Collect DAX expression (indented lines)
            elif collecting_dax and current_measure:
                if line.startswith('\t') and not line_stripped.startswith(('formatString', 'displayFolder', 'isHidden', 'lineageTag', 'annotation')):
                    if current_measure['dax_expression']:
                        current_measure['dax_expression'] += '\n' + line.strip()
                    else:
                        current_measure['dax_expression'] = line.strip()
                elif line_stripped.startswith('formatString:'):
                    current_measure['format_string'] = line_stripped.split(':', 1)[1].strip()
                    collecting_dax = False
                elif line_stripped.startswith('displayFolder:'):
                    current_measure['display_folder'] = line_stripped.split(':', 1)[1].strip().strip('"\'')
                elif line_stripped.startswith('isHidden'):
                    current_measure['is_hidden'] = True
                elif line_stripped.startswith(('measure ', 'column ', 'partition ')):
                    collecting_dax = False

        # Save last measure
        if current_measure and current_measure.get('dax_expression'):
            current_measure['dax_expression'] = current_measure['dax_expression'].strip()
            measures.append(current_measure)

    logger.info(f"Extracted {len(measures)} measures")
    return measures


def extract_relationships_from_tmdl(tmdl_dir: Path) -> List[Dict]:
    """
    Extract relationship definitions from relationships.tmdl.

    Args:
        tmdl_dir: Path to definition/ directory

    Returns:
        List of relationship dictionaries
    """
    relationships = []
    rel_file = tmdl_dir / "relationships.tmdl"

    if not rel_file.exists():
        logger.info("No relationships.tmdl found")
        return relationships

    with open(rel_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    current_rel = None

    for line in lines:
        line_stripped = line.strip()

        if line_stripped.startswith('relationship '):
            # Save previous relationship
            if current_rel and current_rel.get('from_table') and current_rel.get('to_table'):
                relationships.append(current_rel)

            current_rel = {
                'from_table': '',
                'from_column': '',
                'to_table': '',
                'to_column': '',
                'cardinality': 'many_to_one',
                'cross_filter_direction': 'single',
                'is_active': True
            }

        elif current_rel:
            if 'fromColumn:' in line:
                col_ref = line.split(':', 1)[1].strip()
                if '[' in col_ref:
                    parts = col_ref.split('[')
                    current_rel['from_table'] = parts[0].strip()
                    current_rel['from_column'] = parts[1].rstrip(']').strip().strip("'\"")
                elif '.' in col_ref:
                    parts = col_ref.split('.', 1)
                    current_rel['from_table'] = parts[0].strip()
                    current_rel['from_column'] = parts[1].strip().strip("'\"")

            elif 'toColumn:' in line:
                col_ref = line.split(':', 1)[1].strip()
                if '[' in col_ref:
                    parts = col_ref.split('[')
                    current_rel['to_table'] = parts[0].strip()
                    current_rel['to_column'] = parts[1].rstrip(']').strip().strip("'\"")
                elif '.' in col_ref:
                    parts = col_ref.split('.', 1)
                    current_rel['to_table'] = parts[0].strip()
                    current_rel['to_column'] = parts[1].strip().strip("'\"")

            elif 'crossFilteringBehavior:' in line:
                behavior = line.split(':', 1)[1].strip().lower()
                current_rel['cross_filter_direction'] = 'both' if 'both' in behavior else 'single'

            elif 'isActive:' in line:
                is_active = line.split(':', 1)[1].strip().lower()
                current_rel['is_active'] = is_active == 'true'

    # Save last relationship
    if current_rel and current_rel.get('from_table') and current_rel.get('to_table'):
        relationships.append(current_rel)

    logger.info(f"Extracted {len(relationships)} relationships")
    return relationships


def extract_m_queries_from_tmdl(tmdl_dir: Path) -> List[Dict]:
    """
    Extract M (Power Query) transformations from table partitions.

    TMDL M partition format:
        partition 'TableName' = m
            mode: import
            source =
                    let
                        Source = ...,
                        ...
                    in
                        #"Final Step"

        annotation PBI_ResultType = Table

    The M code block starts after 'source =' and continues until a line
    with less indentation (like 'annotation').

    Args:
        tmdl_dir: Path to definition/ directory

    Returns:
        List of transformation dictionaries with M code
    """
    transformations = []
    tables_dir = tmdl_dir / "tables"

    if not tables_dir.exists():
        return transformations

    for table_file in tables_dir.glob("*.tmdl"):
        table_name = table_file.stem

        with open(table_file, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')

        in_partition = False
        in_m_code = False
        current_m_code = []
        m_code_base_indent = 0

        for i, line in enumerate(lines):
            line_stripped = line.strip()

            # Detect M partition start
            if line_stripped.startswith('partition ') and '= m' in line_stripped:
                in_partition = True
                in_m_code = False
                current_m_code = []
                m_code_base_indent = 0

            elif in_partition:
                # Check for 'source =' line - M code starts on next line
                if line_stripped == 'source =' or line_stripped.startswith('source ='):
                    in_m_code = True
                    # If there's content after 'source =' on same line, that's unusual
                    # Normally M code starts on next line
                    continue

                elif in_m_code:
                    # Calculate current line indentation (tabs count as 1)
                    current_indent = len(line) - len(line.lstrip())

                    # First M code line sets the base indentation
                    if not current_m_code and line_stripped:
                        m_code_base_indent = current_indent
                        current_m_code.append(line_stripped)

                    elif line_stripped:
                        # If line has less indentation than M code base, we've exited the block
                        if current_indent < m_code_base_indent:
                            # Save the M query
                            if current_m_code:
                                m_code = '\n'.join(current_m_code)
                                transformations.append({
                                    'name': f"{table_name}_M_Query",
                                    'table_name': table_name,
                                    'm_code': m_code
                                })
                            in_partition = False
                            in_m_code = False
                            current_m_code = []
                        else:
                            # Still inside M code block
                            current_m_code.append(line_stripped)

                    elif not line_stripped and current_m_code:
                        # Empty line inside M code - check if next non-empty line
                        # has proper indentation (look ahead)
                        next_non_empty_indent = 0
                        for j in range(i + 1, min(i + 5, len(lines))):
                            next_line = lines[j]
                            if next_line.strip():
                                next_non_empty_indent = len(next_line) - len(next_line.lstrip())
                                break

                        if next_non_empty_indent >= m_code_base_indent:
                            # Empty line within M code block
                            current_m_code.append('')
                        else:
                            # Empty line signals end of M code
                            if current_m_code:
                                m_code = '\n'.join(current_m_code)
                                transformations.append({
                                    'name': f"{table_name}_M_Query",
                                    'table_name': table_name,
                                    'm_code': m_code
                                })
                            in_partition = False
                            in_m_code = False
                            current_m_code = []

        # Save last M query if file ends while in M code
        if in_m_code and current_m_code:
            m_code = '\n'.join(current_m_code)
            transformations.append({
                'name': f"{table_name}_M_Query",
                'table_name': table_name,
                'm_code': m_code
            })

    logger.info(f"Extracted {len(transformations)} M transformations")
    return transformations


def extract_report_pages(report_dir: Path) -> Tuple[List[Dict], List[Dict]]:
    """
    Extract report pages and visuals WITH measure/column bindings.

    This is critical for lineage: it extracts which measures/columns
    are used in each visual via the 'projections' property.

    Supports TWO formats:
    1. Legacy: Single report.json with embedded sections[].visualContainers[]
    2. Enhanced PBIR: Separate pages/{pageId}/page.json and visuals/{visualId}/visual.json

    Args:
        report_dir: Path to .Report directory

    Returns:
        Tuple of (pages list, visuals list with measure bindings)
    """
    # Check which format we have
    pages_dir = report_dir / "definition" / "pages"
    report_json = report_dir / "report.json"

    if pages_dir.exists():
        # Enhanced PBIR format (Ferrero style)
        return _extract_enhanced_format(report_dir)
    elif report_json.exists():
        # Legacy format (Contoso style)
        return _extract_legacy_format(report_dir)
    else:
        logger.warning(f"No report structure found in {report_dir}")
        return [], []


def _extract_legacy_format(report_dir: Path) -> Tuple[List[Dict], List[Dict]]:
    """Extract from legacy report.json format"""
    report_json = report_dir / "report.json"

    with open(report_json, 'r', encoding='utf-8') as f:
        report_data = json.load(f)

    pages = []
    visuals = []

    sections = report_data.get('sections', [])

    for idx, section in enumerate(sections):
        page_name = section.get('displayName', f'Page {idx + 1}')
        page_id = section.get('name', f'page_{idx}')

        # Parse config for visibility
        config_str = section.get('config', '{}')
        try:
            config = json.loads(config_str)
            visibility = config.get('visibility', 1)
        except:
            visibility = 1

        pages.append({
            'page_id': page_id,
            'page_name': page_name,
            'page_number': idx + 1,
            'is_hidden': visibility != 0,  # visibility 0 = visible, 1+ = hidden
            'page_type': 'Standard',
            'width': int(section.get('width', 1280)),
            'height': int(section.get('height', 720))
        })

        # Extract visuals with measure bindings
        visual_containers = section.get('visualContainers', [])
        for v_idx, visual in enumerate(visual_containers):
            config_str = visual.get('config', '{}')

            try:
                v_config = json.loads(config_str)
            except:
                v_config = {}

            single_visual = v_config.get('singleVisual', {})
            visual_type = single_visual.get('visualType', 'unknown')
            visual_id = v_config.get('name', f'visual_{v_idx}')

            # Extract visual title from vcObjects (user-friendly name)
            visual_title = _extract_visual_title(single_visual)
            visual_name = visual_title if visual_title else visual_id

            # Extract measure/column bindings from projections
            measures_used, columns_used, tables_used = _extract_projections_legacy(single_visual)

            visuals.append({
                'visual_id': f"{page_id}_{visual_id}",
                'page_id': page_id,
                'visual_name': visual_name,
                'visual_type': visual_type,
                'x': visual.get('x', 0),
                'y': visual.get('y', 0),
                'width': visual.get('width', 0),
                'height': visual.get('height', 0),
                'z_order': visual.get('z', 0),
                'measures_used': measures_used,
                'columns_used': columns_used,
                'tables_used': tables_used
            })

    logger.info(f"Extracted {len(pages)} pages with {len(visuals)} visuals (legacy format)")
    return pages, visuals


def _extract_enhanced_format(report_dir: Path) -> Tuple[List[Dict], List[Dict]]:
    """Extract from enhanced PBIR format with separate page/visual files"""
    pages_dir = report_dir / "definition" / "pages"

    pages = []
    visuals = []

    # Read page order
    pages_json = pages_dir / "pages.json"
    page_order = []
    if pages_json.exists():
        with open(pages_json, 'r', encoding='utf-8') as f:
            pages_meta = json.load(f)
            page_order = pages_meta.get('pageOrder', [])

    # Find all page directories
    page_dirs = [d for d in pages_dir.iterdir() if d.is_dir()]

    for idx, page_dir in enumerate(page_dirs):
        page_json = page_dir / "page.json"
        if not page_json.exists():
            continue

        with open(page_json, 'r', encoding='utf-8') as f:
            page_data = json.load(f)

        page_id = page_data.get('name', page_dir.name)
        page_name = page_data.get('displayName', f'Page {idx + 1}')

        # Determine page order
        try:
            page_number = page_order.index(page_id) + 1 if page_id in page_order else idx + 1
        except:
            page_number = idx + 1

        # Check visibility
        visibility = page_data.get('visibility', '')
        is_hidden = 'Hidden' in visibility if visibility else False

        pages.append({
            'page_id': page_id,
            'page_name': page_name,
            'page_number': page_number,
            'is_hidden': is_hidden,
            'page_type': 'Standard',
            'width': int(page_data.get('width', 1280)),
            'height': int(page_data.get('height', 720))
        })

        # Extract visuals from this page
        visuals_dir = page_dir / "visuals"
        if visuals_dir.exists():
            for visual_dir in visuals_dir.iterdir():
                if not visual_dir.is_dir():
                    continue

                visual_json = visual_dir / "visual.json"
                if not visual_json.exists():
                    continue

                with open(visual_json, 'r', encoding='utf-8') as f:
                    visual_data = json.load(f)

                visual_id = visual_data.get('name', visual_dir.name)
                visual_obj = visual_data.get('visual', {})
                visual_type = visual_obj.get('visualType', 'unknown')
                position = visual_data.get('position', {})

                # Extract visual title from vcObjects (user-friendly name)
                visual_title = _extract_visual_title(visual_obj)
                visual_name = visual_title if visual_title else visual_id

                # Extract measure/column bindings from enhanced projections
                measures_used, columns_used, tables_used = _extract_projections_enhanced(visual_obj)

                visuals.append({
                    'visual_id': f"{page_id}_{visual_id}",
                    'page_id': page_id,
                    'visual_name': visual_name,
                    'visual_type': visual_type,
                    'x': position.get('x', 0),
                    'y': position.get('y', 0),
                    'width': position.get('width', 0),
                    'height': position.get('height', 0),
                    'z_order': position.get('z', 0),
                    'measures_used': measures_used,
                    'columns_used': columns_used,
                    'tables_used': tables_used
                })

    logger.info(f"Extracted {len(pages)} pages with {len(visuals)} visuals (enhanced format)")
    return pages, visuals


def _extract_visual_title(single_visual: dict) -> Optional[str]:
    """
    Extract visual title from vcObjects.title property.

    Structure: vcObjects.title[0].properties.text.expr.Literal.Value
    The value is typically quoted like "'My Title'" - we strip the quotes.
    """
    vc_objects = single_visual.get('vcObjects', {})
    title_list = vc_objects.get('title', [])

    if not title_list or not isinstance(title_list, list):
        return None

    title_obj = title_list[0] if title_list else {}
    properties = title_obj.get('properties', {})
    text_prop = properties.get('text', {})
    expr = text_prop.get('expr', {})
    literal = expr.get('Literal', {})
    value = literal.get('Value', '')

    # Strip surrounding quotes (e.g., "'My Title'" -> "My Title")
    if value and isinstance(value, str):
        value = value.strip("'\"")

    return value if value else None


def _extract_projections_legacy(single_visual: dict) -> Tuple[List[str], List[str], List[str]]:
    """Extract projections from legacy format (config.singleVisual.projections)"""
    measures_used = []
    columns_used = []
    tables_used = set()

    projections = single_visual.get('projections', {})
    for role, bindings in projections.items():
        if isinstance(bindings, list):
            for binding in bindings:
                if isinstance(binding, dict):
                    query_ref = binding.get('queryRef', '')
                    if query_ref:
                        _classify_query_ref(query_ref, measures_used, columns_used, tables_used)

    return measures_used, columns_used, list(tables_used)


def _extract_projections_enhanced(visual_obj: dict) -> Tuple[List[str], List[str], List[str]]:
    """Extract projections from enhanced format (visual.query.queryState.{role}.projections)"""
    measures_used = []
    columns_used = []
    tables_used = set()

    query = visual_obj.get('query', {})
    query_state = query.get('queryState', {})

    for role, role_data in query_state.items():
        if isinstance(role_data, dict):
            projections = role_data.get('projections', [])
            if isinstance(projections, list):
                for proj in projections:
                    if isinstance(proj, dict):
                        query_ref = proj.get('queryRef', '')
                        if query_ref:
                            _classify_query_ref(query_ref, measures_used, columns_used, tables_used)

                        # Also check field.Column for table references
                        field = proj.get('field', {})
                        column = field.get('Column', {})
                        expr = column.get('Expression', {})
                        source_ref = expr.get('SourceRef', {})
                        entity = source_ref.get('Entity', '')
                        if entity:
                            tables_used.add(entity)

    return measures_used, columns_used, list(tables_used)


def _classify_query_ref(query_ref: str, measures_used: list, columns_used: list, tables_used: set):
    """Classify a queryRef as measure or column and extract table"""
    parts = query_ref.split('.')
    if len(parts) >= 2:
        table = parts[0]
        field = parts[1]
        tables_used.add(table)

        # Heuristic: measures typically have uppercase first letter or contain aggregations
        if '(' in field or (field and field[0].isupper()):
            measures_used.append(query_ref)
        else:
            columns_used.append(query_ref)


def infer_table_types(tables: List[Dict], relationships: List[Dict]) -> List[Dict]:
    """
    Infer table types (Fact, Dimension, Bridge, Date) based on relationships and naming.

    Args:
        tables: List of table dictionaries
        relationships: List of relationship dictionaries

    Returns:
        Updated tables list with inferred table_type
    """
    # Count relationships for each table
    many_side_count = {}
    one_side_count = {}

    for rel in relationships:
        from_table = rel.get('from_table', '')
        to_table = rel.get('to_table', '')

        # From is typically many side, to is typically one side
        many_side_count[from_table] = many_side_count.get(from_table, 0) + 1
        one_side_count[to_table] = one_side_count.get(to_table, 0) + 1

    for table in tables:
        name = table['name']
        name_lower = name.lower()

        # Check naming patterns
        if 'localdate' in name_lower or 'datetable' in name_lower:
            table['table_type'] = 'Date (Auto)'
            table['source_type'] = 'calculated'
        elif name_lower.startswith(('dim_', 'dimension_')):
            table['table_type'] = 'Dimension'
        elif name_lower.startswith(('fact_', 'fct_')):
            table['table_type'] = 'Fact'
        elif name_lower.startswith(('bridge_', 'brg_')):
            table['table_type'] = 'Bridge'
        elif name_lower.startswith(('vw_', 'view_', 'mvw_')):
            # Infer from relationships
            many_count = many_side_count.get(name, 0)
            one_count = one_side_count.get(name, 0)

            if many_count > one_count and many_count >= 2:
                table['table_type'] = 'Fact'
            elif one_count > many_count:
                table['table_type'] = 'Dimension'
            else:
                table['table_type'] = 'View'
        else:
            # Infer from relationship patterns
            many_count = many_side_count.get(name, 0)
            one_count = one_side_count.get(name, 0)

            if many_count > 2 and many_count > one_count:
                table['table_type'] = 'Fact'
            elif one_count > 0 and one_count >= many_count:
                table['table_type'] = 'Dimension'
            elif many_count == 0 and one_count == 0:
                table['table_type'] = 'Standalone'
            else:
                table['table_type'] = 'Table'

    return tables


def extract_all_from_pbip(pbip_path: Path, output_dir: Optional[Path] = None) -> Dict:
    """
    Extract all metadata from a PBIP project.

    Args:
        pbip_path: Path to .pbip file or .SemanticModel directory
        output_dir: Optional output directory for JSON file

    Returns:
        Dictionary with all extracted metadata
    """
    pbip_path = Path(pbip_path)

    # Find semantic model directory
    if pbip_path.suffix == '.pbip':
        semantic_model_dir = pbip_path.parent / f"{pbip_path.stem}.SemanticModel"
    elif pbip_path.name.endswith('.SemanticModel'):
        semantic_model_dir = pbip_path
    else:
        # Look for .SemanticModel subdirectory
        semantic_model_dirs = list(pbip_path.glob("*.SemanticModel"))
        if semantic_model_dirs:
            semantic_model_dir = semantic_model_dirs[0]
        else:
            raise ValueError(f"No .SemanticModel found in: {pbip_path}")

    if not semantic_model_dir.exists():
        raise FileNotFoundError(f"SemanticModel not found: {semantic_model_dir}")

    # Find definition directory
    tmdl_dir = semantic_model_dir / "definition"
    if not tmdl_dir.exists():
        raise FileNotFoundError(f"Definition directory not found: {tmdl_dir}")

    report_name = semantic_model_dir.name.replace('.SemanticModel', '')
    logger.info(f"Extracting from: {report_name}")

    # Extract all components
    tables = extract_tables_from_tmdl(tmdl_dir)
    columns = extract_columns_from_tmdl(tmdl_dir)
    measures = extract_measures_from_tmdl(tmdl_dir)
    relationships = extract_relationships_from_tmdl(tmdl_dir)
    transformations = extract_m_queries_from_tmdl(tmdl_dir)

    # Find report directory
    report_dir = semantic_model_dir.parent / f"{report_name}.Report"
    pages, visuals = [], []
    if report_dir.exists():
        pages, visuals = extract_report_pages(report_dir)

    # Infer table types
    tables = infer_table_types(tables, relationships)

    return {
        'name': report_name,
        'total_tables': len(tables),
        'total_columns': len(columns),
        'total_measures': len(measures),
        'total_relationships': len(relationships),
        'tables': tables,
        'columns': columns,
        'measures': measures,
        'relationships': relationships,
        'transformations': transformations,
        'report_pages': pages,
        'visuals': visuals
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if len(sys.argv) < 2:
        print("Usage: python pbip_extractor.py <path_to_pbip>")
        sys.exit(1)

    result = extract_all_from_pbip(sys.argv[1])
    print(f"\nExtracted from: {result['name']}")
    print(f"  Tables: {result['total_tables']}")
    print(f"  Columns: {result['total_columns']}")
    print(f"  Measures: {result['total_measures']}")
    print(f"  Relationships: {result['total_relationships']}")
    print(f"  Pages: {len(result['report_pages'])}")
    print(f"  Visuals: {len(result['visuals'])}")
