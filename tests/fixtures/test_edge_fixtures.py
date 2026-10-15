"""Verify the edge fixtures produce the hand-computed expected results."""

import os
import tempfile
import unittest

import duckdb

from tests.fixtures.edge_fixtures import EXPECTED, build_fixture


class EdgeFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = build_fixture(os.path.join(cls.tmp.name, "edge.duckdb"))
        cls.con = duckdb.connect(cls.path, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def scalar(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchone()[0]

    def test_ties_rank_both_rows_first(self):
        rows = self.con.execute(
            "SELECT order_id, RANK() OVER (PARTITION BY region ORDER BY sales DESC) "
            "FROM orders WHERE region = 'South'"
        ).fetchall()
        ranks = sorted(rank for _, rank in rows)
        self.assertEqual(ranks, list(EXPECTED["south_rank_of_80"]))

    def test_duplicate_lines_double_count_in_line_sum(self):
        got = dict(self.con.execute(
            "SELECT region, SUM(sales) FROM orders GROUP BY region"
        ).fetchall())
        self.assertEqual(got, EXPECTED["region_sales"])

    def test_duplicate_line_collapses_when_deduplicated_by_order_line(self):
        got = dict(self.con.execute(
            "SELECT order_id, MAX(sales) FROM orders GROUP BY order_id"
        ).fetchall())
        self.assertEqual(got, EXPECTED["order_sales"])

    def test_null_profit_excluded_from_sum_but_row_counted(self):
        self.assertEqual(
            self.scalar("SELECT SUM(profit) FROM orders WHERE region = 'East'"),
            EXPECTED["east_profit_sum"],
        )
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM orders WHERE region = 'East'"),
            EXPECTED["east_row_count"],
        )

    def test_zero_sales_order_is_present(self):
        got = [r[0] for r in self.con.execute(
            "SELECT order_id FROM orders WHERE sales = 0"
        ).fetchall()]
        self.assertEqual(got, EXPECTED["zero_sales_orders"])

    def test_single_purchase_customer_identified(self):
        got = [r[0] for r in self.con.execute(
            "SELECT customer_id FROM purchases GROUP BY customer_id HAVING COUNT(*) = 1"
        ).fetchall()]
        self.assertEqual(got, EXPECTED["single_purchase_customers"])

    def test_gap_between_purchases_in_days(self):
        gap = self.scalar(
            "SELECT DATE_DIFF('day', MIN(purchase_date), MAX(purchase_date)) "
            "FROM purchases WHERE customer_id = 'C4'"
        )
        self.assertEqual(gap, EXPECTED["c4_gap_days"])

    def test_missing_month_is_absent(self):
        months = {r[0] for r in self.con.execute("SELECT month FROM monthly_sales").fetchall()}
        expected_missing = set(EXPECTED["missing_months"])
        self.assertTrue(expected_missing.isdisjoint(months))

    def test_customer_without_orders_found_by_anti_join(self):
        got = [r[0] for r in self.con.execute(
            "SELECT c.customer_id FROM customers c "
            "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id) "
            "ORDER BY c.customer_id"
        ).fetchall()]
        self.assertEqual(got, EXPECTED["customers_without_orders"])


if __name__ == "__main__":
    unittest.main()
