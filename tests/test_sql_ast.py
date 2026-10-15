import unittest
from pathlib import Path

import yaml

from src.query.catalog import Catalog, ColumnInfo, TableInfo
from src.query.sql_ast import (
    SqlAstError,
    _Walk,
    _walk,
    serialize_sql,
    validate_sql_ast,
)

ROOT = Path(__file__).resolve().parents[1]


def _config_capabilities():
    with (ROOT / "config.yaml").open(encoding="utf-8") as fh:
        return {str(k): bool(v) for k, v in yaml.safe_load(fh)["capabilities"].items()}


CONFIG_CAPS = _config_capabilities()
ALL_ON = {flag: True for flag in CONFIG_CAPS}


def _column_info(name):
    return ColumnInfo(name=name, type="VARCHAR", nullable=False, has_nulls=False, is_unique=False)


def _table(name, columns):
    return TableInfo(
        name=name,
        source_sheet=name,
        row_count=10,
        columns={c: _column_info(c) for c in columns},
    )


def _catalog():
    return Catalog(
        key="shop",
        schema="shop",
        database_file="shop.duckdb",
        tables={
            "orders": _table("orders", ["order_id", "customer_id", "region", "sales"]),
            "customers": _table("customers", ["customer_id", "name"]),
        },
        semantic_columns=(),
        relationships=(),
        numeric_operators=(),
        fingerprint=(),
        metrics=(),
    )


class AcceptedSqlTests(unittest.TestCase):
    def check(self, sql, params=0):
        return validate_sql_ast(sql, _catalog(), params, ALL_ON)

    def test_simple_select(self):
        summary = self.check("SELECT region, sales FROM orders")
        self.assertEqual(summary.tables, ("orders",))
        self.assertEqual(summary.parameter_count, 0)
        self.assertEqual(summary.features, ())

    def test_aggregates_group_by_and_distinct(self):
        summary = self.check(
            "SELECT region, sum(sales) AS total, count(DISTINCT customer_id) AS n, count(*) AS rows "
            "FROM orders GROUP BY region"
        )
        self.assertIn("sum", summary.functions)
        self.assertIn("count", summary.functions)
        self.assertIn("count_star", summary.functions)

    def test_cte_chain_reports_cte_names(self):
        summary = self.check(
            "WITH base AS (SELECT region, sales FROM orders), "
            "agg AS (SELECT region, sum(sales) AS total FROM base GROUP BY region) "
            "SELECT * FROM agg"
        )
        self.assertEqual(summary.tables, ("orders",))
        self.assertEqual(set(summary.cte_names), {"base", "agg"})

    def test_cte_may_shadow_a_catalog_table_name(self):
        summary = self.check("WITH orders AS (SELECT 1 AS x) SELECT x FROM orders")
        self.assertEqual(summary.tables, ())

    def test_subquery_in_from_when_enabled(self):
        summary = self.check("SELECT t.region FROM (SELECT region FROM orders) AS t")
        self.assertEqual(summary.tables, ("orders",))
        self.assertIn("subqueries", summary.features)

    def test_set_operation_when_enabled(self):
        summary = self.check(
            "SELECT region FROM orders UNION ALL SELECT name FROM customers"
        )
        self.assertEqual(set(summary.tables), {"orders", "customers"})
        self.assertIn("set_operations", summary.features)

    def test_cte_on_union_can_reference_earlier_cte(self):
        summary = self.check(
            "WITH a AS (SELECT region FROM orders), b AS (SELECT region FROM a) "
            "SELECT region FROM b UNION ALL SELECT region FROM orders"
        )
        self.assertEqual(set(summary.cte_names), {"a", "b"})
        self.assertEqual(summary.tables, ("orders",))

    def test_case_coalesce_between_in_and_null_checks(self):
        self.check(
            "SELECT CASE WHEN sales BETWEEN 1 AND 10 THEN 'low' ELSE 'high' END AS band, "
            "coalesce(region, 'none') AS r FROM orders "
            "WHERE region IN ('East', 'West') AND sales IS NOT NULL"
        )

    def test_window_row_number(self):
        self.check("SELECT region, row_number() OVER (ORDER BY sales DESC) AS rk FROM orders")

    def test_join_is_structurally_allowed_here_and_counted(self):
        summary = self.check(
            "SELECT o.region, c.name FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
        )
        self.assertEqual(set(summary.tables), {"orders", "customers"})
        self.assertEqual(summary.join_count, 1)

    def test_join_condition_may_use_lower_and_trim(self):
        summary = self.check(
            "SELECT o.region, c.name FROM orders o JOIN customers c "
            "ON lower(trim(o.customer_id)) = lower(trim(c.customer_id))"
        )
        self.assertEqual(summary.join_count, 1)

    def test_schema_qualified_catalog_table_is_allowed(self):
        summary = self.check("SELECT region FROM shop.orders")
        self.assertEqual(summary.tables, ("orders",))

    def test_positional_parameters_are_counted(self):
        summary = self.check("SELECT sales FROM orders WHERE region = ? AND sales > ?", params=2)
        self.assertEqual(summary.parameter_count, 2)


class RejectedSqlTests(unittest.TestCase):
    def assertRejected(self, sql, category="unsafe_sql", params=0):
        with self.assertRaises(SqlAstError) as ctx:
            validate_sql_ast(sql, _catalog(), params, ALL_ON)
        self.assertEqual(ctx.exception.category, category)
        return ctx.exception

    def test_aggregate_in_where_is_rejected_for_repair(self):
        error = self.assertRejected(
            'SELECT region FROM orders WHERE COUNT(DISTINCT region) > 2 GROUP BY region', category="invalid_sql"
        )
        self.assertIn("not allowed in WHERE", error.reason)

    def test_aggregate_inside_subquery_in_where_is_accepted(self):
        summary = AcceptedSqlTests().check(
            "SELECT region FROM orders WHERE region IN (SELECT region FROM orders GROUP BY region HAVING COUNT(*) > 2)"
        )
        self.assertEqual(summary.tables, ("orders",))

    def test_file_reader_table_function_is_rejected(self):
        self.assertRejected("SELECT * FROM read_csv('x.csv')")

    def test_unknown_table_rejection_names_the_table(self):
        error = self.assertRejected("SELECT region FROM not_a_table")
        self.assertIn("'not_a_table' is not in the selected dataset", error.reason)

    def test_table_name_case_resolves_to_catalog_spelling(self):
        summary = AcceptedSqlTests().check("SELECT region FROM ORDERS")
        self.assertEqual(summary.tables, ("orders",))

    def test_bare_file_path_table_is_rejected(self):
        self.assertRejected("SELECT * FROM 'x.csv'")

    def test_unknown_table_is_rejected(self):
        self.assertRejected("SELECT * FROM secret_table")

    def test_other_schema_is_rejected(self):
        self.assertRejected("SELECT * FROM other.orders")

    def test_lower_and_trim_are_accepted_outside_join_conditions(self):
        validate_sql_ast("SELECT region FROM orders WHERE trim(region) = 'East'", _catalog(), 0, ALL_ON)

    def test_other_catalog_is_rejected(self):
        self.assertRejected("SELECT * FROM memory.shop.orders")

    def test_tablesample_is_rejected(self):
        self.assertRejected("SELECT * FROM orders USING SAMPLE 10 PERCENT")

    def test_pivot_is_rejected(self):
        self.assertRejected("PIVOT orders ON region USING sum(sales)")

    def test_values_list_is_rejected(self):
        self.assertRejected("SELECT * FROM (VALUES (1), (2)) AS t(x)")

    def test_create_is_rejected_as_non_select(self):
        self.assertRejected("CREATE TABLE t AS SELECT 1")

    def test_attach_is_rejected(self):
        self.assertRejected("ATTACH 'other.duckdb'")

    def test_multiple_statements_are_rejected(self):
        self.assertRejected("SELECT 1; SELECT 2")

    def test_file_reader_inside_cte_body_is_rejected(self):
        self.assertRejected(
            "WITH a AS (SELECT * FROM secret_table) SELECT * FROM a"
        )

    def test_cte_name_does_not_leak_out_of_its_scope(self):
        self.assertRejected(
            "SELECT * FROM (WITH x AS (SELECT 1 AS a) SELECT a FROM x) AS t, x"
        )

    def test_named_parameter_is_rejected(self):
        self.assertRejected("SELECT sales FROM orders WHERE region = $r", category="invalid_sql", params=1)

    def test_placeholder_count_mismatch_is_rejected(self):
        self.assertRejected("SELECT sales FROM orders WHERE region = ?", category="invalid_sql", params=0)

    def test_extra_values_are_rejected(self):
        self.assertRejected("SELECT 1", category="invalid_sql", params=2)

    def test_syntax_error_is_invalid_sql(self):
        self.assertRejected("SELEC * FROM orders", category="invalid_sql")


class WalkerFailClosedTests(unittest.TestCase):
    def test_unknown_node_type_fails_closed(self):
        walk = _Walk(catalog=_catalog(), capabilities=ALL_ON)
        with self.assertRaises(SqlAstError) as ctx:
            _walk({"type": "SOMETHING_NEW"}, "$", walk, frozenset())
        self.assertEqual(ctx.exception.category, "unsafe_sql")
        self.assertIn("SOMETHING_NEW", ctx.exception.reason)


class CapabilityGateTests(unittest.TestCase):
    """D7: each SQL feature is accepted only when its config flag is on."""

    def assertGated(self, sql, caps, flag, category="unsupported"):
        with self.assertRaises(SqlAstError) as ctx:
            validate_sql_ast(sql, _catalog(), 0, caps)
        self.assertEqual(ctx.exception.category, category)
        self.assertIn(flag, ctx.exception.reason)

    def accepts(self, sql, caps, feature=None):
        summary = validate_sql_ast(sql, _catalog(), 0, caps)
        if feature is not None:
            self.assertIn(feature, summary.features)
        return summary

    def test_config_defaults_match_design(self):
        self.assertTrue(CONFIG_CAPS["ctes"])
        self.assertTrue(CONFIG_CAPS["conditional_aggregation"])
        self.assertTrue(CONFIG_CAPS["ranking"])
        self.assertTrue(CONFIG_CAPS["subqueries"])
        self.assertTrue(CONFIG_CAPS["set_operations"])
        self.assertTrue(CONFIG_CAPS["joins"])
        self.assertTrue(CONFIG_CAPS["percentiles"])

    def test_default_config_accepts_cte_and_plain_aggregate(self):
        self.accepts(
            "WITH agg AS (SELECT region, sum(sales) AS total FROM orders GROUP BY region) "
            "SELECT * FROM agg",
            CONFIG_CAPS,
            feature="ctes",
        )

    def test_default_config_accepts_median_without_a_flag(self):
        self.accepts("SELECT median(sales) AS m FROM orders", CONFIG_CAPS)

    def test_default_config_accepts_derived_table(self):
        self.accepts(
            "SELECT t.region FROM (SELECT region FROM orders) AS t", CONFIG_CAPS, feature="subqueries"
        )

    def test_derived_table_rejected_when_subqueries_disabled(self):
        self.assertGated(
            "SELECT t.region FROM (SELECT region FROM orders) AS t",
            dict(CONFIG_CAPS, subqueries=False),
            "subqueries",
        )

    def test_default_config_accepts_union(self):
        self.accepts(
            "SELECT region FROM orders UNION ALL SELECT region FROM orders",
            CONFIG_CAPS,
            feature="set_operations",
        )

    def test_union_rejected_when_set_operations_disabled(self):
        self.assertGated(
            "SELECT region FROM orders UNION ALL SELECT region FROM orders",
            dict(CONFIG_CAPS, set_operations=False),
            "set_operations",
        )

    def test_default_config_accepts_join(self):
        self.accepts(
            "SELECT o.region FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
            CONFIG_CAPS,
        )

    def test_join_rejected_when_joins_disabled(self):
        self.assertGated(
            "SELECT o.region FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
            dict(CONFIG_CAPS, joins=False),
            "joins",
        )

    def test_default_config_accepts_percentile(self):
        self.accepts(
            "SELECT quantile_cont(sales, 0.5) AS p FROM orders", CONFIG_CAPS, feature="percentiles"
        )

    def test_percentile_rejected_when_disabled(self):
        self.assertGated(
            "SELECT quantile_cont(sales, 0.5) AS p FROM orders",
            dict(CONFIG_CAPS, percentiles=False),
            "percentiles",
        )

    def test_percentile_accepted_when_enabled(self):
        self.accepts(
            "SELECT quantile_cont(sales, 0.5) AS p FROM orders",
            dict(ALL_ON),
            feature="percentiles",
        )

    def test_case_expression_needs_case_expressions(self):
        sql = "SELECT CASE WHEN sales > 1 THEN 'a' ELSE 'b' END AS band FROM orders"
        self.assertGated(sql, dict(ALL_ON, case_expressions=False), "case_expressions")
        self.accepts(sql, ALL_ON, feature="case_expressions")

    def test_conditional_aggregation_needs_its_flag(self):
        sql = "SELECT sum(CASE WHEN region = 'East' THEN sales ELSE 0 END) AS e FROM orders"
        self.assertGated(sql, dict(ALL_ON, conditional_aggregation=False), "conditional_aggregation")
        self.accepts(sql, ALL_ON, feature="conditional_aggregation")

    def test_window_needs_windows(self):
        sql = "SELECT lag(sales) OVER (ORDER BY sales) AS prev FROM orders"
        self.assertGated(sql, dict(ALL_ON, windows=False), "windows")

    def test_ranking_window_needs_ranking_but_lag_does_not(self):
        rank_sql = "SELECT row_number() OVER (ORDER BY sales) AS rk FROM orders"
        self.assertGated(rank_sql, dict(ALL_ON, ranking=False), "ranking")
        lag_sql = "SELECT lag(sales) OVER (ORDER BY sales) AS prev FROM orders"
        self.accepts(lag_sql, dict(ALL_ON, ranking=False))

    def test_date_grain_needs_date_grains(self):
        sql = "SELECT date_trunc('month', order_id) AS m FROM orders"
        self.assertGated(sql, dict(ALL_ON, date_grains=False), "date_grains")
        self.accepts(sql, ALL_ON, feature="date_grains")

    def test_date_part_needs_date_grains(self):
        sql = "SELECT date_part('year', order_id) AS y FROM orders"
        self.assertGated(sql, dict(ALL_ON, date_grains=False), "date_grains")
        self.accepts(sql, ALL_ON, feature="date_grains")

    def test_strftime_needs_date_grains(self):
        sql = "SELECT strftime(order_date, '%Y-%m') AS m FROM orders"
        self.assertGated(sql, dict(ALL_ON, date_grains=False), "date_grains")
        self.accepts(sql, ALL_ON, feature="date_grains")

    def test_arithmetic_needs_calculations(self):
        sql = "SELECT sales + 1 AS x FROM orders"
        self.assertGated(sql, dict(ALL_ON, calculations=False), "calculations")
        self.accepts(sql, ALL_ON, feature="calculations")

    def test_group_having_and_order_need_their_flags(self):
        self.assertGated(
            "SELECT region FROM orders GROUP BY region", dict(ALL_ON, group_by=False), "group_by"
        )
        self.assertGated(
            "SELECT region, sum(sales) AS t FROM orders GROUP BY region HAVING sum(sales) > 1",
            dict(ALL_ON, having=False),
            "having",
        )
        self.assertGated(
            "SELECT region FROM orders ORDER BY sales", dict(ALL_ON, order_by=False), "order_by"
        )

    def test_missing_capability_key_counts_as_disabled(self):
        self.assertGated(
            "SELECT region FROM orders GROUP BY region", {}, "group_by"
        )

    def test_plain_select_needs_no_flags(self):
        summary = self.accepts("SELECT region, sales FROM orders", {})
        self.assertEqual(summary.features, ())

    def test_any_select_function_is_accepted_by_name(self):
        for sql in (
            "SELECT lower(region) FROM orders",
            "SELECT trim(region) FROM orders",
            "SELECT abs(sales) FROM orders",
            "SELECT sales % 2 FROM orders",
        ):
            with self.subTest(sql=sql):
                validate_sql_ast(sql, _catalog(), 0, ALL_ON)


class ParserErrorHintTests(unittest.TestCase):
    def test_parser_message_is_kept_when_it_has_no_quote(self):
        with self.assertRaises(SqlAstError) as ctx:
            serialize_sql("SELECT region FROM orders AS as")
        self.assertEqual(ctx.exception.category, "invalid_sql")
        self.assertIn('syntax error at or near "as"', str(ctx.exception))

    def test_parser_message_with_quote_is_dropped(self):
        with self.assertRaises(SqlAstError) as ctx:
            serialize_sql("SELECT 'x FROM orders")
        self.assertNotIn("'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
