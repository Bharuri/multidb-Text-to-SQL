import copy
import unittest
from typing import Any

from src.query.catalog import Resolved
from src.query.sql_builder import build_sql
from src.query.validator import validate

SCHEMA = "shop"
CATALOG = "shop"
TABLE = "Orders"

PHYSICAL = {
    "schema": SCHEMA,
    "tables": [
        {
            "name": TABLE,
            "columns": [
                {"name": "Order_Date", "data_type": "TIMESTAMP"},
                {"name": "Sales", "data_type": "DOUBLE"},
                {"name": "Region", "data_type": "VARCHAR"},
                {"name": "Category", "data_type": "VARCHAR"},
                {"name": "Returned", "data_type": "BOOLEAN"},
            ],
        }
    ],
}

SEMANTIC = {
    "numeric_operators": [{"operator": op} for op in ("=", ">=", ">", "<=", "<", "BETWEEN")],
    "columns": [
        {"table": TABLE, "column": "Order_Date", "data_type": "TIMESTAMP", "column_class": "other",
         "date_operators": ["year", "month", "year_month", "date_between"]},
        {"table": TABLE, "column": "Sales", "data_type": "DOUBLE", "column_class": "numeric"},
        {"table": TABLE, "column": "Region", "data_type": "VARCHAR", "column_class": "other"},
        {"table": TABLE, "column": "Category", "data_type": "VARCHAR", "column_class": "categorical",
         "values": ["Phones", "Chairs"]},
        {"table": TABLE, "column": "Returned", "data_type": "BOOLEAN", "column_class": "boolean", "values": ["Yes"]},
    ],
}

RESOLVED = Resolved(
    database=SCHEMA,
    registry_entry={},
    physical_file="",
    semantic_file="",
    db_file=f"databases/{CATALOG}.duckdb",
    physical=PHYSICAL,
    semantic=SEMANTIC,
)

REGIONS = {"Mumbai": ["Mumbai"], "Dup": ["Dup A", "Dup B"]}


def lookup(table: str, column: str, text: str) -> list[str]:
    if column != "Region":
        return []
    return [value for key, values in REGIONS.items() if key.casefold() == text.casefold() for value in values]


def qualified(table: str = TABLE) -> str:
    return f'"{CATALOG}"."{SCHEMA}"."{table}"'


def matched(**overrides: Any) -> dict[str, Any]:
    plan = {
        "table": TABLE,
        "select": [],
        "dimensions": [],
        "metrics": [{"function": "COUNT", "column": "*", "alias": "count"}],
        "filters": [],
        "group_by": [],
        "having": [],
        "order_by": [],
        "calculations": [],
        "limit": None,
        "matches": [],
    }
    plan.update(overrides)
    return plan


def built_for(**overrides: Any) -> dict[str, Any]:
    return build_sql(matched(**overrides), SCHEMA, CATALOG)


def check(sql: str, params: list[Any] | None = None, look: Any = lookup) -> dict[str, Any]:
    return validate({"sql": sql, "params": params or []}, RESOLVED, look)


class ValidBuiltSqlTests(unittest.TestCase):
    def assert_valid(self, built: dict[str, Any]) -> None:
        result = validate(built, RESOLVED, lookup)
        self.assertEqual(result, {"valid": True, "reasons": []}, built["sql"])

    def assert_valid_sql(self, sql: str, params: list[Any] | None = None) -> None:
        result = check(sql, params)
        self.assertEqual(result, {"valid": True, "reasons": []}, sql)

    def test_row_query_with_stored_value_and_limit(self) -> None:
        built = built_for(select=["Sales"], filters=[{"column": "Category", "operator": "=", "value": "Phones"}], limit=50)
        self.assert_valid(built)

    def test_count_with_text_lookup(self) -> None:
        built = built_for(filters=[{"column": "Region", "operator": "=", "value": "mumbai"}])
        self.assert_valid(built)

    def test_each_date_form(self) -> None:
        for operator, value in (
            ("year", 2025),
            ("month", 9),
            ("year_month", [2025, 9]),
            ("date_between", ["2025-01-01", "2025-03-31"]),
        ):
            with self.subTest(operator=operator):
                self.assert_valid(built_for(filters=[{"column": "Order_Date", "operator": operator, "value": value}]))

    def test_numeric_comparisons(self) -> None:
        self.assert_valid(built_for(filters=[{"column": "Sales", "operator": ">=", "value": 10}]))
        self.assert_valid(built_for(filters=[{"column": "Sales", "operator": "BETWEEN", "value": [10, 20.5]}]))

    def test_aggregates(self) -> None:
        self.assert_valid(built_for(metrics=[{"function": "SUM", "column": "Sales", "alias": "total"}]))
        self.assert_valid(built_for(metrics=[{"function": "COUNT", "column": "Region", "alias": "n"}]))

    def test_grouped_aggregate_with_having_order_and_limit(self) -> None:
        built = built_for(
            dimensions=["Category"],
            group_by=["Category"],
            metrics=[{"function": "SUM", "column": "Sales", "alias": "total"}],
            having=[{"metric": "total", "operator": ">=", "value": 100}],
            order_by=[{"ref": "total", "direction": "DESC"}],
            limit=5,
        )
        self.assert_valid(built)

    def test_multiple_dimensions_and_metrics_with_having_and_order(self) -> None:
        built = built_for(
            dimensions=["Region", "Category"],
            group_by=["Region", "Category"],
            metrics=[
                {"function": "SUM", "column": "Sales", "alias": "total"},
                {"function": "COUNT", "column": "*", "alias": "n"},
            ],
            having=[{"metric": "total", "operator": ">=", "value": 100}],
            order_by=[{"ref": "total", "direction": "DESC"}, {"ref": "Region", "direction": "ASC"}],
            limit=10,
        )
        self.assert_valid(built)

    def test_raw_grouped_query_with_ascending_order(self) -> None:
        self.assert_valid_sql(f'SELECT "Category", COUNT(*) AS n FROM {qualified()} GROUP BY "Category" ORDER BY "Category" ASC LIMIT 5')

    def test_year_over_year_change_with_parameters(self) -> None:
        sql = (
            f'SELECT "Category", SUM("Sales") AS total, '
            f'SUM("Sales") - LAG(SUM("Sales")) OVER (PARTITION BY "Category" ORDER BY "Category") AS change '
            f'FROM {qualified()} GROUP BY "Category" ORDER BY "Category" LIMIT 10'
        )
        self.assert_valid_sql(sql)

    def test_percentage_growth_with_parameters(self) -> None:
        sql = (
            f'SELECT "Category", ((SUM("Sales") - LAG(SUM("Sales")) OVER (PARTITION BY "Category" ORDER BY "Category")) '
            f'/ NULLIF(LAG(SUM("Sales")) OVER (PARTITION BY "Category" ORDER BY "Category"), ?)) * ? AS growth '
            f'FROM {qualified()} GROUP BY "Category" ORDER BY "Category" LIMIT 10'
        )
        self.assert_valid_sql(sql, [0, 100])

    def test_boolean_stored_value(self) -> None:
        self.assert_valid(built_for(filters=[{"column": "Returned", "operator": "=", "value": "Yes"}]))


class RejectedSqlTests(unittest.TestCase):
    def assert_rejected(self, sql: str, params: list[Any] | None = None, look: Any = lookup) -> None:
        result = check(sql, params, look)
        self.assertFalse(result["valid"], sql)
        self.assertTrue(result["reasons"], sql)

    def test_join_is_rejected(self) -> None:
        self.assert_rejected(
            f"SELECT COUNT(*) FROM {qualified()} a JOIN {qualified()} b ON a.\"Sales\" = b.\"Sales\""
        )

    def test_subquery_is_rejected(self) -> None:
        self.assert_rejected(
            f'SELECT "Sales" FROM {qualified()} WHERE "Sales" > (SELECT 1) LIMIT 5'
        )

    def test_unknown_table_is_rejected(self) -> None:
        self.assert_rejected(f"SELECT COUNT(*) FROM {qualified('Nope')}")

    def test_unknown_column_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Nope" FROM {qualified()} LIMIT 5')

    def test_wrong_catalog_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM "other"."{SCHEMA}"."{TABLE}"')

    def test_ilike_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM {qualified()} WHERE "Category" ILIKE ?', ["%phone%"])

    def test_value_not_stored_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM {qualified()} WHERE LOWER("Category") = LOWER(?)', ["Tables"])

    def test_ambiguous_lookup_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM {qualified()} WHERE LOWER("Region") = LOWER(?)', ["Dup"])

    def test_text_literal_in_sql_is_rejected(self) -> None:
        self.assert_rejected(f"SELECT COUNT(*) FROM {qualified()} WHERE LOWER(\"Category\") = LOWER('Phones')")

    def test_bare_literal_comparison_is_rejected(self) -> None:
        self.assert_rejected(f"SELECT COUNT(*) FROM {qualified()} WHERE \"Category\" = 'Phones'")

    def test_row_query_without_limit_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales" FROM {qualified()}')

    def test_aggregate_with_limit_is_rejected(self) -> None:
        self.assert_rejected(f"SELECT COUNT(*) FROM {qualified()} LIMIT 5")

    def test_limit_above_row_limit_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales" FROM {qualified()} LIMIT 5000')

    def test_limit_of_zero_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales" FROM {qualified()} LIMIT 0')

    def test_write_statement_is_rejected(self) -> None:
        self.assert_rejected(f'DELETE FROM {qualified()} WHERE "Sales" > ?', [1])

    def test_two_statements_are_rejected(self) -> None:
        self.assert_rejected(f"SELECT 1; DELETE FROM {qualified()}")

    def test_star_select_is_rejected(self) -> None:
        self.assert_rejected(f"SELECT * FROM {qualified()} LIMIT 5")

    def test_grouped_aggregate_without_limit_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) FROM {qualified()} GROUP BY "Category"')

    def test_grouped_aggregate_with_large_limit_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) FROM {qualified()} GROUP BY "Category" LIMIT 5000')

    def test_rollup_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) FROM {qualified()} GROUP BY ROLLUP("Category") LIMIT 5')

    def test_cte_is_rejected(self) -> None:
        self.assert_rejected(f'WITH x AS (SELECT "Sales" FROM {qualified()}) SELECT "Sales" FROM x LIMIT 5')

    def test_row_number_window_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales", ROW_NUMBER() OVER (ORDER BY "Sales") AS rn FROM {qualified()} LIMIT 5')

    def test_literal_in_nullif_is_rejected(self) -> None:
        sql = (
            f'SELECT "Category", SUM("Sales") / NULLIF(SUM("Sales"), 0) AS ratio '
            f'FROM {qualified()} GROUP BY "Category" LIMIT 5'
        )
        self.assert_rejected(sql)

    def test_literal_window_offset_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales", LAG("Sales", 2) OVER (ORDER BY "Sales") AS prev FROM {qualified()} LIMIT 5')

    def test_having_on_plain_column_is_rejected(self) -> None:
        self.assert_rejected(
            f'SELECT "Category", COUNT(*) AS n FROM {qualified()} GROUP BY "Category" HAVING "Category" = ? LIMIT 5',
            ["Phones"],
        )

    def test_having_with_literal_is_rejected(self) -> None:
        self.assert_rejected(
            f'SELECT "Category", COUNT(*) AS n FROM {qualified()} GROUP BY "Category" HAVING COUNT(*) > 5 LIMIT 5'
        )

    def test_order_by_unknown_column_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) AS n FROM {qualified()} GROUP BY "Category" ORDER BY "Nope" LIMIT 5')

    def test_order_by_ungrouped_column_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) AS n FROM {qualified()} GROUP BY "Category" ORDER BY "Region" LIMIT 5')

    def test_plan_table_mismatch_is_rejected(self) -> None:
        built = built_for(select=["Sales"], limit=5)
        result = validate(built, RESOLVED, lookup, matched=matched(table="Other", select=["Sales"], metrics=[], limit=5))
        self.assertFalse(result["valid"])

    def test_plan_metric_mismatch_is_rejected(self) -> None:
        built = built_for(metrics=[{"function": "SUM", "column": "Sales", "alias": "total"}])
        result = validate(built, RESOLVED, lookup, matched=matched(metrics=[{"function": "MAX", "column": "Sales", "alias": "total"}]))
        self.assertFalse(result["valid"])

    def test_plan_dimension_mismatch_is_rejected(self) -> None:
        built = built_for(dimensions=["Category"], group_by=["Category"], metrics=[{"function": "COUNT", "column": "*", "alias": "n"}], limit=5)
        result = validate(built, RESOLVED, lookup, matched=matched(dimensions=["Region"], group_by=["Region"], metrics=[{"function": "COUNT", "column": "*", "alias": "n"}], limit=5))
        self.assertFalse(result["valid"])

    def test_plan_consistent_query_is_accepted(self) -> None:
        built = built_for(dimensions=["Category"], group_by=["Category"], metrics=[{"function": "COUNT", "column": "*", "alias": "n"}], limit=5)
        plan = matched(dimensions=["Category"], group_by=["Category"], metrics=[{"function": "COUNT", "column": "*", "alias": "n"}], limit=5)
        self.assertTrue(validate(built, RESOLVED, lookup, matched=plan)["valid"])

    def test_mixed_columns_and_aggregate_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Sales", COUNT(*) FROM {qualified()}')

    def test_month_outside_range_is_rejected(self) -> None:
        built = built_for(filters=[{"column": "Order_Date", "operator": "month", "value": 9}])
        built["params"] = [13]
        self.assertFalse(validate(built, RESOLVED, lookup)["valid"])

    def test_text_operator_on_numeric_column_is_rejected(self) -> None:
        built = built_for(filters=[{"column": "Sales", "operator": ">=", "value": 10}])
        built["sql"] = built["sql"].replace('"Sales" >= ?', 'LOWER("Sales") = LOWER(?)')
        self.assertFalse(validate(built, RESOLVED, lookup)["valid"])

    def test_numeric_operator_on_text_column_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM {qualified()} WHERE "Category" > ?', ["Phones"])

    def test_parameter_count_mismatch_is_rejected(self) -> None:
        self.assert_rejected(
            f'SELECT COUNT(*) FROM {qualified()} WHERE LOWER("Category") = LOWER(?)', ["Phones", "extra"]
        )

    def test_text_value_without_lookup_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT COUNT(*) FROM {qualified()} WHERE LOWER("Region") = LOWER(?)', ["Mumbai"], look=None)

    def test_function_outside_allowlist_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT upper("Category") FROM {qualified()} LIMIT 5')

    def test_between_with_low_above_high_is_rejected(self) -> None:
        built = built_for(filters=[{"column": "Sales", "operator": "BETWEEN", "value": [10, 20]}])
        built["params"] = [30, 20]
        self.assertFalse(validate(built, RESOLVED, lookup)["valid"])


class ValidatorShapeTests(unittest.TestCase):
    def test_reasons_name_the_problem(self) -> None:
        result = check(f"SELECT COUNT(*) FROM {qualified('Nope')}")
        self.assertIn("Nope", result["reasons"][0])

    def test_does_not_change_the_built_sql(self) -> None:
        built = built_for(filters=[{"column": "Category", "operator": "=", "value": "Phones"}])
        before = copy.deepcopy(built)
        validate(built, RESOLVED, lookup)
        self.assertEqual(built, before)


if __name__ == "__main__":
    unittest.main()
