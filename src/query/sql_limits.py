"""Complexity and parameter limits for generated SQL (design section 7.4, task D8).

Runs after AST validation (D5) and before consistency checks (D6). The limit values come
from the `limits` block of config.yaml; nothing here is dataset-specific.
"""

from dataclasses import dataclass, field
from typing import Any

from src.query.config import SqlLimits
from src.query.sql_ast import AstSummary, SqlAstError, serialize_sql

__all__ = ["SqlLimits", "check_sql_limits"]

OFFSET_WINDOW_TYPES = frozenset({"WINDOW_LAG", "WINDOW_LEAD"})
MAX_OFFSET = 1000


@dataclass
class _LimitWalk:
    ctes: int = 0
    windows: int = 0
    max_depth: int = 0
    limit_values: list[Any] = field(default_factory=list)
    offset_values: list[Any] = field(default_factory=list)


def check_sql_limits(sql: str, summary: AstSummary, limits: SqlLimits, max_result_rows: int) -> None:
    """Enforce D8 limits on SQL that already passed AST validation.

    Raises SqlAstError. Categories follow design 7.4: `invalid_sql` for length, `unsupported`
    for size and depth limits, and `unsafe_sql` for a LIMIT or window OFFSET outside its bound.
    Inline literals of any other kind are allowed.
    """
    if len(sql) > limits.max_sql_chars:
        raise SqlAstError("invalid_sql", "$", f"SQL exceeds {limits.max_sql_chars} characters")

    root = serialize_sql(sql)["statements"][0]["node"]
    walk = _LimitWalk()
    _visit(root, "$", None, 1, walk)

    if walk.ctes > limits.max_ctes:
        raise SqlAstError("unsupported", "$", f"query has more than {limits.max_ctes} CTEs")
    if walk.max_depth > limits.max_nesting_depth:
        raise SqlAstError("unsupported", "$", f"query is nested deeper than {limits.max_nesting_depth} levels")
    if summary.join_count > limits.max_joins:
        raise SqlAstError("unsupported", "$", f"query has more than {limits.max_joins} joins")
    if walk.windows > limits.max_window_functions:
        raise SqlAstError("unsupported", "$", f"query has more than {limits.max_window_functions} window functions")

    for raw in walk.limit_values:
        if not (isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= max_result_rows):
            raise SqlAstError("unsafe_sql", "$", f"LIMIT is not allowed above {max_result_rows} rows")
    for raw in walk.offset_values:
        if not (isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= MAX_OFFSET):
            raise SqlAstError("unsafe_sql", "$", f"window OFFSET is not allowed above {MAX_OFFSET}")


def _visit(value: Any, path: str, parent_type: str | None, depth: int, walk: _LimitWalk) -> None:
    if isinstance(value, dict):
        node_type = value.get("type")
        if isinstance(node_type, str):
            if node_type == "SELECT_NODE":
                walk.max_depth = max(walk.max_depth, depth)
                walk.ctes += len((value.get("cte_map") or {}).get("map", []))
                depth += 1
            elif node_type.startswith("WINDOW_"):
                walk.windows += 1
            elif node_type == "VALUE_CONSTANT":
                raw = (value.get("value") or {}).get("value")
                if parent_type == "LIMIT_MODIFIER":
                    walk.limit_values.append(raw)
                elif parent_type in OFFSET_WINDOW_TYPES:
                    walk.offset_values.append(raw)
            parent_type = node_type
        for key, child in value.items():
            _visit(child, f"{path}.{key}", parent_type, depth, walk)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _visit(child, f"{path}[{index}]", parent_type, depth, walk)
