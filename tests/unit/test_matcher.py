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
        "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}],
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
        self.assertEqual(matched["metrics"], [{"function": "COUNT", "column": "*", "alias": "n"}])
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
        matched = match_plan(plan(metrics=[{"function": "SUM", "column": "revenue", "alias": "m"}]), self.resolved)
        self.assertEqual(matched["metrics"], [{"function": "SUM", "column": "Sales", "alias": "m"}])
        self.assertEqual(matched["matches"][-1]["source"], "synonym")

    def test_boolean_expression_maps_to_stored_value(self) -> None:
        matched = match_plan(plan(filters=[filter_on("Returned", "=", "returned")]), self.resolved)
        self.assertEqual(matched["filters"][0]["value"], "Yes")

    def test_unknown_table_lists_closest(self) -> None:
        candidate = plan(table="Ordrs")
        self.assertRejected(candidate, "table: 'Ordrs' does not match any table", "Closest: Orders")

    def test_unknown_column_is_reported(self) -> None:
        self.assertRejected(plan(metrics=[{"function": "COUNT", "column": "Profit", "alias": "m"}]), "does not match any column of Orders")

    def test_ambiguous_column_is_rejected(self) -> None:
        self.assertRejected(plan(metrics=[{"function": "COUNT", "column": "shipdate", "alias": "m"}]), "matches more than one column of Orders")

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
        self.assertRejected(plan(metrics=[{"function": "SUM", "column": "Sub_Category", "alias": "m"}]), "SUM needs a numeric column")
        self.assertRejected(plan(metrics=[{"function": "SUM", "column": "*", "alias": "m"}]), "only for COUNT")
        self.assertRejected(plan(metrics=[{"function": "MEDIAN", "column": "Sales", "alias": "m"}]), "function 'MEDIAN' is not allowed")

    def test_row_query_needs_select_and_rejects_select_with_aggregate(self) -> None:
        self.assertRejected(plan(metrics=None, select=[]), "a row query needs at least one column")
        matched = match_plan(plan(metrics=None, select=["Sub_Category"], limit=10), self.resolved)
        self.assertEqual(matched["select"], ["Sub_Category"])
        self.assertEqual(matched["limit"], 10)
        self.assertRejected(plan(select=["Sales"]), "cannot be used together with metrics")

    def test_star_select_expands_to_every_column(self) -> None:
        matched = match_plan(plan(metrics=None, select=["*"]), self.resolved)
        self.assertEqual(
            matched["select"],
            ["Sub_Category", "Ship_Date", "ShipDate", "Order_Date", "Sales", "Quantity", "Ship_Mode", "Returned"],
        )
        self.assertEqual(matched["matches"][-1]["result"], "all 8 columns")

    def test_star_select_cannot_mix_with_columns(self) -> None:
        self.assertRejected(plan(metrics=None, select=["*", "Sales"]), "'*' cannot be combined with other columns")

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

    def test_dimensions_group_by_metrics_order_and_limit_match(self) -> None:
        matched = match_plan(
            plan(
                dimensions=["sub category"],
                group_by=["Sub_Category"],
                metrics=[{"function": "SUM", "column": "sales", "alias": "total_sales"}],
                order_by=[{"ref": "total_sales", "direction": "desc"}],
                limit=5,
            ),
            self.resolved,
        )
        self.assertEqual(matched["dimensions"], ["Sub_Category"])
        self.assertEqual(matched["group_by"], ["Sub_Category"])
        self.assertEqual(matched["metrics"], [{"function": "SUM", "column": "Sales", "alias": "total_sales"}])
        self.assertEqual(matched["order_by"], [{"ref": "total_sales", "direction": "DESC"}])
        self.assertEqual(matched["select"], [])
        self.assertEqual(matched["limit"], 5)

    def test_group_by_must_match_dimensions(self) -> None:
        self.assertRejected(
            plan(dimensions=["Sub_Category"], group_by=["Ship_Mode"]),
            "group_by: must list the same columns as dimensions",
        )

    def test_dimensions_need_a_metric(self) -> None:
        self.assertRejected(plan(metrics=None, select=["Sub_Category"], dimensions=["Sub_Category"]), "dimensions: need at least one metric")

    def test_group_by_needs_a_metric(self) -> None:
        self.assertRejected(plan(metrics=None, select=["Sales"], group_by=["Sub_Category"]), "group_by: needs at least one metric")

    def test_having_references_a_metric_alias(self) -> None:
        matched = match_plan(plan(having=[{"metric": "n", "operator": ">", "value": 10}]), self.resolved)
        self.assertEqual(matched["having"], [{"metric": "n", "operator": ">", "value": 10}])
        self.assertRejected(plan(having=[{"metric": "total", "operator": ">", "value": 10}]), "does not match any metric alias")

    def test_having_between_is_checked(self) -> None:
        self.assertRejected(plan(having=[{"metric": "n", "operator": "BETWEEN", "value": [9, 2]}]), "range for n is reversed")

    def test_order_by_rejects_unknown_reference(self) -> None:
        self.assertRejected(
            plan(dimensions=["Sub_Category"], group_by=["Sub_Category"], order_by=[{"ref": "Profit", "direction": "ASC"}]),
            "does not match any output column or metric alias",
        )

    def test_order_by_direction_is_checked(self) -> None:
        self.assertRejected(
            plan(dimensions=["Sub_Category"], group_by=["Sub_Category"], order_by=[{"ref": "n", "direction": "UP"}]),
            "direction 'UP' is not allowed",
        )

    def test_single_value_aggregate_rejects_limit_and_order(self) -> None:
        self.assertRejected(plan(limit=5), "limit: a single-value aggregate has no limit")
        self.assertRejected(plan(order_by=[{"ref": "n", "direction": "ASC"}]), "order_by: a single-value aggregate has nothing to order")

    def test_row_query_order_by_uses_selected_columns(self) -> None:
        matched = match_plan(
            plan(metrics=None, select=["Sub_Category", "Sales"], order_by=[{"ref": "Sales", "direction": "desc"}]),
            self.resolved,
        )
        self.assertEqual(matched["order_by"], [{"ref": "Sales", "direction": "DESC"}])
        self.assertRejected(
            plan(metrics=None, select=["Sub_Category"], order_by=[{"ref": "Quantity", "direction": "ASC"}]),
            "does not match any output column or metric alias",
        )

    def test_alias_rules(self) -> None:
        self.assertRejected(plan(metrics=[{"function": "COUNT", "column": "*", "alias": "1bad"}]), "alias '1bad' must start")
        self.assertRejected(plan(metrics=[{"function": "COUNT", "column": "*"}]), "alias None must start")
        self.assertRejected(
            plan(metrics=[{"function": "COUNT", "column": "*", "alias": "n"}, {"function": "MAX", "column": "Sales", "alias": "n"}]),
            "alias 'n' is used more than once",
        )
        self.assertRejected(
            plan(
                dimensions=["Sub_Category"],
                group_by=["Sub_Category"],
                metrics=[{"function": "COUNT", "column": "*", "alias": "Sub_Category"}],
            ),
            "is also a dimension name",
        )

    def test_calculation_yoy_is_matched(self) -> None:
        calculation = {"type": "yoy_change", "metric": "n", "date_column": "order date", "grain": "Year", "year": 2014}
        matched = match_plan(plan(calculations=[calculation]), self.resolved)
        self.assertEqual(
            matched["calculations"],
            [{"type": "yoy_change", "metric": "n", "date_column": "Order_Date", "grain": "year", "year": 2014}],
        )

    def test_calculation_rules_are_checked(self) -> None:
        base = {"type": "percentage_growth", "metric": "n", "date_column": "Order_Date", "grain": "year", "year": 2014}
        self.assertRejected(plan(calculations=[{**base, "date_column": "Sales"}]), "date_column Sales is not a date column")
        self.assertRejected(plan(calculations=[{**base, "type": "median_change"}]), "calculation type 'median_change' is not allowed")
        self.assertRejected(plan(calculations=[{**base, "grain": "week"}]), "grain 'week' is not allowed")
        self.assertRejected(plan(calculations=[{**base, "year": "2014"}]), "year for Order_Date must be a whole number")
        self.assertRejected(plan(calculations=[{**base, "metric": "missing"}]), "does not match any metric alias")

    def test_calculation_needs_a_metric(self) -> None:
        calculation = {"type": "yoy_change", "metric": "n", "date_column": "Order_Date", "grain": "year", "year": 2014}
        self.assertRejected(plan(metrics=None, select=["Sales"], calculations=[calculation]), "calculations: needs at least one metric")

    def test_normalize_rules(self) -> None:
        self.assertEqual(normalize("Sub_Category"), "subcategory")
        self.assertEqual(normalize("sub category"), normalize("Sub_Category"))
        self.assertEqual(normalize("Phone"), normalize("Phones"))
        self.assertEqual(normalize("s"), "s")


class CapabilityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = make_resolved()
        self.having = [{"metric": "n", "operator": ">", "value": 5}]

    def test_disabled_having_rejects(self) -> None:
        with self.assertRaises(MatchError) as caught:
            match_plan(plan(having=self.having), self.resolved, capabilities={"having": False})
        self.assertTrue(any(reason.startswith("having: is turned off") for reason in caught.exception.reasons))

    def test_enabled_having_matches(self) -> None:
        matched = match_plan(plan(having=self.having), self.resolved, capabilities={"having": True})
        self.assertEqual(len(matched["having"]), 1)

    def test_no_capabilities_means_no_gate(self) -> None:
        self.assertEqual(len(match_plan(plan(having=self.having), self.resolved)["having"]), 1)

    def test_plan_without_feature_is_not_affected(self) -> None:
        matched = match_plan(plan(), self.resolved, capabilities={"having": False})
        self.assertEqual(matched["having"], [])


if __name__ == "__main__":
    unittest.main()
