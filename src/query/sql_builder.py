"""Build one DuckDB SELECT and its parameter list from a matched plan."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

AGGREGATE_FUNCTIONS = ("COUNT", "SUM", "AVG", "MIN", "MAX")


class BuildError(Exception):
    """A matched plan that cannot be turned into SQL."""


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_sql(matched: dict[str, Any], schema: str, catalog: str, default_row_limit: int = 1000) -> dict[str, Any]:
    """Return {"sql", "params"}. Every user value is a parameter. Only the integer LIMIT is written into the text."""
    conditions: list[str] = []
    params: list[Any] = []
    for raw in matched["filters"]:
        condition, values = _filter_sql(raw)
        conditions.append(condition)
        params.extend(values)

    source = f"{quote_identifier(catalog)}.{quote_identifier(schema)}.{quote_identifier(matched['table'])}"
    aggregation = matched.get("aggregation")
    if aggregation:
        head, tail = _aggregate_sql(aggregation, source), ""
    else:
        if not matched["select"]:
            raise BuildError("a row query needs at least one column")
        columns = ", ".join(quote_identifier(name) for name in matched["select"])
        limit = min(matched["limit"] or default_row_limit, default_row_limit)
        head, tail = f"SELECT {columns} FROM {source}", f" LIMIT {limit}"

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return {"sql": head + where + tail, "params": params}


def _aggregate_sql(aggregation: dict[str, Any], source: str) -> str:
    function = aggregation["function"]
    if function not in AGGREGATE_FUNCTIONS:
        raise BuildError(f"unknown aggregate function: {function}")
    if aggregation["column"] == "*":
        if function != "COUNT":
            raise BuildError(f"'*' is only valid for COUNT, not {function}")
        target = "*"
    else:
        target = quote_identifier(aggregation["column"])
    return f"SELECT {function}({target}) FROM {source}"


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
    if operator == "BETWEEN":
        low, high = value
        return f"{column} BETWEEN ? AND ?", [low, high]
    if operator == "=" and isinstance(value, str):
        # Text values are compared without case.
        return f"LOWER({column}) = LOWER(?)", [value]
    if operator in ("=", ">=", ">", "<=", "<"):
        return f"{column} {operator} ?", [value]
    raise BuildError(f"unknown operator: {operator}")
