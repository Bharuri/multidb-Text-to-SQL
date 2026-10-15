"""Strict schema and reference validation for physical and semantic metadata.

Run as `python -m src.mapping.metadata_validator`. Exits 1 and lists every
problem with its file and location when any configured metadata file is invalid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from src.mapping.requirements_to_yaml import (
    CARDINALITIES,
    NORMALIZATION_CAST_TYPES,
    NORMALIZATION_OPS,
    is_numeric,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"

PHYSICAL_TOP_KEYS = {"schema", "database", "file", "workbook", "tables", "relationships"}
TABLE_KEYS = {"name", "source_sheet", "row_count", "columns"}
COLUMN_KEYS = {"name", "type", "nullable", "has_nulls", "is_unique"}
RELATIONSHIP_KEYS = {
    "name", "status", "meaning", "cardinality", "enforceable_now", "enabled",
    "join_policy", "normalization", "fan_out", "child", "parent", "issues", "checks",
}
END_KEYS = {"table", "column"}
CHECK_KEYS = {"column", "child_type", "parent_type", "orphan_rows"}
RELATIONSHIP_STATUSES = {"proposed", "approved", "rejected"}
JOIN_POLICIES = {"inner", "left"}

SEMANTIC_TOP_KEYS = {
    "database", "schema", "database_file", "schema_mapping", "numeric_operators", "columns", "metrics",
}
SEMANTIC_COLUMN_REQUIRED = {
    "database", "schema", "table", "column", "data_type", "column_class", "synonyms", "flags",
}
SEMANTIC_COLUMN_OPTIONAL = {
    "values", "numeric_rules", "date_operators", "true_expressions",
    "false_expressions", "true_value", "false_value",
}
COLUMN_CLASSES = {"categorical", "numeric", "boolean", "date", "other"}
OPERATOR_KEYS = {"operator", "phrases", "template"}
DATE_OPERATORS = {"year", "month", "year_month", "date_between"}
METRIC_STATUSES = {"approved", "proposed"}
METRIC_KIND_KEYS = {
    "ratio": {"numerator", "denominator"},
    "aggregate": {"expression"},
    "weighted_average": {"value", "weight"},
}
METRIC_BASE_KEYS = {
    "name", "synonyms", "status", "kind", "unit", "grain", "null_zero_policy", "description",
}
METRIC_AGGREGATES = {"sum", "count", "avg"}
METRIC_FILTER_OPERATORS = {"=", "!=", "<", "<=", ">", ">="}
METRIC_CATEGORICAL_OPERATORS = {"=", "!="}
METRIC_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

TYPE_PATTERN = re.compile(
    r"^(VARCHAR|BIGINT|INTEGER|SMALLINT|TINYINT|HUGEINT|DOUBLE|FLOAT|REAL|BOOLEAN|DATE|TIMESTAMP"
    r"|DECIMAL\(\d+,\s*\d+\))$"
)


def _check_keys(obj: Any, required: set[str], optional: set[str], where: str, file: str, errors: list[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{file}: {where}: expected a mapping, found {type(obj).__name__}. Fix the YAML structure.")
        return False
    missing = required - obj.keys()
    unknown = obj.keys() - required - optional
    for key in sorted(missing):
        errors.append(f"{file}: {where}: missing required key '{key}'. Add it or regenerate from requirements.")
    for key in sorted(unknown):
        errors.append(f"{file}: {where}: unknown key '{key}'. Remove it or add it to the metadata schema.")
    return not missing and not unknown


def _check_bool(value: Any, where: str, file: str, errors: list[str]) -> None:
    if not isinstance(value, bool):
        errors.append(f"{file}: {where}: must be true or false, found {value!r}.")


def _check_str(value: Any, where: str, file: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{file}: {where}: must be a non-empty string, found {value!r}.")


def _check_normalization(value: Any, where: str, file: str, errors: list[str]) -> None:
    """Each side is a list of approved ops, where each op is lower, trim, or cast <allowed type>."""
    if not isinstance(value, dict) or set(value) != {"child", "parent"}:
        errors.append(f"{file}: {where}: must be a mapping with exactly the keys child and parent.")
        return
    for side in ("child", "parent"):
        ops = value[side]
        if not isinstance(ops, list):
            errors.append(f"{file}: {where}.{side}: must be a list of normalization ops.")
            continue
        for op in ops:
            cast = re.fullmatch(r"cast ([A-Z]+)", op) if isinstance(op, str) else None
            if op in NORMALIZATION_OPS or (cast and cast.group(1) in NORMALIZATION_CAST_TYPES):
                continue
            errors.append(
                f"{file}: {where}.{side}: unsupported normalization {op!r}. "
                f"Use lower, trim, or cast <{'|'.join(NORMALIZATION_CAST_TYPES)}>."
            )


def validate_physical(data: Any, file: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Return (errors, {table: {column: type}}) for one physical mapping file."""
    errors: list[str] = []
    column_types: dict[str, dict[str, str]] = {}
    if not _check_keys(data, PHYSICAL_TOP_KEYS, set(), "top level", file, errors):
        return errors, column_types

    for key in ("schema", "database", "file", "workbook"):
        _check_str(data[key], key, file, errors)

    tables = data["tables"]
    if not isinstance(tables, list) or not tables:
        errors.append(f"{file}: tables: must be a non-empty list.")
        tables = []
    seen_tables: set[str] = set()
    for t_index, table in enumerate(tables):
        where = f"tables[{t_index}]"
        if not _check_keys(table, TABLE_KEYS, set(), where, file, errors):
            continue
        name = table["name"]
        _check_str(name, f"{where}.name", file, errors)
        if name in seen_tables:
            errors.append(f"{file}: {where}: duplicate table name '{name}'. Table names must be unique.")
        seen_tables.add(name)
        _check_str(table["source_sheet"], f"{where}.source_sheet", file, errors)
        if not isinstance(table["row_count"], int) or table["row_count"] < 0:
            errors.append(f"{file}: {where}.row_count: must be a non-negative integer, found {table['row_count']!r}.")

        columns = table["columns"]
        if not isinstance(columns, list) or not columns:
            errors.append(f"{file}: {where}.columns: must be a non-empty list.")
            continue
        types_for_table: dict[str, str] = {}
        for c_index, column in enumerate(columns):
            c_where = f"{where}.columns[{c_index}]"
            if not _check_keys(column, COLUMN_KEYS, set(), c_where, file, errors):
                continue
            col_name = column["name"]
            _check_str(col_name, f"{c_where}.name", file, errors)
            if col_name in types_for_table:
                errors.append(f"{file}: {c_where}: duplicate column '{col_name}' in table '{name}'.")
            if not isinstance(column["type"], str) or not TYPE_PATTERN.match(column["type"]):
                errors.append(
                    f"{file}: {c_where}.type: unsupported type {column['type']!r} for column '{col_name}'. "
                    "Use a DuckDB type such as VARCHAR, BIGINT, DOUBLE, TIMESTAMP, or DECIMAL(p,s)."
                )
            for flag in ("nullable", "has_nulls", "is_unique"):
                _check_bool(column[flag], f"{c_where}.{flag}", file, errors)
            if column["has_nulls"] is True and column["nullable"] is False:
                errors.append(
                    f"{file}: {c_where}: column '{col_name}' has NULLs but nullable is false. "
                    "Re-profile the workbook or correct nullable."
                )
            types_for_table[col_name] = column["type"]
        column_types[name] = types_for_table

    relationships = data["relationships"]
    if not isinstance(relationships, list):
        errors.append(f"{file}: relationships: must be a list.")
        relationships = []
    seen_rels: set[str] = set()
    for r_index, rel in enumerate(relationships):
        where = f"relationships[{r_index}]"
        if not _check_keys(rel, RELATIONSHIP_KEYS, set(), where, file, errors):
            continue
        rel_name = rel["name"]
        _check_str(rel_name, f"{where}.name", file, errors)
        if rel_name in seen_rels:
            errors.append(f"{file}: {where}: duplicate relationship name '{rel_name}'.")
        seen_rels.add(rel_name)
        if rel["status"] not in RELATIONSHIP_STATUSES:
            errors.append(f"{file}: {where}.status: must be one of {sorted(RELATIONSHIP_STATUSES)}, found {rel['status']!r}.")
        if rel["cardinality"] not in CARDINALITIES:
            errors.append(
                f"{file}: {where}.cardinality: must be one of {sorted(CARDINALITIES)}, found {rel['cardinality']!r}. "
                "Use 'unknown' until the grain is profiled."
            )
        for flag in ("enforceable_now", "enabled"):
            _check_bool(rel[flag], f"{where}.{flag}", file, errors)
        if rel["enabled"] is True and rel["status"] != "approved":
            errors.append(
                f"{file}: {where}: relationship '{rel_name}' is enabled but status is {rel['status']!r}. "
                "Approve it in the requirements file or disable it."
            )
        if rel["join_policy"] not in JOIN_POLICIES:
            errors.append(f"{file}: {where}.join_policy: must be 'inner' or 'left', found {rel['join_policy']!r}.")
        _check_normalization(rel["normalization"], f"{where}.normalization", file, errors)

        ends_ok = True
        for side in ("child", "parent"):
            if not _check_keys(rel[side], END_KEYS, set(), f"{where}.{side}", file, errors):
                ends_ok = False
                continue
            table_name, col_name = rel[side]["table"], rel[side]["column"]
            if table_name not in column_types:
                ends_ok = False
                errors.append(
                    f"{file}: {where}.{side}: table '{table_name}' is not defined in tables. "
                    "Fix the table name or add the table."
                )
            elif col_name not in column_types[table_name]:
                ends_ok = False
                errors.append(
                    f"{file}: {where}.{side}: column '{col_name}' is not in table '{table_name}'. "
                    "Fix the column name."
                )

        if not isinstance(rel["checks"], list):
            errors.append(f"{file}: {where}.checks: must be a list.")
            continue
        for k_index, check in enumerate(rel["checks"]):
            k_where = f"{where}.checks[{k_index}]"
            if not _check_keys(check, CHECK_KEYS, set(), k_where, file, errors):
                continue
            if not isinstance(check["orphan_rows"], int) or check["orphan_rows"] < 0:
                errors.append(f"{file}: {k_where}.orphan_rows: must be a non-negative integer.")
            if not ends_ok:
                continue
            child_type = column_types[rel["child"]["table"]].get(rel["child"]["column"])
            parent_type = column_types[rel["parent"]["table"]].get(rel["parent"]["column"])
            if check["child_type"] != child_type:
                errors.append(
                    f"{file}: {k_where}.child_type: recorded {check['child_type']!r} but "
                    f"{rel['child']['table']}.{rel['child']['column']} is {child_type!r}. Re-profile the relationship."
                )
            if check["parent_type"] != parent_type:
                errors.append(
                    f"{file}: {k_where}.parent_type: recorded {check['parent_type']!r} but "
                    f"{rel['parent']['table']}.{rel['parent']['column']} is {parent_type!r}. Re-profile the relationship."
                )
    return errors, column_types


def validate_semantic(
    data: Any,
    file: str,
    column_types: dict[str, dict[str, str]],
    expected_database: str,
    expected_file: str,
) -> list[str]:
    """Check semantic metadata against the physical columns it names."""
    errors: list[str] = []
    if not _check_keys(data, SEMANTIC_TOP_KEYS, set(), "top level", file, errors):
        return errors
    if data["database"] != expected_database:
        errors.append(f"{file}: database: is {data['database']!r} but config expects {expected_database!r}.")
    if data["database_file"] != expected_file:
        errors.append(f"{file}: database_file: is {data['database_file']!r} but config expects {expected_file!r}.")

    for o_index, op in enumerate(data["numeric_operators"] or []):
        where = f"numeric_operators[{o_index}]"
        if not _check_keys(op, OPERATOR_KEYS, set(), where, file, errors):
            continue
        _check_str(op["operator"], f"{where}.operator", file, errors)
        if not isinstance(op["phrases"], list) or not op["phrases"]:
            errors.append(f"{file}: {where}.phrases: must be a non-empty list.")
        if "{column}" not in str(op["template"]):
            errors.append(f"{file}: {where}.template: must contain the placeholder '{{column}}'.")

    seen: set[tuple[str, str]] = set()
    for c_index, column in enumerate(data["columns"] or []):
        where = f"columns[{c_index}]"
        if not _check_keys(column, SEMANTIC_COLUMN_REQUIRED, SEMANTIC_COLUMN_OPTIONAL, where, file, errors):
            continue
        if column["database"] != expected_database or column["schema"] != expected_database:
            errors.append(f"{file}: {where}: database/schema must both be {expected_database!r}.")
        table, col = column["table"], column["column"]
        key = (table, col)
        if key in seen:
            errors.append(f"{file}: {where}: duplicate entry for {table}.{col}.")
        seen.add(key)
        if table not in column_types:
            errors.append(
                f"{file}: {where}: table '{table}' does not exist in the physical mapping. "
                "Fix the table name or regenerate semantic metadata."
            )
            continue
        if col not in column_types[table]:
            errors.append(
                f"{file}: {where}: column '{col}' does not exist in table '{table}'. "
                "Fix the column name or regenerate semantic metadata."
            )
            continue
        physical_type = column_types[table][col]
        if column["data_type"] != physical_type:
            errors.append(
                f"{file}: {where}: data_type for {table}.{col} is {column['data_type']!r} but the physical "
                f"type is {physical_type!r}. Regenerate semantic metadata."
            )
        if column["column_class"] not in COLUMN_CLASSES:
            errors.append(
                f"{file}: {where}.column_class: must be one of {sorted(COLUMN_CLASSES)}, "
                f"found {column['column_class']!r}."
            )
        if "date_operators" in column:
            bad = set(column["date_operators"]) - DATE_OPERATORS
            if bad:
                errors.append(f"{file}: {where}.date_operators: unknown operators {sorted(bad)}.")
    errors.extend(validate_metrics(data["metrics"], file, data["columns"]))
    return errors


def _is_numeric_type(column: dict) -> bool:
    """SQL arithmetic depends on the stored type, so a numeric-looking categorical column still qualifies."""
    return is_numeric(str(column.get("data_type", "")))


def validate_metrics(metrics: Any, file: str, semantic_columns: list[dict]) -> list[str]:
    errors: list[str] = []
    if not isinstance(metrics, list):
        return [f"{file}: metrics: must be a list, found {type(metrics).__name__}."]
    by_key = {(column["table"], column["column"]): column for column in semantic_columns}
    seen_names: set[str] = set()

    def check_aggregate(spec: Any, where: str) -> None:
        if not _check_keys(spec, {"table", "column", "aggregate"}, {"filters"}, where, file, errors):
            return
        key = (spec["table"], spec["column"])
        column = by_key.get(key)
        if column is None:
            errors.append(f"{file}: {where}: column {key[0]}.{key[1]} is not in the semantic columns.")
            return
        if spec["aggregate"] not in METRIC_AGGREGATES:
            errors.append(f"{file}: {where}.aggregate: must be one of {sorted(METRIC_AGGREGATES)}.")
        elif spec["aggregate"] in {"sum", "avg"} and not _is_numeric_type(column):
            errors.append(
                f"{file}: {where}: {spec['aggregate']} needs a numeric column, "
                f"but {key[0]}.{key[1]} is {column['data_type']}."
            )
        for f_index, flt in enumerate(spec.get("filters", []) or []):
            check_filter(flt, spec["table"], f"{where}.filters[{f_index}]")

    def check_filter(flt: Any, table: str, where: str) -> None:
        if not _check_keys(flt, {"column", "operator", "value"}, set(), where, file, errors):
            return
        column = by_key.get((table, flt["column"]))
        if column is None:
            errors.append(f"{file}: {where}: column {table}.{flt['column']} is not in the semantic columns.")
            return
        operator = flt["operator"]
        if operator not in METRIC_FILTER_OPERATORS:
            errors.append(f"{file}: {where}.operator: must be one of {sorted(METRIC_FILTER_OPERATORS)}.")
            return
        value = flt["value"]
        if column["column_class"] in ("categorical", "boolean"):
            if operator not in METRIC_CATEGORICAL_OPERATORS:
                errors.append(f"{file}: {where}: {operator} is not allowed on {column['column_class']} columns.")
            allowed = [str(v) for v in column.get("values", [])]
            if str(value) not in allowed:
                errors.append(f"{file}: {where}.value: {value!r} is not a stored value of {table}.{flt['column']}.")
        elif column["column_class"] == "numeric":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{file}: {where}.value: must be a number for numeric column {table}.{flt['column']}.")

    def check_ref(ref: Any, where: str) -> None:
        if not _check_keys(ref, {"table", "column"}, set(), where, file, errors):
            return
        column = by_key.get((ref["table"], ref["column"]))
        if column is None:
            errors.append(f"{file}: {where}: column {ref['table']}.{ref['column']} is not in the semantic columns.")
        elif not _is_numeric_type(column):
            errors.append(f"{file}: {where}: {ref['table']}.{ref['column']} must be numeric for a weighted average.")

    for index, metric in enumerate(metrics):
        where = f"metrics[{index}]"
        if not isinstance(metric, dict):
            errors.append(f"{file}: {where}: expected a mapping, found {type(metric).__name__}.")
            continue
        kind = metric.get("kind")
        if kind not in METRIC_KIND_KEYS:
            errors.append(f"{file}: {where}.kind: must be one of {sorted(METRIC_KIND_KEYS)}, found {kind!r}.")
            continue
        if not _check_keys(metric, METRIC_BASE_KEYS | METRIC_KIND_KEYS[kind], set(), where, file, errors):
            continue
        name = metric["name"]
        if not isinstance(name, str) or not METRIC_NAME_PATTERN.match(name):
            errors.append(f"{file}: {where}.name: must be snake_case, found {name!r}.")
        elif name in seen_names:
            errors.append(f"{file}: {where}.name: {name!r} is defined more than once.")
        seen_names.add(str(name))
        if metric["status"] not in METRIC_STATUSES:
            errors.append(f"{file}: {where}.status: must be one of {sorted(METRIC_STATUSES)}.")
        if not isinstance(metric["synonyms"], list) or not all(
            isinstance(term, str) and term.strip() for term in metric["synonyms"]
        ):
            errors.append(f"{file}: {where}.synonyms: must be a list of non-empty text.")
        for field in ("unit", "grain", "null_zero_policy", "description"):
            _check_str(metric[field], f"{where}.{field}", file, errors)
        if kind == "ratio":
            check_aggregate(metric["numerator"], f"{where}.numerator")
            check_aggregate(metric["denominator"], f"{where}.denominator")
        elif kind == "aggregate":
            check_aggregate(metric["expression"], f"{where}.expression")
        else:
            check_ref(metric["value"], f"{where}.value")
            check_ref(metric["weight"], f"{where}.weight")
    return errors


def _load_yaml(path: Path, errors: list[str]) -> Any:
    if not path.exists():
        errors.append(f"{path}: file not found. Check the path in config.yaml or generate the file.")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path}: invalid YAML ({type(exc).__name__}). Fix the syntax reported by the parser.")
        return None


def validate_database(name: str, physical_path: Path, semantic_path: Path, database_file: str) -> list[str]:
    errors: list[str] = []
    physical = _load_yaml(physical_path, errors)
    semantic = _load_yaml(semantic_path, errors)
    if physical is None or semantic is None:
        return errors
    phys_errors, column_types = validate_physical(physical, str(physical_path))
    errors.extend(phys_errors)
    if physical.get("database") != name:
        errors.append(f"{physical_path}: database: is {physical.get('database')!r} but config key is {name!r}.")
    if physical.get("file") != database_file:
        errors.append(f"{physical_path}: file: is {physical.get('file')!r} but config expects {database_file!r}.")
    errors.extend(validate_semantic(semantic, str(semantic_path), column_types, name, database_file))
    schema_mapping = semantic.get("schema_mapping") if isinstance(semantic, dict) else None
    if schema_mapping and (ROOT / schema_mapping).resolve() != physical_path.resolve():
        errors.append(f"{semantic_path}: schema_mapping: points to {schema_mapping!r}, not {physical_path.name}.")
    return errors


def validate_all(config_path: Path = CONFIG_PATH) -> list[str]:
    errors: list[str] = []
    config = _load_yaml(config_path, errors)
    if not isinstance(config, dict) or "databases" not in config:
        errors.append(f"{config_path}: must define a 'databases' mapping.")
        return errors
    for name, entry in config["databases"].items():
        physical_path = ROOT / entry["physical_mapping"]
        semantic_path = ROOT / entry["semantic_mapping"]
        errors.extend(validate_database(name, physical_path, semantic_path, entry["database_file"]))
    return errors


def main() -> int:
    errors = validate_all()
    if errors:
        print(f"Metadata validation failed with {len(errors)} problem(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Metadata validation passed for all configured databases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
