"""Stage 8 (SQL prompt). Builds the prompt for the SQL-generator LLM from the matched plan. An oversized prompt is refused before any LLM call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.query.catalog import Resolved
from src.query.prompt import PromptError

SQL_RULES = """\
You write one DuckDB SQL query for a matched query plan. The plan is already validated. Follow it exactly.

Reply with exactly one JSON object and nothing else. Its keys are only "sql" and "params". "sql" is one SELECT statement. "params" is the list of values for the "?" placeholders, in the order they appear in "sql".

Rules:
- Use only the tables and columns in the schema below, and only the metrics, dimensions, filters, grouping, having, ordering and calculations in the plan. Do not invent names.
- Write every value as a "?" placeholder and put the value in "params". This covers filter and having values, the 0 inside NULLIF, and any constant multiplier such as 100 for a percentage. The only literals allowed in the SQL are the LIMIT integer and the YEAR or MONTH keyword in EXTRACT. Never paste values into the SQL text.
- Quote every identifier with double quotes, for example "Orders"."Sales". Compare text with equality as LOWER("column") = LOWER(?), never a plain "column" = ?.
- FROM must name the table exactly as the schema shows it after "FROM:", as catalog.schema.table. Do not use a bare table name.
- Write no joins, subqueries, CTEs, UNION, or statements other than one SELECT.
- Use aggregate functions only from the plan. Use window functions (OVER, LAG or LEAD) only for the plan's calculations, and use NULLIF(denominator, ?) with 0 as a parameter to avoid division by zero.
- GROUP BY lists the plan's group_by columns. HAVING applies the plan's having items. ORDER BY applies the plan's order_by items, using the metric alias or dimension column.
- Add a LIMIT only as follows. Use the plan's limit when it is set. If it is null and the query returns rows or groups, use LIMIT {default_row_limit}. A single-value aggregate has no LIMIT. Write LIMIT as a literal integer, not a placeholder.
- A SHARE metric is COUNT(*) FILTER (WHERE <its where filters>) / NULLIF(COUNT(*), ?). Write its filter values as "?" placeholders, and put the 0 for NULLIF in "params" in that order. Do not use FILTER for any other metric.
- Date filters: "year" is EXTRACT(YEAR FROM column) = ?, "month" is EXTRACT(MONTH FROM column) = ?, "year_month" is the year and month pair, and "date_between" is column BETWEEN ? AND ? with both dates inclusive.
"""


def build_sql_prompt(
    question: str,
    matched: dict[str, Any],
    resolved: Resolved,
    max_chars: int,
    default_row_limit: int,
    business_definitions: str = "",
) -> str:
    plan = {key: value for key, value in matched.items() if key != "matches"}
    sections = [
        SQL_RULES.replace("{default_row_limit}", str(default_row_limit)),
        f"Database: {resolved.database}\n\nSchema:\n{_schema_block(resolved)}",
    ]
    if business_definitions:
        sections.append(f"Business definitions:\n{business_definitions}")
    sections += [
        f"Question: {question}",
        f"Matched plan JSON:\n{json.dumps(plan, sort_keys=True, default=str)}",
        'SQL JSON (reply with {"sql": ..., "params": [...]} only):',
    ]
    text = "\n\n".join(sections)
    if len(text) > max_chars:
        raise PromptError(
            f"The SQL prompt is {len(text)} characters, over the limit of {max_chars}. Ask a shorter question or use a smaller database.",
            len(text),
            max_chars,
        )
    return text


def _schema_block(resolved: Resolved) -> str:
    catalog = Path(resolved.db_file).stem
    schema = resolved.physical["schema"]
    lines: list[str] = []
    for table in resolved.physical["tables"]:
        lines.append(f"Table {table['name']} (FROM: {catalog}.{schema}.{table['name']})")
        for column in table["columns"]:
            lines.append(f"  {column['name']} {column.get('type', '')}".rstrip())
    return "\n".join(lines)
