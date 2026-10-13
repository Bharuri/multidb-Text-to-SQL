"""Build physical and semantic YAML mappings from the per-schema requirement files.

Reads specs/properties/physical_requirements/*.md and
specs/properties/semantic_requirements/*.md, checks them against each other,
and writes specs/properties/physical_mapping/*.yaml and
specs/properties/semantic_mapping/*.yaml.

Run from the repository root:
    python -m src.mapping.requirements_to_yaml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROPERTIES_DIR = ROOT / "specs" / "properties"
PHYSICAL_REQ_DIR = PROPERTIES_DIR / "physical_requirements"
SEMANTIC_REQ_DIR = PROPERTIES_DIR / "semantic_requirements"
PHYSICAL_YAML_DIR = PROPERTIES_DIR / "physical_mapping"
SEMANTIC_YAML_DIR = PROPERTIES_DIR / "semantic_mapping"
SYNONYMS_PATH = SEMANTIC_YAML_DIR / "synonyms.yaml"

INTEGER_TYPES = ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT")
FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")
CATEGORY_LIMIT = 30

PHYSICAL_TABLES_HEADER = ["Table", "Source sheet", "Rows", "Columns"]
PHYSICAL_COLUMNS_HEADER = ["Column", "Type", "Nullable", "Has NULLs", "Unique"]
RELATIONSHIPS_HEADER = [
    "Relationship",
    "Child (table.column)",
    "Parent (table.column)",
    "Cardinality",
    "Enforceable now",
]
CLASSIFICATION_HEADER = ["Table", "Column", "Data type", "Class", "Values", "Numeric rules", "Flags"]
OPERATOR_HEADER = ["Operator", "Phrases", "Template"]
NUMERIC_RANGE_HEADER = ["Column", "Min", "Max", "NULL matches", "BETWEEN bounds"]
DATE_TYPES = ("DATE", "TIMESTAMP")
DATE_OPERATORS = ["year", "month", "year_month", "date_between"]

BACKTICK_PATTERN = re.compile(r"`([^`]*)`")
VALUE_LINE_PATTERN = re.compile(r"- `[^`]*`")
VALUE_HEADING_PATTERN = re.compile(r"`([^`]+)` \((categorical|boolean), (\d+) values?\)")
CHECK_PATTERN = re.compile(r"^- `([^`]+)`: child (\S+), parent (\S+), orphan rows (\d+)$")


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def search(pattern: str, text: str, what: str) -> re.Match[str]:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise ValueError(f"missing {what}")
    return match


def sections(lines: list[str], level: int) -> dict[str, list[str]]:
    """Map each heading of the given level to the lines beneath it."""
    marker = "#" * level + " "
    result: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith(marker):
            current = line[len(marker):].strip()
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return result


def markdown_tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """Return (header, rows) for each markdown table in lines. The separator row is dropped."""
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in [*lines, ""]:
        if line.strip().startswith("|"):
            block.append(line.strip())
        elif block:
            blocks.append(block)
            block = []
    tables = []
    for block in blocks:
        cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in block]
        tables.append((cells[0], cells[2:]))
    return tables


def find_table(lines: list[str], header: list[str], where: str) -> list[list[str]]:
    for found_header, rows in markdown_tables(lines):
        if found_header == header:
            return rows
    raise ValueError(f"table with columns {header} not found in {where}")


def unbacktick(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"`([^`]*)`", text)
    return match.group(1) if match else text


def yes_no(text: str) -> bool:
    if text == "yes":
        return True
    if text == "no":
        return False
    raise ValueError(f"expected yes or no, got {text!r}")


def bullet_value(body: list[str], label: str, where: str) -> str:
    prefix = f"- {label}: "
    for line in body:
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):]
    raise ValueError(f"'{label}' missing in {where}")


def is_numeric(data_type: str) -> bool:
    return data_type.upper().startswith(INTEGER_TYPES + FLOAT_TYPES)


def to_number(text: str, data_type: str) -> int | float:
    if data_type.upper().startswith(INTEGER_TYPES):
        return int(text)
    return float(text)


def to_value(text: str, data_type: str) -> int | float | str:
    return to_number(text, data_type) if is_numeric(data_type) else text


def split_reference(text: str) -> tuple[str, str]:
    table, _, column = unbacktick(text).partition(".")
    return table, column


def parse_physical(path: Path) -> dict:
    lines = read_lines(path)
    text = "\n".join(lines)
    schema = search(r"^# [^`\n]*`([^`\n]+)`", text, "schema name in title").group(1)
    counts = search(r"its (\d+) table\(s\), (\d+) columns", text, "table and column counts in Purpose")
    file_path = search(r"^- Database file: `([^`]+)`", text, "database file in Source").group(1)
    workbook = search(r"^- Workbook: `([^`]+)`", text, "workbook in Source").group(1)
    secs = sections(lines, 2)

    column_sections = {unbacktick(heading): body for heading, body in sections(secs["Columns"], 3).items()}
    tables = []
    for table_cell, sheet, rows, n_columns in find_table(secs["Tables"], PHYSICAL_TABLES_HEADER, "Tables"):
        name = unbacktick(table_cell)
        if name not in column_sections:
            raise ValueError(f"no Columns section for table {name}")
        columns = [
            {
                "name": unbacktick(col),
                "type": dtype,
                "nullable": yes_no(nullable),
                "has_nulls": yes_no(has_nulls),
                "is_unique": yes_no(unique),
            }
            for col, dtype, nullable, has_nulls, unique in find_table(
                column_sections[name], PHYSICAL_COLUMNS_HEADER, f"Columns of {name}"
            )
        ]
        if len(columns) != int(n_columns):
            raise ValueError(f"table {name}: {len(columns)} columns listed, Tables says {n_columns}")
        tables.append(
            {
                "name": name,
                "source_sheet": unbacktick(sheet),
                "row_count": int(rows.replace(",", "")),
                "columns": columns,
            }
        )

    if len(tables) != int(counts.group(1)):
        raise ValueError(f"{len(tables)} tables found, Purpose says {counts.group(1)}")
    total_columns = sum(len(table["columns"]) for table in tables)
    if total_columns != int(counts.group(2)):
        raise ValueError(f"{total_columns} columns found, Purpose says {counts.group(2)}")

    return {
        "schema": schema,
        "database": schema,
        "file": file_path,
        "workbook": workbook,
        "tables": tables,
        "relationships": parse_relationships(secs),
    }


def parse_relationships(secs: dict[str, list[str]]) -> list[dict]:
    body = secs["Relationships between tables"]
    if not markdown_tables(body):
        return []
    details = {unbacktick(heading): lines for heading, lines in sections(body, 3).items()}
    relationships = []
    for name_cell, child, parent, cardinality, enforceable in find_table(
        body, RELATIONSHIPS_HEADER, "Relationships between tables"
    ):
        name = unbacktick(name_cell)
        if name not in details:
            raise ValueError(f"no detail section for relationship {name}")
        detail = details[name]
        enforceable_now = yes_no(enforceable)
        if yes_no(bullet_value(detail, "Enforceable now", name)) != enforceable_now:
            raise ValueError(f"relationship {name}: enforceable flag differs between table and detail")
        child_table, child_column = split_reference(child)
        parent_table, parent_column = split_reference(parent)
        issues = bullet_value(detail, "Issues", name)
        checks = []
        for line in detail:
            match = CHECK_PATTERN.match(line.strip())
            if match:
                checks.append(
                    {
                        "column": match.group(1),
                        "child_type": match.group(2),
                        "parent_type": match.group(3),
                        "orphan_rows": int(match.group(4)),
                    }
                )
        relationships.append(
            {
                "name": name,
                "status": bullet_value(detail, "Status", name),
                "meaning": bullet_value(detail, "Meaning", name),
                "cardinality": cardinality,
                "enforceable_now": enforceable_now,
                "child": {"table": child_table, "column": child_column},
                "parent": {"table": parent_table, "column": parent_column},
                "issues": [] if issues == "none" else [issues],
                "checks": checks,
            }
        )
    return relationships


def parse_value_mappings(lines: list[str]) -> dict[str, dict]:
    info = {}
    for heading, body in sections(lines, 3).items():
        match = VALUE_HEADING_PATTERN.fullmatch(heading)
        if match is None:
            raise ValueError(f"unexpected value mapping heading: {heading}")
        key, _, count = match.groups()
        values, true_expressions, false_expressions = [], [], []
        true_value, false_value = None, None
        for line in body:
            stripped = line.strip()
            if stripped.startswith("- True expressions:"):
                true_expressions = BACKTICK_PATTERN.findall(stripped)
            elif stripped.startswith("- False expressions:"):
                false_expressions = BACKTICK_PATTERN.findall(stripped)
            elif stripped.startswith("- True value:"):
                true_value = BACKTICK_PATTERN.findall(stripped)
            elif stripped.startswith("- False value:"):
                false_value = BACKTICK_PATTERN.findall(stripped)
            elif VALUE_LINE_PATTERN.fullmatch(stripped):
                values.append(stripped[3:-1])
        if len(values) != int(count):
            raise ValueError(f"{key}: {len(values)} values listed, heading says {count}")
        info[key] = {
            "values": values,
            "true": true_expressions,
            "false": false_expressions,
            "true_value": true_value,
            "false_value": false_value,
        }
    return info


def stored_side(found: list[str] | None, key: str, side: str, values: list[str], data_type: str, required: bool):
    if found is None:
        raise ValueError(f"{key}: missing '{side}' line")
    if not found:
        if required:
            raise ValueError(f"{key}: '{side}' must name a stored value")
        return None
    if len(found) != 1 or found[0] not in values:
        raise ValueError(f"{key}: '{side}' must be one of the listed values")
    return to_value(found[0], data_type)


def parse_semantic(path: Path) -> dict:
    lines = read_lines(path)
    text = "\n".join(lines)
    schema = search(r"^# [^`\n]*`([^`\n]+)`", text, "schema name in title").group(1)
    database_file = search(r"^- Database file: `([^`]+)`", text, "database file in Inputs").group(1)
    schema_mapping = search(r"^- Physical mapping: `([^`]+)`", text, "physical mapping in Inputs").group(1)
    secs = sections(lines, 2)

    value_info = parse_value_mappings(secs["Value mappings"])
    numeric_body = secs["Numeric rules"]
    numeric_operators = [
        {
            "operator": unbacktick(operator),
            "phrases": [phrase.strip() for phrase in phrases.split(";")],
            "template": unbacktick(template),
        }
        for operator, phrases, template in find_table(numeric_body, OPERATOR_HEADER, "Numeric rules")
    ]
    numeric_ranges = {
        unbacktick(row[0]): row
        for row in find_table(numeric_body, NUMERIC_RANGE_HEADER, "Numeric rules")
    }
    columns = []
    used_value_keys = set()
    used_range_keys = set()
    for table_cell, column_cell, data_type, column_class, value_count, has_rules, flags_cell in find_table(
        secs["Column classification"], CLASSIFICATION_HEADER, "Column classification"
    ):
        table, column = unbacktick(table_cell), unbacktick(column_cell)
        key = f"{table}.{column}"
        item = {
            "database": schema,
            "schema": schema,
            "table": table,
            "column": column,
            "data_type": data_type,
            "column_class": column_class,
        }

        if column_class in ("categorical", "boolean"):
            if key not in value_info:
                raise ValueError(f"{key} is {column_class} but has no value mapping")
            info = value_info[key]
            if len(info["values"]) != int(value_count) or len(info["values"]) > CATEGORY_LIMIT:
                raise ValueError(f"{key}: value count is outside the rule of {CATEGORY_LIMIT} or differs")
            used_value_keys.add(key)
            item["values"] = [to_value(value, data_type) for value in info["values"]]
            if column_class == "boolean":
                item["true_expressions"] = info["true"]
                item["false_expressions"] = info["false"]
                item["true_value"] = stored_side(info["true_value"], key, "True value", info["values"], data_type, True)
                item["false_value"] = stored_side(info["false_value"], key, "False value", info["values"], data_type, False)
        elif key in value_info:
            raise ValueError(f"{key} has a value mapping but its class is {column_class}")

        if has_rules == "yes":
            if key not in numeric_ranges:
                raise ValueError(f"{key} has numeric rules but no range row")
            used_range_keys.add(key)
            _, low, high, null_matches, bounds = numeric_ranges[key]
            item["numeric_rules"] = {
                "min": to_number(low, data_type),
                "max": to_number(high, data_type),
                "null_matches": yes_no(null_matches),
                "between_bounds": bounds,
            }
        elif key in numeric_ranges:
            raise ValueError(f"{key} has a range row but no numeric rules flag")

        if data_type.upper().startswith(DATE_TYPES):
            item["date_operators"] = list(DATE_OPERATORS)

        item["synonyms"] = []
        item["flags"] = [] if flags_cell == "-" else [flag.strip() for flag in flags_cell.split(",")]
        columns.append(item)

    if set(value_info) - used_value_keys:
        raise ValueError(f"value mappings for unknown columns: {sorted(set(value_info) - used_value_keys)}")
    if set(numeric_ranges) - used_range_keys:
        raise ValueError(f"range rows for unknown columns: {sorted(set(numeric_ranges) - used_range_keys)}")

    return {
        "database": schema,
        "schema": schema,
        "database_file": database_file,
        "schema_mapping": schema_mapping,
        "numeric_operators": numeric_operators,
        "columns": columns,
    }


def check_pair(stem: str, physical: dict, semantic: dict) -> None:
    if physical["schema"] != stem or semantic["schema"] != stem:
        raise ValueError("schema name in title does not match the file name")
    physical_columns = {
        (table["name"], column["name"]) for table in physical["tables"] for column in table["columns"]
    }
    semantic_columns = {(column["table"], column["column"]) for column in semantic["columns"]}
    if physical_columns != semantic_columns:
        only_physical = sorted(physical_columns - semantic_columns)
        only_semantic = sorted(semantic_columns - physical_columns)
        raise ValueError(f"columns differ: only in physical {only_physical}, only in semantic {only_semantic}")


def load_synonyms(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("synonyms") if isinstance(data, dict) else None
    if not isinstance(entries, list) or set(data) != {"synonyms"}:
        raise ValueError(f"{path.name}: expected one top-level 'synonyms' list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"schema", "table", "column", "terms"}:
            raise ValueError(f"{path.name}: each entry needs schema, table, column, and terms")
        terms = entry["terms"]
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) and t.strip() for t in terms):
            raise ValueError(f"{path.name}: terms for {entry['table']}.{entry['column']} must be a non-empty list of text")
        key = (entry["schema"], entry["table"], entry["column"])
        if key in seen:
            raise ValueError(f"{path.name}: {key} is listed more than once")
        seen.add(key)
    return entries


def apply_synonyms(stem: str, semantic: dict, entries: list[dict]) -> None:
    by_key = {(column["table"], column["column"]): column for column in semantic["columns"]}
    for entry in entries:
        if entry["schema"] != stem:
            continue
        column = by_key.get((entry["table"], entry["column"]))
        if column is None:
            raise ValueError(f"synonym for unknown column {entry['table']}.{entry['column']}")
        column["synonyms"] = entry["terms"]


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=100)


def main() -> int:
    physical_files = {path.stem: path for path in PHYSICAL_REQ_DIR.glob("*.md")}
    semantic_files = {path.stem: path for path in SEMANTIC_REQ_DIR.glob("*.md")}
    if physical_files.keys() != semantic_files.keys():
        print(
            "error: physical and semantic requirement files differ: "
            f"{sorted(physical_files)} vs {sorted(semantic_files)}"
        )
        return 1

    try:
        synonym_entries = load_synonyms(SYNONYMS_PATH)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    unknown_schemas = {entry["schema"] for entry in synonym_entries} - set(physical_files)
    if unknown_schemas:
        print(f"error in {SYNONYMS_PATH.name}: unknown schemas {sorted(unknown_schemas)}")
        return 1

    outputs: list[tuple[Path, dict]] = []
    for stem in sorted(physical_files):
        try:
            physical = parse_physical(physical_files[stem])
            semantic = parse_semantic(semantic_files[stem])
            check_pair(stem, physical, semantic)
            apply_synonyms(stem, semantic, synonym_entries)
        except ValueError as exc:
            print(f"error in {stem}: {exc}")
            return 1
        outputs.append((PHYSICAL_YAML_DIR / f"{stem}.yaml", physical))
        outputs.append((SEMANTIC_YAML_DIR / f"{stem}.yaml", semantic))

    for path, data in outputs:
        write_yaml(path, data)
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
