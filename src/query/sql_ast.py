"""Structural validation of generated SQL using DuckDB's own parser.

The generated SQL is serialized to a JSON AST by DuckDB. Nothing is executed
and no data file is opened. The walker then checks every node against an
allowlist, resolves every table reference against the catalog, and checks
parameter placeholders. Functions are not restricted by name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import duckdb

from src.query.catalog import Catalog

NODE_ALLOWLIST = frozenset({
    "SELECT_NODE",
    "SET_OPERATION_NODE",
    "BASE_TABLE",
    "SUBQUERY",
    "JOIN",
    "STAR",
    "COLUMN_REF",
    "VALUE_CONSTANT",
    "VALUE_PARAMETER",
    "FUNCTION",
    "CASE_EXPR",
    "COMPARE_EQUAL",
    "COMPARE_NOTEQUAL",
    "COMPARE_GREATERTHAN",
    "COMPARE_GREATERTHANOREQUALTO",
    "COMPARE_LESSTHAN",
    "COMPARE_LESSTHANOREQUALTO",
    "COMPARE_BETWEEN",
    "COMPARE_IN",
    "COMPARE_NOT_IN",
    "CONJUNCTION_AND",
    "CONJUNCTION_OR",
    "OPERATOR_CAST",
    "OPERATOR_COALESCE",
    "OPERATOR_IS_NULL",
    "OPERATOR_IS_NOT_NULL",
    "WINDOW_ROW_NUMBER",
    "WINDOW_RANK",
    "WINDOW_RANK_DENSE",
    "WINDOW_LAG",
    "WINDOW_LEAD",
    "WINDOW_AGGREGATE",
    "ORDER_MODIFIER",
    "ORDER_DEFAULT",
    "ASCENDING",
    "DESCENDING",
    "LIMIT_MODIFIER",
    "DECIMAL_TYPE_INFO",
    "EMPTY",
})

ROOT_TYPES = frozenset({"SELECT_NODE", "SET_OPERATION_NODE"})

AGGREGATE_FUNCTIONS = frozenset({"sum", "avg", "count", "count_star", "min", "max", "median"})

FEATURE_BY_FUNCTION = {
    "quantile_cont": "percentiles",
    "date_trunc": "date_grains",
    "date_diff": "date_grains",
    "date_part": "date_grains",
    "strftime": "date_grains",
    "+": "calculations",
    "-": "calculations",
    "*": "calculations",
    "/": "calculations",
    "nullif": "calculations",
}

RANKING_WINDOW_TYPES = frozenset({"WINDOW_ROW_NUMBER", "WINDOW_RANK", "WINDOW_RANK_DENSE"})


class SqlAstError(Exception):
    """Raised when generated SQL fails structural validation."""

    def __init__(self, category: str, path: str, reason: str) -> None:
        super().__init__(f"{category} at {path}: {reason}")
        self.category = category
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class AstSummary:
    tables: tuple[str, ...]
    cte_names: tuple[str, ...]
    functions: tuple[str, ...]
    parameter_count: int
    join_count: int
    features: tuple[str, ...]


@dataclass
class _Walk:
    catalog: Catalog
    capabilities: Mapping[str, bool]
    cte_names: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)
    features: set[str] = field(default_factory=set)
    parameter_count: int = 0
    join_count: int = 0


_SERIALIZER = None


def _serializer() -> duckdb.DuckDBPyConnection:
    global _SERIALIZER
    if _SERIALIZER is None:
        conn = duckdb.connect(":memory:")
        conn.execute("LOAD json")
        _SERIALIZER = conn
    return _SERIALIZER


def serialize_sql(sql: str) -> dict:
    """Return DuckDB's parse tree for one SQL string. Parses only; nothing is executed."""
    try:
        row = _serializer().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    except duckdb.Error as exc:
        raise SqlAstError("invalid_sql", "$", f"DuckDB could not parse the SQL ({type(exc).__name__})") from None
    tree = json.loads(row[0])
    if tree.get("error"):
        message = str(tree.get("error_message", ""))
        if "Only SELECT statements" in message:
            raise SqlAstError("unsafe_sql", "$", "Only a single SELECT or WITH statement is allowed")
        raise SqlAstError("invalid_sql", "$", "DuckDB could not parse the SQL" + _parser_hint(tree.get("error_message")))
    return tree


def _parser_hint(raw) -> str:
    """Return DuckDB's syntax message so a repair can fix the token it names.

    A message containing a quote may echo a string literal, so it is dropped.
    """
    text = " ".join(str(raw or "").split())
    if not text or "'" in text:
        return ""
    return f": {text[:200]}"


def _require(feature: str, path: str, walk: _Walk) -> None:
    walk.features.add(feature)
    if not walk.capabilities.get(feature, False):
        raise SqlAstError("unsupported", path, f"SQL feature '{feature}' is not enabled")


def _contains_type(value, node_type: str) -> bool:
    if isinstance(value, dict):
        if value.get("type") == node_type:
            return True
        return any(_contains_type(child, node_type) for child in value.values())
    if isinstance(value, list):
        return any(_contains_type(child, node_type) for child in value)
    return False


def _has_outer_aggregate(value) -> bool:
    """True when an aggregate call appears outside any subquery in the given expression."""
    if isinstance(value, dict):
        if value.get("type") == "SUBQUERY":
            return False
        if value.get("type") == "FUNCTION" and str(value.get("function_name", "")).lower() in AGGREGATE_FUNCTIONS:
            return True
        return any(_has_outer_aggregate(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_outer_aggregate(child) for child in value)
    return False


def _check_node(node: dict, path: str, walk: _Walk, scope: frozenset[str]) -> None:
    node_type = node["type"]
    if node_type not in NODE_ALLOWLIST:
        raise SqlAstError("unsafe_sql", path, f"SQL construct '{node_type}' is not allowed")
    if node_type in _CTE_HOLDER_TYPES and _cte_names(node):
        _require("ctes", path, walk)

    if node_type == "SELECT_NODE":
        if node.get("sample") is not None:
            raise SqlAstError("unsafe_sql", path, "TABLESAMPLE is not allowed")
        if _has_outer_aggregate(node.get("where_clause")):
            raise SqlAstError(
                "invalid_sql", path, "aggregate functions are not allowed in WHERE; move them to HAVING or a CTE"
            )
        if node.get("group_expressions"):
            _require("group_by", path, walk)
        if node.get("having") is not None:
            _require("having", path, walk)
    elif node_type == "BASE_TABLE":
        _check_base_table(node, path, walk, scope)
    elif node_type == "FUNCTION":
        name = str(node.get("function_name", "")).lower()
        walk.functions.add(name)
        if name in FEATURE_BY_FUNCTION:
            _require(FEATURE_BY_FUNCTION[name], path, walk)
        if name in AGGREGATE_FUNCTIONS and _contains_type(node.get("children"), "CASE_EXPR"):
            _require("conditional_aggregation", path, walk)
    elif node_type == "CASE_EXPR":
        _require("case_expressions", path, walk)
    elif node_type == "SUBQUERY":
        _require("subqueries", path, walk)
    elif node_type == "SET_OPERATION_NODE":
        _require("set_operations", path, walk)
    elif node_type == "ORDER_MODIFIER":
        _require("order_by", path, walk)
    elif node_type.startswith("WINDOW_"):
        _require("windows", path, walk)
        if node_type in RANKING_WINDOW_TYPES:
            _require("ranking", path, walk)
    elif node_type == "VALUE_PARAMETER":
        walk.parameter_count += 1
        identifier = str(node.get("identifier", ""))
        if not identifier.isdigit():
            raise SqlAstError("invalid_sql", path, "only positional parameters (?) are allowed")
    elif node_type == "JOIN":
        walk.join_count += 1
        _require("joins", path, walk)



_CTE_HOLDER_TYPES = ("SELECT_NODE", "SET_OPERATION_NODE")


def _cte_names(node: dict) -> list[str]:
    entries = (node.get("cte_map") or {}).get("map", [])
    return [str(entry["key"]) for entry in entries if isinstance(entry, dict) and "key" in entry]


def _check_base_table(node: dict, path: str, walk: _Walk, scope: frozenset[str]) -> None:
    if node.get("sample") is not None:
        raise SqlAstError("unsafe_sql", path, "TABLESAMPLE is not allowed")
    if node.get("at_clause") is not None:
        raise SqlAstError("unsafe_sql", path, "time travel (AT) is not allowed")
    if node.get("catalog_name"):
        raise SqlAstError("unsafe_sql", path, "catalog-qualified table names are not allowed")

    table = str(node.get("table_name", ""))
    schema = str(node.get("schema_name") or "")
    if table in scope and not schema:
        return

    canonical = _catalog_table(table, walk.catalog.tables)
    if canonical is None:
        raise SqlAstError("unsafe_sql", path, f"table {table!r} is not in the selected dataset")
    if schema and schema.lower() != walk.catalog.schema.lower():
        raise SqlAstError("unsafe_sql", path, f"schema {schema!r} is not in the selected dataset")
    walk.tables.add(canonical)


def _catalog_table(name: str, tables) -> str | None:
    """Return the catalog spelling of a table name. DuckDB identifiers are case-insensitive."""
    if name in tables:
        return name
    matches = [known for known in tables if known.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


def _walk(value, path: str, walk: _Walk, scope: frozenset[str]) -> None:
    if isinstance(value, dict):
        node_type = value.get("type")
        if isinstance(node_type, str):
            _check_node(value, path, walk, scope)
            if node_type in _CTE_HOLDER_TYPES:
                names = _cte_names(value)
                walk.cte_names.update(names)
                scope = scope | frozenset(names)
        for key, child in value.items():
            _walk(child, f"{path}.{key}", walk, scope)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", walk, scope)


def validate_sql_ast(
    sql: str,
    catalog: Catalog,
    param_count: int,
    capabilities: Mapping[str, bool],
) -> AstSummary:
    """Validate generated SQL structure against the catalog and enabled capabilities.

    Raises SqlAstError with category invalid_sql, unsafe_sql, or unsupported.
    A capability missing from `capabilities` counts as disabled.
    """
    tree = serialize_sql(sql)
    statements = tree.get("statements", [])
    if len(statements) != 1:
        raise SqlAstError("unsafe_sql", "$", "exactly one SQL statement is allowed")

    root = statements[0].get("node", {})
    if root.get("type") not in ROOT_TYPES:
        raise SqlAstError("unsafe_sql", "$", "only a SELECT or WITH statement is allowed")

    walk = _Walk(catalog=catalog, capabilities=capabilities)
    _walk(root, "$", walk, frozenset())

    if walk.parameter_count != param_count:
        raise SqlAstError(
            "invalid_sql", "$",
            f"SQL has {walk.parameter_count} parameter placeholders but {param_count} values were supplied",
        )

    return AstSummary(
        tables=tuple(sorted(walk.tables)),
        cte_names=tuple(sorted(walk.cte_names)),
        functions=tuple(sorted(walk.functions)),
        parameter_count=walk.parameter_count,
        join_count=walk.join_count,
        features=tuple(sorted(walk.features)),
    )
