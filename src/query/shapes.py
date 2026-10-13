"""Shared shapes for the query pipeline: the planner plan, the matched plan, and the generated SQL.

The JSON contracts are documented in specs/retrival/design.md. Parsers in plan.py and the SQL-generation reader check replies against these shapes.
"""

from __future__ import annotations

PLAN_SCHEMA_VERSION = 1
SQL_SCHEMA_VERSION = 1

METRIC_FUNCTIONS = ("COUNT", "SUM", "AVG", "MIN", "MAX", "SHARE")
DATE_GRAINS = ("year", "quarter", "month", "year_month")
CALCULATION_TYPES = ("yoy_change", "percentage_growth")
SORT_DIRECTIONS = ("ASC", "DESC")
SQL_REPLY_KEYS = frozenset({"sql", "params"})
