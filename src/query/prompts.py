"""Prompts for the planner (analysis) and the SQL generator.

The planner reads the question and the schema, and replies with a short plain-text analysis
inside a small JSON envelope. The SQL generator reads that analysis and writes free SQL. The
prompts contain no dataset- or question-specific guidance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from src.query.catalog import Catalog, Metric
from src.query.physical_md import SchemaMap
from src.query.retrieval import RetrievedContext, _endpoint_text

PLANNER_RULES = """You analyse a user question about a database and decide whether it can be answered.

Reply with exactly one JSON object and nothing else. The first character must be "{" and the last "}".
Do not wrap it in a code fence. The object has these keys:
- "status": one of "ready", "needs_clarification", "cannot_answer".
- "analysis": plain text (not JSON, not SQL) that describes what the answer needs: the source tables and
  the columns to read, the join path when more than one table is needed, the row filters, how rows are
  grouped and what is aggregated, any window or comparison step, the sort order, and the row limit. Use the
  exact table and column names from the schema. Use the exact values from the context when a filter depends
  on a value. Use "" when the status is not "ready".
- "clarification": null when the status is "ready". Otherwise an object {"message": <why, in one or two
  sentences>, "missing": [<business terms the question uses that have no definition>]}.

Rules:
- Use only tables, columns, and metrics listed in the full schema, the context, or the approved metrics.
  Never invent names. The full schema lists every table and column, so a column the context does not
  mention can still be used when it fits the question.
- Approved metrics are the only approved metric definitions. If the question uses a business term that
  is not an approved metric and is not a plain column, return "needs_clarification" and name the term in
  clarification.missing.
- If the question cannot be answered from the schema, return "cannot_answer" and explain why in
  clarification.message.
- If the answer is clear, return "ready" with clarification null.
"""

REPAIR_INSTRUCTIONS = (
    "Reply again with one complete corrected JSON object that follows the same rules. "
    "Output exactly one JSON object and nothing else: start with \"{\", end with \"}\", "
    "and do not wrap it in a code fence."
)


def _metric_lines(metrics: Sequence[Metric]) -> str:
    if not metrics:
        return "(none approved for this database)"
    lines = []
    for metric in metrics:
        synonyms = f" synonyms={list(metric.synonyms)}" if metric.synonyms else ""
        lines.append(f"- {metric.name} (kind={metric.kind}, source={metric.source}){synonyms}")
    return "\n".join(lines)


def _full_schema_text(full_schema: SchemaMap) -> str:
    lines = []
    for table, columns in full_schema.items():
        cells = ", ".join(f"{name} ({column_type})" for name, column_type in columns.items())
        lines.append(f"- {table}: {cells}")
    return "\n".join(lines) if lines else "(no tables)"


def build_planner_prompt(
    question: str,
    context: RetrievedContext,
    metrics: Sequence[Metric] = (),
    *,
    full_schema: SchemaMap,
) -> str:
    """Return the full planner prompt. The caller sends it as a single user message."""
    return "\n\n".join(
        [
            PLANNER_RULES.strip(),
            "Approved metrics:\n" + _metric_lines(metrics),
            "Full schema (every table and column of the database):\n" + _full_schema_text(full_schema),
            "Context:\n" + context.to_prompt_text(),
            "Question:\n" + question.strip(),
        ]
    )


def build_repair_prompt(planner_prompt: str, previous_reply: str, reason: str) -> str:
    """Return the single repair prompt sent after a planner reply that does not match the envelope."""
    return "\n\n".join(
        [
            planner_prompt,
            "Your previous reply:\n" + previous_reply,
            "It was rejected because: " + reason,
            REPAIR_INSTRUCTIONS,
        ]
    )


SQL_REPAIR_INSTRUCTIONS = (
    "Reply again with one complete corrected JSON object with the keys \"sql\", \"params\", and \"assumptions\". "
    "Fix the problem named above and keep the same analysis: the SQL must answer the question described by the analysis "
    "and keep every literal valid DuckDB SQL."
)


def build_sql_repair_prompt(sql_prompt: str, previous_reply: str, reason: str) -> str:
    """Return the single repair prompt sent after generated SQL is rejected."""
    return "\n\n".join(
        [
            sql_prompt,
            "Your previous reply:\n" + previous_reply,
            "It was rejected because: " + reason,
            SQL_REPAIR_INSTRUCTIONS,
        ]
    )


SQL_GENERATOR_RULES = """You write one DuckDB SQL query that answers a user question.

Rules:
- Output exactly one JSON object with exactly the keys "sql", "params", and "assumptions".
  No prose and no markdown outside one fenced block.
- "sql" is one SELECT or WITH statement, never several statements separated by semicolons. If the question asks for
  two results, combine them into one SELECT with a CTE, a scalar subquery, or a conditional aggregate.
  Use only DuckDB functions and syntax. Do not use SQLite functions such
  as julianday. For dates use date_trunc or date_part with one of the grains day, week, month, quarter, or year,
  written inline (for example date_trunc('month', "Order_Date")). strftime is allowed with DuckDB format codes
  such as '%Y-%m'.
  To measure the time between two dates, use date_diff with a grain, for example date_diff('day', start, end).
  Do not use DATEDIFF.
- Follow the analysis. It describes the tables, joins, filters, grouping, and ordering the answer needs.
  If the analysis and the question disagree, follow the question and list the difference in "assumptions".
- Use only the tables and columns in the physical metadata below. Use the exact column names. Never invent a
  table, column, or metric.
- Give every alias a name that is not a SQL keyword. Do not alias a table or column as AS, ORDER, GROUP, TABLE,
  SELECT, or any other reserved word; use a name such as sub_avg or product_sales instead.
- Join only along the listed relationships, or along a join the analysis names when it matches a relationship.
- Reproduce each approved metric definition exactly as written when the analysis uses it. Do not change its grain.
- Every string literal and every date literal may be written inline, for example WHERE "Region" = 'West' or
  date_trunc('month', "Order_Date"). Numbers may be written inline too, for example LIMIT 10 or x > 0.
  Use "params" only when a value is easier to pass as a ? placeholder; any ? must match one entry in "params"
  in left-to-right order.
- When a filter names a value, use the exact value listed in the context or the column's listed values.
- "params" holds scalars only (string, number, boolean, or null).
- "assumptions" lists interpretations you made, as strings. Use [] when there are none.
- Each branch of a UNION must reference only its own FROM tables and aliases. A table alias from one branch is not
  visible in another branch.
- PERCENTILE_CONT(fraction) WITHIN GROUP (ORDER BY column) is an aggregate. Use it in a grouped or aggregate
  SELECT, not with OVER.
"""

SQL_OUTPUT_CONTRACT = (
    '{"sql": "SELECT ... FROM ... WHERE \\"Region\\" = ?", "params": ["West"], "assumptions": []}'
)


def _physical_metadata(catalog: Catalog) -> str:
    lines: list[str] = []
    for table, info in catalog.tables.items():
        lines.append(f"Table {table}:")
        for column_name, column in info.columns.items():
            sem = catalog.semantic_for(table, column_name)
            column_class = sem.column_class if sem else "other"
            synonyms = f", synonyms={list(sem.synonyms)}" if sem and sem.synonyms else ""
            lines.append(f"  - {column_name} ({column.type}, {column_class}{synonyms})")
    return "\n".join(lines) if lines else "(no tables)"


def _relationship_lines(catalog: Catalog) -> str:
    lines = sorted(
        f"- {rel.name}: {_endpoint_text(rel.child)} -> {_endpoint_text(rel.parent)} "
        f"(cardinality={rel.cardinality}, join={rel.join_policy}). {rel.fan_out}"
        for rel in catalog.enabled_relationships
    )
    return "\n".join(lines) or "(none)"


def _metric_definitions(catalog: Catalog) -> str:
    lines = [
        f"- {metric.name} (kind={metric.kind}): {json.dumps(metric.raw, sort_keys=True, default=str)}"
        for metric in catalog.metrics
        if metric.status == "approved"
    ]
    return "\n".join(lines) or "(none)"


def build_sql_prompt(
    question: str,
    analysis: str,
    catalog: Catalog,
    context: RetrievedContext,
    capabilities: Mapping[str, bool],
) -> str:
    """Return the SQL-generator prompt: rules, the analysis, the schema, and the question."""
    enabled = sorted(name for name, on in capabilities.items() if on is True)
    return "\n\n".join(
        [
            SQL_GENERATOR_RULES.strip(),
            "Output contract:\n" + SQL_OUTPUT_CONTRACT,
            "Dialect: DuckDB SQL with positional ? placeholders bound in order.\n"
            "Enabled features: " + (", ".join(enabled) or "(none)"),
            "Analysis:\n" + analysis.strip(),
            "Physical metadata:\n" + _physical_metadata(catalog),
            "Relationships:\n" + _relationship_lines(catalog),
            "Approved metrics:\n" + _metric_definitions(catalog),
            "Context:\n" + context.to_prompt_text(),
            "Question:\n" + question.strip(),
        ]
    )
