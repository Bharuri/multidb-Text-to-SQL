import unittest
from pathlib import Path

import yaml

from src.query.catalog import Catalog, ColumnInfo, TableInfo
from src.query.sql_ast import SqlAstError, validate_sql_ast
from src.query.sql_limits import SqlLimits, check_sql_limits

ROOT = Path(__file__).resolve().parents[1]


def _config_capabilities():
    with (ROOT / "config.yaml").open(encoding="utf-8") as fh:
        return {str(k): bool(v) for k, v in yaml.safe_load(fh)["capabilities"].items()}


ALL_ON = {flag: True for flag in _config_capabilities()}
LIMITS = SqlLimits(
    max_sql_chars=20000,
    max_ctes=20,
    max_nesting_depth=12,
    max_joins=12,
    max_window_functions=20,
    query_timeout_seconds=30,
)


def _column_info(name):
    return ColumnInfo(name=name, type="VARCHAR", nullable=False, has_nulls=False, is_unique=False)


def _catalog():
    columns = ["order_id", "customer_id", "region", "sales", "order_date"]
    return Catalog(
        key="shop",
        schema="shop",
        database_file="shop.duckdb",
        tables={
            "orders": TableInfo(
                name="orders",
                source_sheet="orders",
                row_count=10,
                columns={c: _column_info(c) for c in columns},
            ),
            "customers": TableInfo(
                name="customers",
                source_sheet="customers",
                row_count=10,
                columns={c: _column_info(c) for c in ["customer_id", "name"]},
            ),
        },
        semantic_columns=(),
        relationships=(),
        numeric_operators=(),
        fingerprint=(),
        metrics=(),
    )


def _validate(sql, params=0, limits=LIMITS, max_result_rows=1000):
    """Run the AST validator and then the D8 limit checks, as the pipeline does."""
    summary = validate_sql_ast(sql, _catalog(), params, ALL_ON)
    check_sql_limits(sql, summary, limits, max_result_rows)


class AcceptedLimitTests(unittest.TestCase):
    def test_plain_select_passes(self):
        _validate("SELECT region, sum(sales) FROM orders GROUP BY region")

    def test_structural_limit_is_allowed_inline(self):
        _validate("SELECT region FROM orders LIMIT 5")

    def test_lag_offset_and_default_are_allowed_inline(self):
        _validate("SELECT lag(sales, 2, 0) OVER (ORDER BY order_id) AS p FROM orders")

    def test_nullif_zero_guard_is_allowed_inline(self):
        _validate("SELECT sum(sales) / nullif(count(*), 0) FROM orders")

    def test_date_trunc_grain_is_allowed_inline(self):
        _validate("SELECT date_trunc('month', order_date) AS m, sum(sales) FROM orders GROUP BY 1")

    def test_date_part_grain_is_allowed_inline(self):
        _validate("SELECT date_part('year', order_date) AS y, sum(sales) FROM orders GROUP BY 1")

    def test_is_null_is_allowed(self):
        _validate("SELECT region FROM orders WHERE region IS NULL")

    def test_case_without_else_is_allowed(self):
        _validate("SELECT COUNT(CASE WHEN sales < ? THEN ? END) FROM orders", params=2)

    def test_explicit_else_null_is_allowed(self):
        _validate("SELECT COUNT(CASE WHEN sales < ? THEN ? ELSE NULL END) FROM orders", params=2)

    def test_numeric_constant_in_filter_is_allowed_inline(self):
        _validate("SELECT region FROM orders WHERE sales > 100")

    def test_numeric_constant_in_projection_is_allowed_inline(self):
        _validate("SELECT sales + 10 AS s FROM orders")

    def test_numeric_case_when_is_allowed_inline(self):
        _validate("SELECT COUNT(CASE WHEN sales < 0 THEN ? END) FROM orders", params=1)

    def test_sql_at_exact_length_limit_passes(self):
        prefix = "SELECT region FROM orders WHERE region = ? -- "
        sql = prefix + "x" * (LIMITS.max_sql_chars - len(prefix))
        self.assertEqual(len(sql), LIMITS.max_sql_chars)
        _validate(sql, params=1)


class RejectedLimitTests(unittest.TestCase):
    def assertRejected(self, sql, category, params=0, limits=LIMITS):
        with self.assertRaises(SqlAstError) as ctx:
            _validate(sql, params=params, limits=limits)
        self.assertEqual(ctx.exception.category, category)
        return ctx.exception

    def test_sql_over_length_limit_is_invalid(self):
        sql = "SELECT region FROM orders -- " + "x" * LIMITS.max_sql_chars
        self.assertRejected(sql, "invalid_sql")

    def test_too_many_ctes(self):
        limits = SqlLimits(**{**LIMITS.__dict__, "max_ctes": 2})
        sql = (
            "WITH a AS (SELECT region FROM orders), "
            "b AS (SELECT region FROM a), "
            "c AS (SELECT region FROM b) SELECT region FROM c"
        )
        self.assertRejected(sql, "unsupported", limits=limits)

    def test_cte_count_at_limit_passes(self):
        limits = SqlLimits(**{**LIMITS.__dict__, "max_ctes": 2})
        sql = "WITH a AS (SELECT region FROM orders), b AS (SELECT region FROM a) SELECT region FROM b"
        _validate(sql, limits=limits)

    def test_nesting_depth_over_limit(self):
        limits = SqlLimits(**{**LIMITS.__dict__, "max_nesting_depth": 2})
        sql = (
            "WITH a AS (WITH b AS (SELECT region FROM orders) SELECT region FROM b) "
            "SELECT region FROM a"
        )
        self.assertRejected(sql, "unsupported", limits=limits)

    def test_join_count_over_limit(self):
        limits = SqlLimits(**{**LIMITS.__dict__, "max_joins": 0})
        sql = "SELECT o.region FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
        summary = validate_sql_ast(sql, _catalog(), 0, ALL_ON)
        with self.assertRaises(SqlAstError) as ctx:
            check_sql_limits(sql, summary, limits, 1000)
        self.assertEqual(ctx.exception.category, "unsupported")

    def test_window_count_over_limit(self):
        limits = SqlLimits(**{**LIMITS.__dict__, "max_window_functions": 1})
        sql = (
            "SELECT row_number() OVER (ORDER BY order_id) AS a, "
            "row_number() OVER (ORDER BY sales) AS b FROM orders"
        )
        self.assertRejected(sql, "unsupported", limits=limits)

    def test_string_constant_in_filter_is_accepted(self):
        _validate("SELECT region FROM orders WHERE region = 'East'")

    def test_string_constant_in_case_result_is_accepted(self):
        _validate("SELECT CASE WHEN sales > ? THEN 'big' ELSE 'small' END AS s FROM orders", params=1)

    def test_null_outside_case_else_is_accepted(self):
        _validate("SELECT COALESCE(region, NULL) AS r FROM orders")

    def test_inline_string_is_accepted_alongside_placeholder(self):
        _validate("SELECT region FROM orders WHERE region = ? AND region <> 'East'", params=1)

    def test_grain_outside_allowlist_is_accepted(self):
        _validate("SELECT date_part('nanosecond', order_date) AS n FROM orders")

    def test_inline_limit_over_result_cap_is_rejected(self):
        self.assertRejected("SELECT region FROM orders LIMIT 5000", "unsafe_sql")

    def test_inline_limit_at_result_cap_passes(self):
        _validate("SELECT region FROM orders LIMIT 1000")


if __name__ == "__main__":
    unittest.main()
