"""Stage 8: check the built SQL before it runs. Every rule is a rejection. Nothing is repaired."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from src.query.catalog import Resolved
from src.query.matcher import ValueLookup, _is_date, _is_numeric

ALLOWED_CLASSES = frozenset({"FUNCTION", "COLUMN_REF", "CONSTANT", "PARAMETER", "COMPARISON", "CONJUNCTION", "BETWEEN", "CAST"})
AGGREGATE_NAMES = frozenset({"count", "count_star", "sum", "avg", "min", "max"})
ALLOWED_FUNCTIONS = AGGREGATE_NAMES | {"lower", "date_part"}
COMPARISON_OPS = {
    "COMPARE_EQUAL": "=",
    "COMPARE_GREATERTHANOREQUALTO": ">=",
    "COMPARE_GREATERTHAN": ">",
    "COMPARE_LESSTHANOREQUALTO": "<=",
    "COMPARE_LESSTHAN": "<",
}
DATE_PARTS = ("year", "month")


class _Reject(Exception):
    pass


def validate(built: dict[str, Any], resolved: Resolved, lookup: ValueLookup | None = None, default_row_limit: int = 1000) -> dict[str, Any]:
    """Return {"valid", "reasons"}. The first failed rule stops the check."""
    try:
        _Checker(built, resolved, lookup, default_row_limit).run()
    except _Reject as reject:
        return {"valid": False, "reasons": [str(reject)]}
    return {"valid": True, "reasons": []}


class _Checker:
    def __init__(self, built: dict[str, Any], resolved: Resolved, lookup: ValueLookup | None, default_row_limit: int) -> None:
        self.sql: str = built["sql"]
        self.params: list[Any] = list(built["params"])
        self.catalog = Path(resolved.db_file).stem
        self.schema = resolved.physical["schema"]
        self.tables = {table["name"]: [col["name"] for col in table["columns"]] for table in resolved.physical["tables"]}
        self.meta = {(col["table"], col["column"]): col for col in resolved.semantic.get("columns") or []}
        self.numeric_ops = {op["operator"] for op in resolved.semantic.get("numeric_operators") or []}
        self.lookup = lookup
        self.default_row_limit = default_row_limit
        self.allowed_constants: set[int] = set()
        self.used_params: set[str] = set()

    def run(self) -> None:
        node = self._statement()
        self._check_tree_nodes(node)
        table = self._from(node.get("from_table"))
        kind = self._select(node.get("select_list") or [], table)
        self._check_empty_clauses(node)
        self._modifiers(node.get("modifiers") or [], kind)
        if node.get("where_clause") is not None:
            self._condition(node["where_clause"], table)
        self._check_literals(node)
        self._check_params(node)

    def _statement(self) -> dict[str, Any]:
        ast = json.loads(duckdb.execute("SELECT json_serialize_sql(?)", [self.sql]).fetchone()[0])
        if ast.get("error"):
            raise _Reject(f"SQL is not a single SELECT statement: {ast.get('error_message', 'unknown error')}")
        statements = ast.get("statements") or []
        if len(statements) != 1 or statements[0]["node"].get("type") != "SELECT_NODE":
            raise _Reject("SQL must be exactly one SELECT statement")
        return statements[0]["node"]

    def _check_tree_nodes(self, root: Any) -> None:
        for node in _walk(root):
            if "class" not in node:
                continue
            if node["class"] not in ALLOWED_CLASSES:
                raise _Reject(f"SQL uses a construct that is not allowed: {node['class']}")
            if node["class"] == "FUNCTION" and node.get("function_name") not in ALLOWED_FUNCTIONS:
                raise _Reject(f"SQL calls a function that is not allowed: {node.get('function_name')}")

    def _from(self, source: Any) -> str:
        if not isinstance(source, dict) or source.get("type") != "BASE_TABLE":
            raise _Reject("SQL may read one table only: joins and subqueries are not allowed")
        if source.get("catalog_name") != self.catalog or source.get("schema_name") != self.schema:
            raise _Reject(f"table must be {self.catalog}.{self.schema}.<table>")
        name = source.get("table_name")
        if name not in self.tables:
            raise _Reject(f"unknown table {name}")
        return name

    def _select(self, items: list[dict[str, Any]], table: str) -> str:
        if not items:
            raise _Reject("SELECT has no columns")
        kinds: set[str] = set()
        for item in items:
            if item.get("class") == "COLUMN_REF":
                self._column_name(item, table)
                kinds.add("row")
            elif item.get("class") == "FUNCTION" and item.get("function_name") in AGGREGATE_NAMES:
                if item.get("distinct") or item.get("filter") is not None:
                    raise _Reject("aggregates with DISTINCT or FILTER are not allowed")
                self._aggregate_arguments(item, table)
                kinds.add("aggregate")
            else:
                raise _Reject("SELECT may list columns or one aggregate only")
        if len(kinds) > 1:
            raise _Reject("a query cannot mix columns and aggregates")
        if kinds == {"aggregate"} and len(items) != 1:
            raise _Reject("an aggregate query returns one aggregate only")
        return kinds.pop()

    def _aggregate_arguments(self, item: dict[str, Any], table: str) -> None:
        children = item.get("children") or []
        if item["function_name"] == "count_star":
            if children:
                raise _Reject("COUNT(*) takes no argument")
            return
        if len(children) != 1 or children[0].get("class") != "COLUMN_REF":
            raise _Reject(f"{item['function_name'].upper()} takes one column")
        self._column_name(children[0], table)

    def _column_name(self, node: dict[str, Any], table: str) -> str:
        names = node.get("column_names") or []
        if len(names) != 1:
            raise _Reject("qualified column names are not allowed")
        if names[0] not in self.tables[table]:
            raise _Reject(f"unknown column {names[0]} in {table}")
        return names[0]

    def _check_empty_clauses(self, node: dict[str, Any]) -> None:
        if node.get("group_expressions") or node.get("having") is not None or node.get("qualify") is not None:
            raise _Reject("GROUP BY, HAVING and QUALIFY are not allowed")
        if node.get("sample") is not None or (node.get("cte_map") or {}).get("map"):
            raise _Reject("SAMPLE and WITH clauses are not allowed")

    def _modifiers(self, modifiers: list[dict[str, Any]], kind: str) -> None:
        for modifier in modifiers:
            if modifier.get("type") != "LIMIT_MODIFIER" or modifier.get("offset") is not None:
                raise _Reject("only LIMIT is allowed after the query")
        if kind == "aggregate":
            if modifiers:
                raise _Reject("an aggregate query must not have a LIMIT")
            return
        if len(modifiers) != 1:
            raise _Reject("a row query must have a LIMIT")
        limit = modifiers[0].get("limit") or {}
        value = (limit.get("value") or {}).get("value")
        if limit.get("class") != "CONSTANT" or isinstance(value, bool) or not isinstance(value, int):
            raise _Reject("LIMIT must be a whole number")
        if not 1 <= value <= self.default_row_limit:
            raise _Reject(f"LIMIT must be between 1 and {self.default_row_limit}")
        self.allowed_constants.add(id(limit))

    def _condition(self, node: dict[str, Any], table: str) -> None:
        if node.get("class") == "CONJUNCTION":
            if node.get("type") != "CONJUNCTION_AND":
                raise _Reject("only AND may join conditions")
            for child in node.get("children") or []:
                self._condition(child, table)
        elif node.get("class") == "COMPARISON":
            op = COMPARISON_OPS.get(node.get("type"))
            if op is None:
                raise _Reject("comparison operator is not allowed")
            left = self._expr(node["left"], table)
            right = self._expr(node["right"], table)
            self._comparison(left, op, right, table)
        elif node.get("class") == "BETWEEN":
            self._between(self._expr(node["input"], table), self._expr(node["lower"], table), self._expr(node["upper"], table), table)
        else:
            raise _Reject("WHERE uses a condition that is not allowed")

    def _expr(self, node: dict[str, Any], table: str) -> tuple[Any, ...]:
        cls = node.get("class")
        if cls == "COLUMN_REF":
            return ("column", self._column_name(node, table))
        if cls == "PARAMETER":
            return ("param", self._param(node))
        if cls == "CAST":
            child = node.get("child") or {}
            if (node.get("cast_type") or {}).get("id") != "DATE" or child.get("class") != "PARAMETER":
                raise _Reject("only CAST(? AS DATE) is allowed")
            return ("date_param", self._param(child))
        if cls == "FUNCTION":
            children = node.get("children") or []
            if node.get("function_name") == "lower" and len(children) == 1:
                inner = self._expr(children[0], table)
                if inner[0] in ("column", "param"):
                    return (f"lower_{inner[0]}", inner[1])
            if node.get("function_name") == "date_part" and len(children) == 2:
                part, column = children
                part_value = (part.get("value") or {}).get("value")
                if part.get("class") != "CONSTANT" or part_value not in DATE_PARTS:
                    raise _Reject("EXTRACT only accepts YEAR or MONTH")
                self.allowed_constants.add(id(part))
                inner = self._expr(column, table)
                if inner[0] != "column":
                    raise _Reject("EXTRACT must act on a column")
                return ("date_part", part_value, inner[1])
        raise _Reject("expression is not allowed in WHERE")

    def _comparison(self, left: tuple[Any, ...], op: str, right: tuple[Any, ...], table: str) -> None:
        if left[0] == "lower_column" and right[0] == "lower_param" and op == "=":
            self._text_filter(table, left[1], right[1])
        elif left[0] == "date_part" and right[0] == "param" and op == "=":
            self._date_part_filter(table, left[1], left[2], right[1])
        elif left[0] == "column" and right[0] == "date_param" and op in (">=", "<"):
            self._date_range_filter(table, left[1], op, right[1])
        elif left[0] == "column" and right[0] == "param":
            self._numeric_filter(table, left[1], op, right[1])
        else:
            raise _Reject("comparison does not match an allowed filter form")

    def _between(self, column: tuple[Any, ...], low: tuple[Any, ...], high: tuple[Any, ...], table: str) -> None:
        if column[0] != "column" or low[0] != "param" or high[0] != "param":
            raise _Reject("BETWEEN must compare a column with two values")
        name = column[1]
        meta = self._meta(table, name)
        if not _is_numeric(meta) or "BETWEEN" not in self.numeric_ops:
            raise _Reject(f"BETWEEN is not allowed on {name}")
        low_value, high_value = self._number(low[1]), self._number(high[1])
        if low_value > high_value:
            raise _Reject(f"BETWEEN on {name} needs low at most high")

    def _text_filter(self, table: str, column: str, param: str) -> None:
        meta = self._meta(table, column)
        if _is_date(meta) or _is_numeric(meta):
            raise _Reject(f"text comparison is not allowed on {column}")
        value = self._value(param)
        if not isinstance(value, str):
            raise _Reject(f"value for {column} must be text")
        self._check_stored_text(table, column, meta, value)

    def _check_stored_text(self, table: str, column: str, meta: dict[str, Any], value: str) -> None:
        if "values" in meta:
            if any(str(stored).casefold() == value.casefold() for stored in meta["values"]):
                return
            raise _Reject(f"'{value}' is not a stored value of {column}")
        if self.lookup is None:
            raise _Reject(f"cannot check '{value}' for {column}: no lookup is available")
        found = self.lookup(table, column, value)
        if len(found) == 1 and found[0].casefold() == value.casefold():
            return
        raise _Reject(f"'{value}' is not a stored value of {column}")

    def _date_part_filter(self, table: str, part: str, column: str, param: str) -> None:
        meta = self._meta(table, column)
        if not _is_date(meta) or part not in (meta.get("date_operators") or []):
            raise _Reject(f"{part} is not an allowed date operator on {column}")
        value = self._value(param)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _Reject(f"{part} needs a whole number")
        if part == "month" and not 1 <= value <= 12:
            raise _Reject("month must be from 1 to 12")

    def _date_range_filter(self, table: str, column: str, op: str, param: str) -> None:
        meta = self._meta(table, column)
        if not _is_date(meta) or "date_between" not in (meta.get("date_operators") or []):
            raise _Reject(f"date range is not allowed on {column}")
        value = self._value(param)
        if not isinstance(value, str):
            raise _Reject("a date must be text in ISO format")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise _Reject(f"'{value}' is not an ISO date") from None

    def _numeric_filter(self, table: str, column: str, op: str, param: str) -> None:
        meta = self._meta(table, column)
        if not _is_numeric(meta) or op not in self.numeric_ops:
            raise _Reject(f"operator {op} is not allowed on {column}")
        self._number(param)

    def _number(self, param: str) -> float:
        value = self._value(param)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _Reject("numeric value must be a finite number")
        return value

    def _meta(self, table: str, column: str) -> dict[str, Any]:
        return self.meta.get((table, column), {})

    def _param(self, node: dict[str, Any]) -> str:
        identifier = str(node.get("identifier"))
        if identifier not in {str(index) for index in range(1, len(self.params) + 1)}:
            raise _Reject("SQL has a parameter with no value")
        self.used_params.add(identifier)
        return identifier

    def _value(self, identifier: str) -> Any:
        return self.params[int(identifier) - 1]

    def _check_literals(self, root: Any) -> None:
        for node in _walk(root):
            if node.get("class") == "CONSTANT" and id(node) not in self.allowed_constants:
                raise _Reject("a literal value appears in the SQL text")

    def _check_params(self, root: Any) -> None:
        found = {str(node.get("identifier")) for node in _walk(root) if node.get("class") == "PARAMETER"}
        expected = {str(index) for index in range(1, len(self.params) + 1)}
        if found != expected or self.used_params != expected:
            raise _Reject("parameters do not match the SQL placeholders")


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
