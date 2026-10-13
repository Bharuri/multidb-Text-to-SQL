import unittest
from typing import Any

from src.query.catalog import Resolved
from src.query.matcher import MatchError, match_plan, normalize

DATE_OPS = ["year", "month", "year_month", "date_between"]
NUMERIC_OPS = [
    {"operator": ">="},
    {"operator": ">"},
    {"operator": "<="},
    {"operator": "<"},
    {"operator": "="},
    {"operator": "BETWEEN"},
]


def _column(table: str, name: str, data_type: str, column_class: str, **extra: Any) -> dict[str, Any]:
    return {"table": table, "column": name, "data_type": data_type, "column_class": column_class, "synonyms": [], **extra}


def make_resolved(lookup_values: bool = False) -> Resolved:
    physical = {
        "file": "databases/demo.duckdb",
        "tables": [
            {
                "name": "Orders",
                "columns": [
                    {"name": "Sub_Category"},
                    {"name": "Ship_Date"},
                    {"name": "ShipDate"},
                    {"name": "Order_Date"},
                    {"name": "Sales"},
                    {"name": "Quantity"},
                    {"name": "Ship_Mode"},
                    {"name": "Returned"},
                ],
            },
            {"name": "People", "columns": [{"name": "Region"}]},
        ],
    }
    semantic = {
        "numeric_operators": NUMERIC_OPS,
        "columns": [
            _column("Orders", "Sub_Category", "VARCHAR", "categorical", values=["Phones", "Paper", "Chairs"]),
            _column("Orders", "Ship_Date", "TIMESTAMP", "other", date_operators=DATE_OPS),
            _column("Orders", "ShipDate", "TIMESTAMP", "other", date_operators=DATE_OPS),
            _column("Orders", "Order_Date", "TIMESTAMP", "other", date_operators=DATE_OPS),
            _column("Orders", "Sales", "DOUBLE", "numeric", synonyms=["revenue"]),
            _column("Orders", "Quantity", "BIGINT", "numeric"),
            _column("Orders", "Ship_Mode", "VARCHAR", "other"),
            _column(
                "Orders",
                "Returned",
                "VARCHAR",
                "boolean",
                values=["Yes"],
                true_expressions=["returned", "yes", "y"],
                false_expressions=["not returned", "no"],
                true_value="Yes",
                false_value=None,
            ),
            _column("People", "Region", "VARCHAR", "categorical", values=["East", "West"]),
        ],
    }
    return Resolved(
        database="demo",
        registry_entry={},
        physical_file="specs/physical/demo.yaml",
        semantic_file="specs/semantic/demo.yaml",
        db_file="databases/demo.duckdb",
        physical=physical,
        semantic=semantic,
    )


def plan(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "table": "Orders",
        "select": [],
        "aggregation": {"function": "COUNT", "column": "*"},
        "filters": [],
        "limit": None,
    }
    base.update(overrides)
    return base


def filter_on(column: str, operator: str, value: Any) -> dict[str, Any]:
    return {"column": column, "operator": operator, "value": value}


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = make_resolved()

    def assertRejected(self, candidate: dict[str, Any], *fragments: str, lookup: Any = None) -> list[str]:
        with self.assertRaises(MatchError) as caught:
            match_plan(candidate, self.resolved, lookup)
        reasons = list(caught.exception.reasons)
        for fragment in fragments:
            self.assertTrue(any(fragment in reason for reason in reasons), f"{fragment!r} not in {reasons}")
        return reasons

    def test_stub_plan_matches(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Sub_Category", "=", "Phones")]), self.resolved)
        self.assertEqual(matched["table"], "Orders")
        self.assertEqual(matched["aggregation"], {"function": "COUNT", "column": "*"})
        self.assertEqual(matched["filters"], [{"column": "Sub_Category", "operator": "=", "value": "Phones"}])
        self.assertIsNone(matched["limit"])

    def test_repeat_runs_give_identical_output(self) -> None:
        candidate = plan(filters=[filter_on("sub category", "=", "phone")])
        first = match_plan(candidate, self.resolved)
        for _ in range(3):
            self.assertEqual(match_plan(candidate, self.resolved), first)

    def test_normalized_names_and_values(self) -> None:
        matched = match_plan(plan(filters=[filter_on("subcategory", "=", "Phone")]), self.resolved)
        self.assertEqual(matched["filters"][0]["column"], "Sub_Category")
        self.assertEqual(matched["filters"][0]["value"], "Phones")
        sources = [m["source"] for m in matched["matches"] if m["kind"] == "column"]
        self.assertEqual(sources, ["normalized"])

    def test_synonym_column_match(self) -> None:
        matched = match_plan(plan(aggregation={"function": "SUM", "column": "revenue"}), self.resolved)
        self.assertEqual(matched["aggregation"], {"function": "SUM", "column": "Sales"})
        self.assertEqual(matched["matches"][-1]["source"], "synonym")

    def test_boolean_expression_maps_to_stored_value(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Returned", "=", "returned")]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], "Yes")

    def test_unknown_table_lists_closest(self) -> None:
        candidate = plan(table="Ordrs")
        self.assertRejected(candidate, "table: 'Ordrs' does not match any table", "Closest: Orders")

    def test_unknown_column_is_reported(self) -> None:
        self.assertRejected(plan(aggregation={"function": "COUNT", "column": "Profit"}), "does not match any column of Orders")

    def test_ambiguous_column_is_rejected(self) -> None:
        self.assertRejected(plan(aggregation={"function": "COUNT", "column": "shipdate"}), "matches more than one column of Orders")

    def test_unstored_value_is_rejected_with_closest(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Sub_Category", "=", "Chairz")]), "not match any stored value", "Chairs")

    def test_text_value_without_stored_values_needs_lookup(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Ship_Mode", "=", "Air")]), "no lookup is available")

    def test_lookup_one_row_accepts(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def lookup(table: str, column: str, value: str) -> list[str]:
            calls.append((table, column, value))
            return ["Standard Class"]

        matched = match_plan(plan(filters=[filter_on("Ship_Mode", "=", "standard class")]), self.resolved, lookup)
        self.assertEqual(matched["filters"][0]["value"], "Standard Class")
        self.assertEqual(calls, [("Orders", "Ship_Mode", "standard class")])

    def test_lookup_zero_or_two_rows_rejects(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Ship_Mode", "=", "Air")]), "is not a value", lookup=lambda t, c, v: [])
        self.assertRejected(
            plan(filters=[filter_on("Ship_Mode", "=", "Air")]),
            "matches more than one value",
            lookup=lambda t, c, v: ["Air", "AIR"],
        )

    def test_numeric_operator_on_numeric_column(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Quantity", ">=", "3")]), self.resolved)
        self.assertEqual(matched["filters"][0], {"column": "Quantity", "operator": ">=", "value": 3.0})

    def test_between_needs_low_not_above_high(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Sales", "BETWEEN", [1, 5])]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], [1, 5])
        self.assertRejected(plan(filters=[filter_on("Sales", "between", [9, 2])]), "range for Sales is reversed")

    def test_non_numeric_value_is_rejected(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Sales", ">", "lots")]), "must be a number")

    def test_month_of_13_is_rejected(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Ship_Date", "month", 13)]), "month for Ship_Date must be from 1 to 12")

    def test_month_of_12_is_accepted(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Ship_Date", "month", 12)]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], 12)

    def test_year_must_be_integer(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Order_Date", "year", "2014")]), "must be a whole number")

    def test_year_month_shape(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Order_Date", "year_month", [2014, 9])]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], [2014, 9])
        self.assertRejected(plan(filters=[filter_on("Order_Date", "year_month", [2014])]), "needs a list of two values")

    def test_date_between_is_inclusive_and_checked(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Order_Date", "date_between", ["2014-01-01", "2014-03-31"])]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], ["2014-01-01", "2014-03-31"])
        self.assertRejected(plan(filters=[filter_on("Order_Date", "date_between", ["2014-03-31", "2014-01-01"])]), "is reversed")
        self.assertRejected(plan(filters=[filter_on("Order_Date", "date_between", ["2014-02-30", "2014-03-01"])]), "not a real date")

    def test_date_operator_on_text_column_is_rejected(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Sub_Category", "year", 2014)]), "not allowed for text column Sub_Category")

    def test_numeric_operator_on_date_column_is_rejected(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Order_Date", ">=", 2014)]), "not allowed here")

    def test_text_column_rejects_range_operator(self) -> None:
        self.assertRejected(plan(filters=[filter_on("Sub_Category", ">", "Paper")]), "Only '=' is allowed")

    def test_sum_needs_numeric_column(self) -> None:
        self.assertRejected(plan(aggregation={"function": "SUM", "column": "Sub_Category"}), "SUM needs a numeric column")
        self.assertRejected(plan(aggregation={"function": "SUM", "column": "*"}), "only for COUNT")
        self.assertRejected(plan(aggregation={"function": "MEDIAN", "column": "Sales"}), "function 'MEDIAN' is not allowed")

    def test_row_query_needs_select_and_rejects_select_with_aggregate(self) -> None:
        self.assertRejected(plan(aggregation=None, select=[]), "a row query needs at least one column")
        matched = match_plan(plan(aggregation=None, select=["Sub_Category"], limit=10), self.resolved)
        self.assertEqual(matched["select"], ["Sub_Category"])
        self.assertEqual(matched["limit"], 10)
        self.assertRejected(plan(select=["Sales"]), "cannot be used together with an aggregation")

    def test_star_select_expands_to_every_column(self) -> None:
        matched = match_plan(plan(aggregation=None, select=["*"]), self.resolved)
        self.assertEqual(
            matched["select"],
            ["Sub_Category", "Ship_Date", "ShipDate", "Order_Date", "Sales", "Quantity", "Ship_Mode", "Returned"],
        )
        self.assertEqual(matched["matches"][-1]["result"], "all 8 columns")

    def test_star_select_cannot_mix_with_columns(self) -> None:
        self.assertRejected(plan(aggregation=None, select=["*", "Sales"]), "'*' cannot be combined with other columns")

    def test_limit_must_be_positive_whole_number(self) -> None:
        self.assertRejected(plan(limit=0), "limit: must be a positive whole number")
        self.assertRejected(plan(limit="5"), "limit: must be a positive whole number")

    def test_every_problem_is_reported_together(self) -> None:
        reasons = self.assertRejected(
            plan(filters=[filter_on("Sub_Category", "=", "Chairz"), filter_on("Order_Date", "month", 13)]),
            "filters[0]",
            "filters[1]",
        )
        self.assertEqual(len(reasons), 2)

    def test_plan_is_not_changed(self) -> None:
        candidate = plan(filters=[filter_on("Sub_Category", "=", "Phone")])
        snapshot = repr(candidate)
        match_plan(candidate, self.resolved)
        self.assertEqual(repr(candidate), snapshot)

    def test_normalize_rules(self) -> None:
        self.assertEqual(normalize("Sub_Category"), "subcategory")
        self.assertEqual(normalize("sub category"), normalize("Sub_Category"))
        self.assertEqual(normalize("Phone"), normalize("Phones"))
        self.assertEqual(normalize("s"), "s")


if __name__ == "__main__":
    unittest.main()
