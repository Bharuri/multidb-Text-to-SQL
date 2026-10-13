"""Build one DuckDB SELECT and its parameter list from a matched plan.

This is the optional deterministic path, enabled by capabilities.simple_sql_builder. build_sql returns None for plans it does not handle (calculations), and the SQL-generator LLM writes those.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src.query.shapes import METRIC_FUNCTIONS, SORT_DIRECTIONS

AGGREGATE_FUNCTIONS = METRIC_FUNCTIONS


class BuildError(Exception):
    """A matched plan that cannot be turned into SQL."""


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_sql(matched: dict[str, Any], schema: str, catalog: str, default_row_limit: int = 1000) -> dict[str, Any] | None:
    """Return {"sql", "params"}, or None when the plan needs the LLM path (calculations). A repeat-purchase plan always gets its fixed template.

    Every user value is a parameter. Only the integer LIMIT is written into the text.
    A single-value aggregate (metrics with no dimensions) has no LIMIT. Row queries and grouped aggregates always get a LIMIT, capped at default_row_limit.
    """
    if matched.get("repeat_purchase"):
        return _repeat_sql(matched, schema, catalog, default_row_limit)
    if matched.get("calculations"):
        return None

    conditions: list[str] = []
    filter_params: list[Any] = []
    for raw in matched["filters"]:
        condition, values = _filter_sql(raw)
        conditions.append(condition)
        filter_params.extend(values)

    source = f"{quote_identifier(catalog)}.{quote_identifier(schema)}.{quote_identifier(matched['table'])}"
    metrics = matched.get("metrics") or []
    dimensions = matched.get("dimensions") or []
    order_by = matched.get("order_by") or []
    group = ""

    params: list[Any] = []
    if metrics:
        expressions = {metric["alias"]: _metric_sql(metric) for metric in metrics}
        select_items = [quote_identifier(name) for name in dimensions]
        select_items += [f"{text} AS {quote_identifier(alias)}" for alias, (text, _) in expressions.items()]
        # SELECT comes before WHERE in the SQL text, so its placeholders are numbered first.
        for _, values in expressions.values():
            params.extend(values)
        if dimensions:
            group = " GROUP BY " + ", ".join(quote_identifier(name) for name in dimensions)
        head = f"SELECT {', '.join(select_items)} FROM {source}"
    else:
        if not matched.get("select"):
            raise BuildError("a row query needs at least one column")
        expressions = {}
        columns = ", ".join(quote_identifier(name) for name in matched["select"])
        head = f"SELECT {columns} FROM {source}"

    params.extend(filter_params)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    having = ""
    having_conditions: list[str] = []
    for item in matched.get("having") or []:
        expression = expressions.get(item["metric"])
        if expression is None:
            raise BuildError(f"having refers to an unknown metric: {item['metric']}")
        text, expression_values = expression
        condition, values = _comparison(text, item["operator"], item["value"])
        having_conditions.append(condition)
        params.extend(expression_values)
        params.extend(values)
    if having_conditions:
        having = f" HAVING {' AND '.join(having_conditions)}"

    ordering = ""
    if order_by:
        parts = []
        for item in order_by:
            direction = item["direction"].upper()
            if direction not in SORT_DIRECTIONS:
                raise BuildError(f"unknown sort direction: {item['direction']}")
            parts.append(f"{quote_identifier(item['ref'])} {direction}")
        ordering = " ORDER BY " + ", ".join(parts)

    limit_clause = ""
    if not metrics or dimensions:
        limit_clause = f" LIMIT {min(matched.get('limit') or default_row_limit, default_row_limit)}"

    return {"sql": head + where + group + having + ordering + limit_clause, "params": params}


def _repeat_sql(matched: dict[str, Any], schema: str, catalog: str, default_row_limit: int) -> dict[str, Any]:
    """The fixed repeat-purchase analysis. A purchase is a distinct (customer, date) pair, so same-day line items count once.

    The first purchase is a customer's earliest date and the second is their next date. A customer counts once in each dimension value on the first purchase date. Without a dimension the result is one overall row.
    """
    repeat = matched["repeat_purchase"]
    source = f"{quote_identifier(catalog)}.{quote_identifier(schema)}.{quote_identifier(matched['table'])}"
    customer = quote_identifier(repeat["customer"])
    day = quote_identifier(repeat["date"])
    dimension_list = [quote_identifier(repeat["dimension"])] if repeat.get("dimension") else []
    keys = ", ".join([customer, *dimension_list])

    ranked = (
        f"SELECT {keys}, {day}, DENSE_RANK() OVER (PARTITION BY {customer} ORDER BY {day}) AS purchase_no, "
        f"MIN({day}) OVER (PARTITION BY {customer}) AS first_date FROM {source} GROUP BY {keys}, {day}"
    )
    placed = (
        f"SELECT {keys}, purchase_no, first_date, "
        f"MIN({day}) FILTER (WHERE purchase_no = 2) OVER (PARTITION BY {customer}) AS second_date FROM ({ranked}) AS ranked"
    )
    firsts = f"SELECT {keys}, first_date, second_date FROM ({placed}) AS placed WHERE purchase_no = 1"
    select = ", ".join(
        [
            *dimension_list,
            "COUNT(*) AS customers",
            "AVG(date_diff('day', first_date, second_date)) AS average_days",
            "COUNT(*) FILTER (WHERE second_date IS NULL) AS never_returned",
        ]
    )
    grouping = f" GROUP BY {dimension_list[0]} ORDER BY {dimension_list[0]}" if dimension_list else ""
    return {"sql": f"SELECT {select} FROM ({firsts}) AS firsts{grouping} LIMIT {default_row_limit}", "params": []}


def _metric_sql(metric: dict[str, Any]) -> tuple[str, list[Any]]:
    function = metric["function"]
    if function == "SHARE":
        return _share_sql(metric)
    if function not in AGGREGATE_FUNCTIONS:
        raise BuildError(f"unknown aggregate function: {function}")
    if metric["column"] == "*":
        if function != "COUNT":
            raise BuildError(f"'*' is only valid for COUNT, not {function}")
        return f"{function}(*)", []
    return f"{function}({quote_identifier(metric['column'])})", []


def _share_sql(metric: dict[str, Any]) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    for raw in metric.get("where") or []:
        condition, condition_values = _filter_sql(raw)
        conditions.append(condition)
        values.extend(condition_values)
    if not conditions:
        raise BuildError("SHARE needs at least one where filter")
    # The zero divisor is a placeholder, so no literal enters the SQL text.
    text = f"COUNT(*) FILTER (WHERE {' AND '.join(conditions)}) / NULLIF(COUNT(*), ?)"
    return text, values + [0]


def _filter_sql(raw: dict[str, Any]) -> tuple[str, list[Any]]:
    column = quote_identifier(raw["column"])
    operator = raw["operator"]
    value = raw["value"]

    if operator == "year":
        return f"EXTRACT(YEAR FROM {column}) = ?", [value]
    if operator == "month":
        return f"EXTRACT(MONTH FROM {column}) = ?", [value]
    if operator == "year_month":
        year, month = value
        return f"EXTRACT(YEAR FROM {column}) = ? AND EXTRACT(MONTH FROM {column}) = ?", [year, month]
    if operator == "date_between":
        start, end = value
        # The end date is inclusive, so the exclusive upper bound is the day after it. It is computed here so no literal enters the SQL text.
        after_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        return f"{column} >= CAST(? AS DATE) AND {column} < CAST(? AS DATE)", [start, after_end]
    if operator == "=" and isinstance(value, str):
        # Text values are compared without case.
        return f"LOWER({column}) = LOWER(?)", [value]
    return _comparison(column, operator, value)


def _comparison(expression: str, operator: str, value: Any) -> tuple[str, list[Any]]:
    if operator == "BETWEEN":
        low, high = value
        return f"{expression} BETWEEN ? AND ?", [low, high]
    if operator in ("=", ">=", ">", "<=", "<"):
        return f"{expression} {operator} ?", [value]
    raise BuildError(f"unknown operator: {operator}")
