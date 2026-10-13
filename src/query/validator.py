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
from src.query.sql_builder import build_sql

AGGREGATE_NAMES = frozenset({"count", "count_star", "sum", "avg", "min", "max"})
ARITHMETIC_NAMES = frozenset({"+", "-", "*", "/"})
TWO_VALUE_FUNCTIONS = ARITHMETIC_NAMES | {"nullif"}
ALLOWED_CLASSES = frozenset({"FUNCTION", "COLUMN_REF", "CONSTANT", "PARAMETER", "COMPARISON", "CONJUNCTION", "BETWEEN", "CAST", "WINDOW"})
ALLOWED_FUNCTIONS = AGGREGATE_NAMES | TWO_VALUE_FUNCTIONS | {"lower", "date_part"}
WINDOW_TYPES = frozenset({"WINDOW_LAG", "WINDOW_LEAD"})
SORT_TYPES = frozenset({"ORDER_DEFAULT", "ASCENDING", "DESCENDING"})
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


def validate(
    built: dict[str, Any],
    resolved: Resolved,
    lookup: ValueLookup | None = None,
    default_row_limit: int = 1000,
    matched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return {"valid", "reasons"}. The first failed rule stops the check. With matched, the SQL must also agree with the plan."""
    try:
        if matched is not None and matched.get("repeat_purchase"):
            _check_repeat(built, resolved, matched, default_row_limit)
        else:
            _Checker(built, resolved, lookup, default_row_limit).run(matched)
    except _Reject as reject:
        return {"valid": False, "reasons": [str(reject)]}
    return {"valid": True, "reasons": []}


def _check_repeat(built: dict[str, Any], resolved: Resolved, matched: dict[str, Any], default_row_limit: int) -> None:
    """The repeat-purchase template uses window functions and nested subqueries that the general rules reject. It passes only when it is exactly the template built from this matched plan."""
    expected = build_sql(matched, resolved.physical["schema"], Path(resolved.db_file).stem, default_row_limit)
    if expected is None or built.get("sql") != expected["sql"] or list(built.get("params") or []) != expected["params"]:
        raise _Reject("SQL does not match the repeat-purchase template for this plan")


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

    def run(self, matched: dict[str, Any] | None) -> None:
        node = self._statement()
        self._check_tree_nodes(node)
        if node.get("sample") is not None or (node.get("cte_map") or {}).get("map"):
            raise _Reject("SAMPLE and WITH clauses are not allowed")
        if node.get("qualify") is not None:
            raise _Reject("QUALIFY is not allowed")
        table = self._from(node.get("from_table"))
        if node.get("where_clause") is not None:
            self._condition(node["where_clause"], table)
        groups = self._group_by(node, table)
        items = node.get("select_list") or []
        aggregated = bool(groups) or node.get("having") is not None or any(_is_aggregate(child) for item in items for child in _walk(item))
        self._select(items, table, groups, aggregated)
        if node.get("having") is not None:
            self._having(node["having"], table)
        aliases = {item["alias"] for item in items if item.get("alias")}
        self._modifiers(node.get("modifiers") or [], table, groups, aliases, aggregated)
        self._check_plan(node, table, groups, matched)
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
            if node["class"] == "WINDOW" and node.get("type") not in WINDOW_TYPES:
                raise _Reject(f"window function is not allowed: {node.get('function_name')}")

    def _from(self, source: Any) -> str:
        if not isinstance(source, dict) or source.get("type") != "BASE_TABLE":
            raise _Reject("SQL may read one table only: joins and subqueries are not allowed")
        if source.get("catalog_name") != self.catalog or source.get("schema_name") != self.schema:
            raise _Reject(f"table must be {self.catalog}.{self.schema}.<table>")
        name = source.get("table_name")
        if name not in self.tables:
            raise _Reject(f"unknown table {name}")
        return name

    def _group_by(self, node: dict[str, Any], table: str) -> list[str]:
        expressions = node.get("group_expressions") or []
        if expressions and node.get("group_sets") != [list(range(len(expressions)))]:
            raise _Reject("GROUP BY may list plain columns only: ROLLUP, CUBE and GROUPING SETS are not allowed")
        names: list[str] = []
        for expression in expressions:
            if expression.get("class") != "COLUMN_REF":
                raise _Reject("GROUP BY may list plain columns only")
            names.append(self._column_name(expression, table))
        return names

    def _select(self, items: list[dict[str, Any]], table: str, groups: list[str], aggregated: bool) -> None:
        if not items:
            raise _Reject("SELECT has no columns")
        for item in items:
            self._projection(item, table, groups, aggregated)

    def _projection(self, node: dict[str, Any], table: str, groups: list[str], aggregated: bool) -> None:
        cls = node.get("class")
        name = node.get("function_name")
        if cls == "CONSTANT":
            return
        if cls == "PARAMETER":
            self._param(node)
        elif cls == "COLUMN_REF":
            column = self._column_name(node, table)
            if aggregated and column not in groups:
                raise _Reject(f"{column} must be in GROUP BY or inside an aggregate")
        elif cls == "FUNCTION" and name in AGGREGATE_NAMES:
            self._aggregate_arguments(node, table)
        elif cls == "FUNCTION" and name == "/" and _is_share_shape(node):
            self._share(node, table)
        elif cls == "FUNCTION" and name in TWO_VALUE_FUNCTIONS:
            children = node.get("children") or []
            if len(children) != 2:
                raise _Reject(f"{name} takes two values")
            for child in children:
                self._projection(child, table, groups, aggregated)
        elif cls == "WINDOW":
            self._window(node, table, groups, aggregated)
        else:
            raise _Reject("SELECT may list columns, aggregates, arithmetic and window functions only")

    def _window(self, node: dict[str, Any], table: str, groups: list[str], aggregated: bool) -> None:
        if node.get("distinct") or node.get("ignore_nulls") or node.get("filter_expr") is not None:
            raise _Reject("window functions take no DISTINCT, IGNORE NULLS or FILTER")
        if node.get("offset_expr") is not None or node.get("default_expr") is not None:
            raise _Reject("window functions take no offset or default")
        children = node.get("children") or []
        if len(children) != 1:
            raise _Reject("window functions take one value")
        self._projection(children[0], table, groups, aggregated)
        for expression in node.get("partitions") or []:
            self._projection(expression, table, groups, aggregated)
        for order in node.get("orders") or []:
            if order.get("type") not in SORT_TYPES:
                raise _Reject("window ORDER BY direction is not allowed")
            self._projection(order.get("expression") or {}, table, groups, aggregated)

    def _aggregate_arguments(self, item: dict[str, Any], table: str) -> None:
        if item.get("distinct") or item.get("filter") is not None or (item.get("order_bys") or {}).get("orders"):
            raise _Reject("aggregates with DISTINCT, FILTER or ORDER BY are not allowed")
        children = item.get("children") or []
        if item["function_name"] == "count_star":
            if children:
                raise _Reject("COUNT(*) takes no argument")
            return
        if len(children) != 1 or children[0].get("class") != "COLUMN_REF":
            raise _Reject(f"{item['function_name'].upper()} takes one column")
        self._column_name(children[0], table)

    def _share(self, node: dict[str, Any], table: str) -> None:
        count, nullif = node["children"]
        if count.get("distinct") or (count.get("order_bys") or {}).get("orders") or count.get("children"):
            raise _Reject("SHARE counts rows with an optional WHERE only")
        self._condition(count["filter"], table)
        self._aggregate_arguments(nullif["children"][0], table)
        if self._value(self._param(nullif["children"][1])) != 0:
            raise _Reject("SHARE must divide by NULLIF(COUNT(*), 0)")

    def _column_name(self, node: dict[str, Any], table: str) -> str:
        names = node.get("column_names") or []
        if len(names) != 1:
            raise _Reject("qualified column names are not allowed")
        if names[0] not in self.tables[table]:
            raise _Reject(f"unknown column {names[0]} in {table}")
        return names[0]

    def _modifiers(self, modifiers: list[dict[str, Any]], table: str, groups: list[str], aliases: set[str], aggregated: bool) -> None:
        limits: list[dict[str, Any]] = []
        for modifier in modifiers:
            kind = modifier.get("type")
            if kind == "ORDER_MODIFIER":
                for order in modifier.get("orders") or []:
                    self._order_key(order, table, groups, aliases, aggregated)
            elif kind == "LIMIT_MODIFIER" and modifier.get("offset") is None:
                limits.append(modifier)
            else:
                raise _Reject("only ORDER BY and LIMIT are allowed after the query")
        if aggregated and not groups:
            if limits:
                raise _Reject("a single-value aggregate must not have a LIMIT")
            return
        if len(limits) != 1:
            raise _Reject("a row query or a grouped aggregate must have one LIMIT")
        self._limit(limits[0])

    def _order_key(self, order: dict[str, Any], table: str, groups: list[str], aliases: set[str], aggregated: bool) -> None:
        if order.get("type") not in SORT_TYPES:
            raise _Reject("ORDER BY direction is not allowed")
        expression = order.get("expression") or {}
        names = expression.get("column_names") or []
        if expression.get("class") != "COLUMN_REF" or len(names) != 1:
            raise _Reject("ORDER BY may name a selected alias or a column")
        name = names[0]
        if name in aliases:
            return
        if name in self.tables[table] and (not aggregated or name in groups):
            return
        raise _Reject(f"ORDER BY {name} must be a selected alias or a grouped column")

    def _limit(self, modifier: dict[str, Any]) -> None:
        limit = modifier.get("limit") or {}
        value = (limit.get("value") or {}).get("value")
        if limit.get("class") != "CONSTANT" or isinstance(value, bool) or not isinstance(value, int):
            raise _Reject("LIMIT must be a whole number")
        if not 1 <= value <= self.default_row_limit:
            raise _Reject(f"LIMIT must be between 1 and {self.default_row_limit}")
        self.allowed_constants.add(id(limit))

    def _having(self, node: dict[str, Any], table: str) -> None:
        if node.get("class") == "CONJUNCTION":
            if node.get("type") != "CONJUNCTION_AND":
                raise _Reject("only AND may join HAVING conditions")
            for child in node.get("children") or []:
                self._having(child, table)
        elif node.get("class") == "COMPARISON":
            op = COMPARISON_OPS.get(node.get("type"))
            if op is None or op not in self.numeric_ops:
                raise _Reject("HAVING uses an operator that is not allowed")
            self._having_metric(node["left"], table)
            right = node["right"]
            if right.get("class") != "PARAMETER":
                raise _Reject("HAVING must compare an aggregate with a value")
            self._number(self._param(right))
        elif node.get("class") == "BETWEEN":
            if "BETWEEN" not in self.numeric_ops:
                raise _Reject("HAVING BETWEEN is not allowed")
            self._having_metric(node["input"], table)
            low, high = node["lower"], node["upper"]
            if low.get("class") != "PARAMETER" or high.get("class") != "PARAMETER":
                raise _Reject("HAVING BETWEEN must compare an aggregate with two values")
            low_value, high_value = self._number(self._param(low)), self._number(self._param(high))
            if low_value > high_value:
                raise _Reject("HAVING BETWEEN needs low at most high")
        else:
            raise _Reject("HAVING uses a condition that is not allowed")

    def _having_metric(self, node: dict[str, Any], table: str) -> None:
        if _is_share_shape(node):
            self._share(node, table)
        else:
            self._aggregate(node, table)

    def _aggregate(self, node: dict[str, Any], table: str) -> None:
        if not _is_aggregate(node):
            raise _Reject("HAVING must compare an aggregate")
        self._aggregate_arguments(node, table)

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

    def _check_plan(self, node: dict[str, Any], table: str, groups: list[str], plan: dict[str, Any] | None) -> None:
        if plan is None:
            return
        if plan.get("table") != table:
            raise _Reject(f"SQL reads {table} but the plan matched {plan.get('table')}")
        if sorted(groups) != sorted(plan.get("dimensions") or []):
            raise _Reject("GROUP BY must list exactly the plan's dimensions")
        items = node.get("select_list") or []
        calculated = bool(plan.get("calculations"))
        for metric in plan.get("metrics") or []:
            if calculated:
                found = any(_is_metric(node_, metric) for node_ in _walk(items))
            else:
                item = next((item_ for item_ in items if item_.get("alias") == metric["alias"]), None)
                found = item is not None and _is_metric(item, metric)
            if not found:
                raise _Reject(f"SELECT must include {metric['function']} of {metric['column']} as {metric['alias']}")
        for name in plan.get("select") or []:
            if not any(found.get("class") == "COLUMN_REF" and found.get("column_names") == [name] for found in items):
                raise _Reject(f"SELECT must include the plan's column {name}")

    def _check_literals(self, root: Any) -> None:
        for node in _walk(root):
            if node.get("class") == "CONSTANT" and id(node) not in self.allowed_constants:
                raise _Reject("a literal value appears in the SQL text")

    def _check_params(self, root: Any) -> None:
        found = {str(node.get("identifier")) for node in _walk(root) if node.get("class") == "PARAMETER"}
        expected = {str(index) for index in range(1, len(self.params) + 1)}
        if found != expected or self.used_params != expected:
            raise _Reject("parameters do not match the SQL placeholders")


def _is_aggregate(node: dict[str, Any]) -> bool:
    return node.get("class") == "FUNCTION" and node.get("function_name") in AGGREGATE_NAMES


def _is_metric(node: dict[str, Any], metric: dict[str, Any]) -> bool:
    if metric["function"] == "SHARE":
        return _is_share_shape(node) and _filter_columns(node["children"][0]["filter"]) == {item["column"] for item in metric.get("where") or []}
    if node.get("class") != "FUNCTION" or node.get("function_name") != _function_name(metric):
        return False
    if metric["column"] == "*":
        return not node.get("children")
    return [child.get("column_names") for child in node.get("children") or []] == [[metric["column"]]]


def _is_share_shape(node: dict[str, Any]) -> bool:
    if node.get("class") != "FUNCTION" or node.get("function_name") != "/":
        return False
    children = node.get("children") or []
    if len(children) != 2 or not _is_count_star(children[0]) or children[0].get("filter") is None:
        return False
    nullif = children[1]
    denominator = nullif.get("children") or []
    return (
        nullif.get("class") == "FUNCTION"
        and nullif.get("function_name") == "nullif"
        and len(denominator) == 2
        and _is_count_star(denominator[0])
        and denominator[1].get("class") == "PARAMETER"
    )


def _is_count_star(node: dict[str, Any]) -> bool:
    return node.get("class") == "FUNCTION" and node.get("function_name") == "count_star"


def _filter_columns(node: Any) -> set[str]:
    names: set[str] = set()
    for item in _walk(node):
        if item.get("class") == "COLUMN_REF" and len(item.get("column_names") or []) == 1:
            names.add(item["column_names"][0])
    return names


def _function_name(metric: dict[str, Any]) -> str:
    return "count_star" if metric["column"] == "*" else metric["function"].lower()


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
