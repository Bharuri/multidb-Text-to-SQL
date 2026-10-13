import copy
import json
import unittest
from pathlib import Path
from typing import Any

import duckdb

from src.query.catalog import resolve
from src.query.matcher import MatchError, match_plan
from src.query.plan import PlanError, parse_reply
from src.query.sql_builder import build_sql
from src.query.validator import validate

RESOLVED = resolve("sample_superstore")
SCHEMA = RESOLVED.physical["schema"]
DB_STEM = Path(RESOLVED.db_file).stem

REPEAT: dict[str, Any] = {"customer": "Customer_ID", "date": "Order_Date", "dimension": "Sub_Category"}

BASE_PLAN: dict[str, Any] = {
    "table": "Orders",
    "select": [],
    "dimensions": [],
    "metrics": [],
    "filters": [],
    "group_by": [],
    "having": [],
    "order_by": [],
    "calculations": [],
    "limit": None,
}


def repeat_plan(repeat: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE_PLAN)
    data["repeat_purchase"] = copy.deepcopy(REPEAT if repeat is None else repeat)
    data.update(overrides)
    return data


def build(data: dict[str, Any]) -> dict[str, Any]:
    matched = match_plan(data, RESOLVED)
    built = build_sql(matched, SCHEMA, DB_STEM, default_row_limit=1000)
    assert built is not None
    return built


def run_sql(built: dict[str, Any]) -> list[tuple]:
    connection = duckdb.connect(str(RESOLVED.db_file), read_only=True)
    try:
        return connection.execute(built["sql"], built["params"]).fetchall()
    finally:
        connection.close()


class ParseRepeatTests(unittest.TestCase):
    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        return parse_reply(json.dumps(data))

    def test_repeat_plan_is_accepted_and_keeps_the_key(self) -> None:
        self.assertEqual(self.parse(repeat_plan())["repeat_purchase"], REPEAT)

    def test_plan_without_repeat_key_has_no_repeat_key(self) -> None:
        self.assertNotIn("repeat_purchase", self.parse(BASE_PLAN))

    def test_null_dimension_is_accepted(self) -> None:
        self.assertIsNone(self.parse(repeat_plan(dict(REPEAT, dimension=None)))["repeat_purchase"]["dimension"])

    def test_missing_field_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parse(repeat_plan({"customer": "Customer_ID", "date": "Order_Date"}))

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parse(repeat_plan("Customer_ID"))

    def test_extra_key_inside_repeat_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parse(repeat_plan(dict(REPEAT, extra="x")))


class MatchRepeatTests(unittest.TestCase):
    def test_repeat_plan_is_matched_with_resolved_columns(self) -> None:
        matched = match_plan(repeat_plan(), RESOLVED)
        self.assertEqual(matched["repeat_purchase"], REPEAT)
        self.assertEqual(matched["metrics"], [])

    def test_unknown_customer_column_is_rejected(self) -> None:
        with self.assertRaises(MatchError):
            match_plan(repeat_plan(dict(REPEAT, customer="Nope")), RESOLVED)

    def test_non_date_date_column_is_rejected(self) -> None:
        with self.assertRaises(MatchError):
            match_plan(repeat_plan(dict(REPEAT, date="Sales")), RESOLVED)

    def test_dimension_equal_to_customer_is_rejected(self) -> None:
        with self.assertRaises(MatchError):
            match_plan(repeat_plan(dict(REPEAT, dimension="Customer_ID")), RESOLVED)

    def test_other_parts_with_repeat_are_rejected(self) -> None:
        with self.assertRaises(MatchError):
            match_plan(repeat_plan(dimensions=["Region"]), RESOLVED)


class BuildRepeatTests(unittest.TestCase):
    def test_built_sql_uses_the_template_without_params(self) -> None:
        built = build(repeat_plan())
        self.assertIn("DENSE_RANK() OVER", built["sql"])
        self.assertEqual(built["params"], [])

    def test_null_dimension_gives_one_overall_row(self) -> None:
        built = build(repeat_plan(dict(REPEAT, dimension=None)))
        self.assertNotIn("GROUP BY ", built["sql"].split(") AS firsts")[-1])

    def test_validator_accepts_the_built_template(self) -> None:
        matched = match_plan(repeat_plan(), RESOLVED)
        built = build(repeat_plan())
        self.assertEqual(validate(built, RESOLVED, default_row_limit=1000, matched=matched), {"valid": True, "reasons": []})

    def test_validator_rejects_altered_sql(self) -> None:
        matched = match_plan(repeat_plan(), RESOLVED)
        built = build(repeat_plan())
        built["sql"] = built["sql"].replace("purchase_no = 2", "purchase_no = 3")
        self.assertFalse(validate(built, RESOLVED, default_row_limit=1000, matched=matched)["valid"])

    def test_validator_rejects_generic_sql_for_a_repeat_plan(self) -> None:
        matched = match_plan(repeat_plan(), RESOLVED)
        built = {"sql": 'SELECT COUNT(*) FROM "Orders"', "params": []}
        self.assertFalse(validate(built, RESOLVED, default_row_limit=1000, matched=matched)["valid"])


class ExecuteRepeatTests(unittest.TestCase):
    def rows(self, dimension: str | None = "Sub_Category") -> dict[str, tuple]:
        return {row[0]: row[1:] for row in run_sql(build(repeat_plan(dict(REPEAT, dimension=dimension))))}

    def test_binders_matches_the_reference(self) -> None:
        customers, average, never = self.rows()["Binders"]
        self.assertEqual(customers, 218)
        self.assertEqual(round(average, 2), 294.07)
        self.assertEqual(never, 3)

    def test_copiers_matches_the_reference(self) -> None:
        customers, average, never = self.rows()["Copiers"]
        self.assertEqual((customers, round(average, 2), never), (9, 188.0, 0))

    def test_overall_row_counts_customers_once(self) -> None:
        (customers, _average, never), = run_sql(build(repeat_plan(dict(REPEAT, dimension=None))))
        self.assertEqual(customers, 804)
        self.assertEqual(never, 12)


if __name__ == "__main__":
    unittest.main()
