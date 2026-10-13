"""Stage 3 (prompt). Builds the one prompt the LLM receives. An oversized prompt is refused before any LLM call."""

from __future__ import annotations

from typing import Any

from src.query.catalog import Resolved

PLAN_RULES = """\
You translate a question into one JSON query plan for a DuckDB database. Never write SQL.

Reply with exactly one JSON object and nothing else. Its keys are only "table", "select", "aggregation", "filters" and "limit".
- Rows ("get all", "show", "list"): "select" lists the column names to return, "aggregation" is null, "limit" is null or a whole number.
- A count ("how many"): "aggregation" is {"function": "COUNT", "column": "*"} or {"function": "COUNT", "column": "<column>"}, and "select" is [].
- A total, average, minimum or maximum: "aggregation" is {"function": "SUM" | "AVG" | "MIN" | "MAX", "column": "<numeric column>"}, and "select" is [].
- For every column of the table, use "select": ["*"]. Otherwise name the columns in "select" explicitly.
- Each filter is {"column": ..., "operator": ..., "value": ...}. Use only the operators listed for that column.
  - Category or Yes/No condition: operator "=" and one stored value, copied exactly as listed. If the question has a typo, pick the closest listed value.
  - Numeric condition: one of the numeric operators listed for the column. BETWEEN takes [low, high].
  - Date column: use only that column's date operators. "year" takes an integer such as 2025. "month" takes an integer from 1 to 12, so September is 9. "year_month" takes [2025, 9]. "date_between" takes ["2025-01-01", "2025-03-31"], both dates inclusive.
  - Text column with no listed values: operator "=" and the user's word as written.
- If the question needs GROUP BY, ORDER BY, a join, or anything not in the metadata, reply {"cannot_answer": true, "reason": "<short reason>"}.
"""


class PromptError(Exception):
    """The prompt is larger than the configured limit. The LLM is not called."""

    def __init__(self, reason: str, chars: int, limit: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.chars = chars
        self.limit = limit


def build_prompt(question: str, resolved: Resolved, max_chars: int, value_threshold: int) -> str:
    text = "\n\n".join(
        [
            PLAN_RULES,
            f"Database: {resolved.database}\n\nTables and columns:\n{_metadata_block(resolved, value_threshold)}",
            f"Question: {question}",
            "Plan JSON:",
        ]
    )
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
