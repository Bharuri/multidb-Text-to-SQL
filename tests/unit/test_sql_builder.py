import copy
import unittest
from typing import Any

import duckdb

from src.query.sql_builder import BuildError, build_sql

SCHEMA = "sample_superstore"
CATALOG = "sample_superstore"

MATCHED_COUNT: dict[str, Any] = {
    "table": "Orders",
    "select": [],
    "aggregation": {"function": "COUNT", "column": "*"},
    "filters": [
        {"column": "Category", "operator": "=", "value": "Furniture"},
        {"column": "Order_Date", "operator": "year_month", "value": [2025, 9]},
    ],
    "limit": None,
    "matches": [],
}

MATCHED_ROWS: dict[str, Any] = {
    "table": "Orders",
    "select": ["Sub_Category", "Order_Date", "Sales"],
    "aggregation": None,
    "filters": [
        {"column": "Sub_Category", "operator": "=", "value": "Phones"},
        {"column": "Order_Date", "operator": "year", "value": 2025},
    ],
    "limit": None,
    "matches": [],
}


class SqlBuilderTests(unittest.TestCase):
    def test_count_query_has_expected_sql_params_and_no_limit(self) -> None:
        built = build_sql(MATCHED_COUNT, SCHEMA, CATALOG)
        self.assertEqual(
            built["sql"],
            'SELECT COUNT(*) FROM "sample_superstore"."sample_superstore"."Orders" WHERE LOWER("Category") = LOWER(?) '
            'AND EXTRACT(YEAR FROM "Order_Date") = ? AND EXTRACT(MONTH FROM "Order_Date") = ?',
        )
        self.assertEqual(built["params"], ["Furniture", 2025, 9])
        self.assertNotIn("LIMIT", built["sql"])

    def test_row_query_selects_listed_columns_with_default_limit(self) -> None:
        built = build_sql(MATCHED_ROWS, SCHEMA, CATALOG, default_row_limit=1000)
        self.assertEqual(
            built["sql"],
            'SELECT "Sub_Category", "Order_Date", "Sales" FROM "sample_superstore"."sample_superstore"."Orders" '
            'WHERE LOWER("Sub_Category") = LOWER(?) AND EXTRACT(YEAR FROM "Order_Date") = ? LIMIT 1000',
        )
        self.assertEqual(built["params"], ["Phones", 2025])

    def test_row_query_without_columns_is_refused(self) -> None:
        matched = copy.deepcopy(MATCHED_ROWS)
        matched["select"] = []
        with self.assertRaises(BuildError):
            build_sql(matched, SCHEMA, CATALOG)

    def test_no_user_value_appears_in_sql_text(self) -> None:
        for matched in (MATCHED_COUNT, MATCHED_ROWS):
            sql = build_sql(matched, SCHEMA, CATALOG)["sql"]
            for value in ("Furniture", "Phones"):
                self.assertNotIn(value, sql)

    def test_aggregates_have_no_limit_even_with_a_limit_given(self) -> None:
        matched = copy.deepcopy(MATCHED_COUNT)
        matched["limit"] = 50
        self.assertNotIn("LIMIT", build_sql(matched, SCHEMA, CATALOG)["sql"])
        matched["aggregation"] = {"function": "SUM", "column": "Sales"}
        built = build_sql(matched, SCHEMA, CATALOG)
        self.assertTrue(built["sql"].startswith('SELECT SUM("Sales") FROM'))
        self.assertNotIn("LIMIT", built["sql"])

    def test_listed_columns_use_the_given_limit(self) -> None:
        matched = copy.deepcopy(MATCHED_ROWS)
        matched["select"] = ["Order_ID", "Sales"]
        matched["limit"] = 50
        built = build_sql(matched, SCHEMA, CATALOG)
        self.assertTrue(built["sql"].startswith('SELECT "Order_ID", "Sales" FROM "sample_superstore"."sample_superstore"."Orders"'))
        self.assertTrue(built["sql"].endswith(" LIMIT 50"))

    def test_limit_above_default_is_capped(self) -> None:
        matched = copy.deepcopy(MATCHED_ROWS)
        matched["limit"] = 5000
        self.assertTrue(build_sql(matched, SCHEMA, CATALOG, default_row_limit=1000)["sql"].endswith(" LIMIT 1000"))

    def test_year_month_gives_both_extract_conditions(self) -> None:
        sql = build_sql(MATCHED_COUNT, SCHEMA, CATALOG)["sql"]
        self.assertIn('EXTRACT(YEAR FROM "Order_Date") = ?', sql)
        self.assertIn('EXTRACT(MONTH FROM "Order_Date") = ?', sql)

    def test_date_between_includes_the_end_date(self) -> None:
        matched = {
            "table": "Orders",
            "select": [],
            "aggregation": {"function": "COUNT", "column": "*"},
            "filters": [{"column": "Order_Date", "operator": "date_between", "value": ["2025-01-01", "2025-03-31"]}],
            "limit": None,
        }
        built = build_sql(matched, SCHEMA, "memory")
        self.assertIn(
            '"Order_Date" >= CAST(? AS DATE) AND "Order_Date" < CAST(? AS DATE)',
            built["sql"],
        )
        self.assertEqual(built["params"], ["2025-01-01", "2025-04-01"])

        connection = duckdb.connect()
        try:
            connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
            connection.execute(f'CREATE TABLE "{SCHEMA}"."Orders" ("Order_Date" TIMESTAMP)')
            connection.execute(
                f"INSERT INTO \"{SCHEMA}\".\"Orders\" VALUES ('2024-12-31 23:59:00'), ('2025-01-01 00:00:00'), "
                "('2025-03-31 23:59:59'), ('2025-04-01 00:00:00')"
            )
            count = connection.execute(built["sql"], built["params"]).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)

    def test_between_binds_both_bounds(self) -> None:
        matched = copy.deepcopy(MATCHED_ROWS)
        matched["filters"] = [{"column": "Sales", "operator": "BETWEEN", "value": [10, 20.5]}]
        built = build_sql(matched, SCHEMA, CATALOG)
        self.assertIn('"Sales" BETWEEN ? AND ?', built["sql"])
        self.assertEqual(built["params"], [10, 20.5])

    def test_unknown_operator_is_a_build_error(self) -> None:
        matched = copy.deepcopy(MATCHED_ROWS)
        matched["filters"] = [{"column": "Sales", "operator": "LIKE", "value": 1}]
        with self.assertRaises(BuildError):
            build_sql(matched, SCHEMA, CATALOG)

    def test_aggregate_star_only_for_count(self) -> None:
        matched = copy.deepcopy(MATCHED_COUNT)
        matched["aggregation"] = {"function": "SUM", "column": "*"}
        with self.assertRaises(BuildError):
            build_sql(matched, SCHEMA, CATALOG)

    def test_unknown_aggregate_function_is_a_build_error(self) -> None:
        matched = copy.deepcopy(MATCHED_COUNT)
        matched["aggregation"] = {"function": "MEDIAN", "column": "Sales"}
        with self.assertRaises(BuildError):
            build_sql(matched, SCHEMA, CATALOG)


if __name__ == "__main__":
    unittest.main()
