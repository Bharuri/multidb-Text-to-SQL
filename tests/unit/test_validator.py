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
        "aggregation": {"function": "COUNT", "column": "*"},
        "filters": [],
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
        self.assert_valid(built_for(aggregation={"function": "SUM", "column": "Sales"}))
        self.assert_valid(built_for(aggregation={"function": "COUNT", "column": "Region"}))

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

    def test_group_by_is_rejected(self) -> None:
        self.assert_rejected(f'SELECT "Category", COUNT(*) FROM {qualified()} GROUP BY "Category"')

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
