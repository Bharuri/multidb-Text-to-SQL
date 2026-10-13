"""Stage 3 (prompt). Builds the one prompt the LLM receives. An oversized prompt is refused before any LLM call."""

from __future__ import annotations

from typing import Any

from src.query.catalog import Resolved

PLAN_RULES = """\
You translate a question into one JSON query plan for a DuckDB database. Never write SQL.

Reply with exactly one JSON object and nothing else. Its keys are only "table", "select", "dimensions", "metrics", "filters", "group_by", "having", "order_by", "calculations" and "limit". Every key except "repeat_purchase" is required, and "repeat_purchase" may be left out. Use null or [] for a part you do not need.

Supported features: repeat-purchase analysis (first to second purchase), row lists, counts and totals, several metrics, grouping by dimension columns, filtering groups with having, sorting, year-over-year or percentage-growth calculations, and SHARE metrics (the fraction of rows that match a condition). Joins and anything not in the metadata are not supported.

- Rows ("show", "list", "get all"): "select" lists the column names to return, or ["*"] for every column. "metrics" is null or [].
- One total, average, minimum or maximum with no grouping: one metric, "dimensions" is [], "group_by" is [], and "limit" and "order_by" are null or [].
- Grouped results ("per", "by", "for each"): "dimensions" lists the grouping columns, "metrics" lists the aggregates, and "group_by" is the same list as "dimensions". "select" is [].
- Each metric is {"function": "COUNT" | "SUM" | "AVG" | "MIN" | "MAX", "column": "<column>", "alias": "<short name>"}. Use "*" as the column only for COUNT. Use a numeric column for SUM, AVG, MIN and MAX. Aliases are unique and must not equal a column name.
- A share metric is {"function": "SHARE", "column": "*", "alias": "<short name>", "where": [<filters>]}. Its value is the fraction of the rows in the table that match every filter in "where" (0 to 1). Use it for "what share", "what percentage of", or "how many percent" questions. "where" is allowed only with SHARE. Its filters use the same format as the filters list below. A share metric can be used in "having" and "order_by".
- "having" filters groups: [{"metric": "<alias>", "operator": "<numeric operator listed for the table>", "value": <number>}]. Use it only with grouped results.
- "order_by" sorts: [{"ref": "<metric alias or dimension column>", "direction": "ASC" | "DESC"}].
- "calculations" compares a period with an earlier one: [{"type": "yoy_change" | "percentage_growth", "metric": "<alias>", "date_column": "<date column>", "grain": "year" | "quarter" | "month" | "year_month", "year": <integer>}]. Use it only with metrics.
- "limit" is null or a whole number. Use it for grouped results and row lists.
- Each filter is {"column": ..., "operator": ..., "value": ...}. Use only the operators listed for that column.
  - Category or Yes/No condition: operator "=" and one stored value, copied exactly as listed. If the question has a typo, pick the closest listed value.
  - Numeric condition: one of the numeric operators listed for the column. BETWEEN takes [low, high].
  - Date column: use only that column's date operators. "year" takes an integer such as 2025. "month" takes an integer from 1 to 12, so September is 9. "year_month" takes [2025, 9]. "date_between" takes ["2025-01-01", "2025-03-31"], both dates inclusive.
  - Text column with no listed values: operator "=" and the user's word as written.
- A business definition such as "loss = Orders.Profit < 0" names a row condition. When the question uses that word, write the condition as the "where" list of a SHARE metric, and give the column, operator and value from the definition.
- Repeat purchases ("second purchase", "came back", "never returned", "days between a customer's first and second purchase"): set "repeat_purchase" to {"customer": "<entity column>", "date": "<date column>", "dimension": "<column of the first purchase to group by>"}, and set every other key to null or []. Use "dimension": null for one overall figure. A purchase is a distinct order date for a customer.
- If the question needs a join, or uses a concept or column that is not in the metadata or business definitions, reply {"cannot_answer": true, "reason": "<short reason>"}.
"""


class PromptError(Exception):
    """The prompt is larger than the configured limit. The LLM is not called."""

    def __init__(self, reason: str, chars: int, limit: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.chars = chars
        self.limit = limit


def build_prompt(
    question: str,
    resolved: Resolved,
    max_chars: int,
    value_threshold: int,
    business_definitions: str = "",
) -> str:
    sections = [
        PLAN_RULES,
        f"Database: {resolved.database}\n\nTables and columns:\n{_metadata_block(resolved, value_threshold)}",
    ]
    if business_definitions:
        sections.append(f"Business definitions:\n{business_definitions}")
    sections += [f"Question: {question}", "Plan JSON:"]
    text = "\n\n".join(sections)
    if len(text) > max_chars:
        raise PromptError(
            f"The prompt is {len(text)} characters, over the limit of {max_chars}. Ask a shorter question or use a smaller database.",
            len(text),
            max_chars,
        )
    return text


def _metadata_block(resolved: Resolved, value_threshold: int) -> str:
    semantic_columns = {(column["table"], column["column"]): column for column in resolved.semantic.get("columns") or []}
    numeric_operators = [str(op["operator"]) for op in resolved.semantic.get("numeric_operators") or []]
    lines: list[str] = []
    for table in resolved.physical["tables"]:
        lines.append(f"Table {table['name']}")
        for column in table["columns"]:
            meta = semantic_columns.get((table["name"], column["name"]), {})
            lines.append(_column_line(column, meta, numeric_operators, value_threshold))
    return "\n".join(lines)


def _column_line(column: dict[str, Any], meta: dict[str, Any], numeric_operators: list[str], value_threshold: int) -> str:
    column_class = meta.get("column_class", "other")
    parts = [f"  - {column['name']} ({column['type']}, {column_class})"]
    date_operators = meta.get("date_operators") or []
    if date_operators:
        parts.append(f"date operators: {', '.join(str(op) for op in date_operators)}")
    if column_class == "numeric":
        parts.append(f"numeric operators: {', '.join(numeric_operators)}")
    values = meta.get("values") or []
    if values and len(values) <= value_threshold:
        parts.append(f"stored values: {', '.join(str(value) for value in values)}")
    return "; ".join(parts)
