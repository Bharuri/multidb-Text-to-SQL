import copy
import dataclasses
import json
import unittest
from typing import Any

from src.query.business_semantics import BusinessError, validate_business
from src.query.matcher import MatchError, match_plan
from src.query.plan import PlanError, parse_reply
from src.query.sql_builder import build_sql
from src.query.validator import validate
from tests.unit.test_matcher import make_resolved

SHARE_WHERE = [{"column": "Sales", "operator": "<", "value": 0}]

BASE_PLAN: dict[str, Any] = {
    "table": "Orders",
    "select": [],
    "dimensions": ["Sub_Category"],
    "metrics": [
        {"function": "SUM", "column": "Sales", "alias": "net_sales"},
        {"function": "SHARE", "column": "*", "alias": "loss_share", "where": SHARE_WHERE},
    ],
    "filters": [],
    "group_by": ["Sub_Category"],
    "having": [{"metric": "net_sales", "operator": ">", "value": 0}],
    "order_by": [{"ref": "loss_share", "direction": "DESC"}],
    "calculations": [],
    "limit": None,
}


def plan(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE_PLAN)
    data.update(overrides)
    return data


def share_metric(**overrides: Any) -> dict[str, Any]:
    metric: dict[str, Any] = {"function": "SHARE", "column": "*", "alias": "loss_share", "where": SHARE_WHERE}
    metric.update(overrides)
    return metric


def resolved_with_schema():
    resolved = make_resolved()
    physical = dict(resolved.physical, schema="demo")
    return dataclasses.replace(resolved, physical=physical)


class PlanShareTests(unittest.TestCase):
    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        return parse_reply(json.dumps(data))

    def test_share_with_where_is_accepted(self) -> None:
        self.assertEqual(self.parse(plan())["metrics"][1]["function"], "SHARE")

    def test_share_without_where_is_rejected(self) -> None:
        metric = share_metric()
        del metric["where"]
        with self.assertRaises(PlanError):
            self.parse(plan(metrics=[metric]))

    def test_share_with_empty_where_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.parse(plan(metrics=[share_metric(where=[])]))

    def test_where_on_non_share_metric_is_rejected(self) -> None:
        metric = {"function": "SUM", "column": "Sales", "alias": "total", "where": SHARE_WHERE}
        with self.assertRaises(PlanError):
            self.parse(plan(metrics=[metric]))


class MatchShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = make_resolved()

    def test_share_metric_is_matched_with_where_conditions(self) -> None:
        share = match_plan(plan(), self.resolved)["metrics"][1]
        self.assertEqual(share["function"], "SHARE")
        self.assertEqual(share["column"], "*")
        self.assertEqual(share["where"], [{"column": "Sales", "operator": "<", "value": 0}])

    def test_share_column_other_than_star_is_rejected(self) -> None:
        with self.assertRaises(MatchError):
            match_plan(plan(metrics=[share_metric(column="Sales")]), self.resolved)

    def test_share_where_on_unknown_column_is_rejected(self) -> None:
        bad = share_metric(where=[{"column": "Nope", "operator": "<", "value": 0}])
        with self.assertRaises(MatchError):
            match_plan(plan(metrics=[bad]), self.resolved)


class BuildShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = resolved_with_schema()

    def build(self, data: dict[str, Any]) -> dict[str, Any]:
        built = build_sql(match_plan(data, self.resolved), "demo", "demo", default_row_limit=1000)
        assert built is not None
        return built

    def test_share_in_select_and_order_by_has_exact_sql(self) -> None:
        built = self.build(plan(having=[]))
        self.assertEqual(
            built["sql"],
            'SELECT "Sub_Category", SUM("Sales") AS "net_sales", '
            'COUNT(*) FILTER (WHERE "Sales" < ?) / NULLIF(COUNT(*), ?) AS "loss_share" '
            'FROM "demo"."demo"."Orders" GROUP BY "Sub_Category" ORDER BY "loss_share" DESC LIMIT 1000',
        )
        self.assertEqual(built["params"], [0, 0])

    def test_share_in_having_puts_its_params_after_select_params(self) -> None:
        having = [
            {"metric": "net_sales", "operator": ">", "value": 0},
            {"metric": "loss_share", "operator": ">", "value": 0.3},
        ]
        built = self.build(plan(having=having))
        self.assertIn(
            'HAVING SUM("Sales") > ? AND COUNT(*) FILTER (WHERE "Sales" < ?) / NULLIF(COUNT(*), ?) > ?',
            built["sql"],
        )
        # SELECT share: filter 0, NULLIF 0. HAVING: SUM threshold 0, share filter 0, NULLIF 0, share threshold 0.3.
        self.assertEqual(built["params"], [0, 0, 0, 0, 0, 0.3])
        self.assertEqual(len(built["params"]), built["sql"].count("?"))

    def test_built_share_sql_is_valid(self) -> None:
        matched = match_plan(plan(), self.resolved)
        built = build_sql(matched, "demo", "demo", default_row_limit=1000)
        assert built is not None
        self.assertEqual(validate(built, self.resolved, default_row_limit=1000, matched=matched), {"valid": True, "reasons": []})


class ValidateShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = resolved_with_schema()
        self.matched = match_plan(plan(), self.resolved)
        built = build_sql(self.matched, "demo", "demo", default_row_limit=1000)
        assert built is not None
        self.built = built

    def check(self, built: dict[str, Any]) -> dict[str, Any]:
        return validate(built, self.resolved, default_row_limit=1000, matched=self.matched)

    def test_nonzero_denominator_parameter_is_rejected(self) -> None:
        built = copy.deepcopy(self.built)
        built["params"] = [0, 1]
        result = self.check(built)
        self.assertFalse(result["valid"])

    def test_filter_outside_share_shape_is_rejected(self) -> None:
        built = copy.deepcopy(self.built)
        built["sql"] = built["sql"].replace('SUM("Sales")', 'SUM("Sales") FILTER (WHERE "Sales" < ?)', 1)
        built["params"] = [0] + built["params"]
        self.assertFalse(self.check(built)["valid"])

    def test_case_expression_is_rejected(self) -> None:
        built = copy.deepcopy(self.built)
        built["sql"] = built["sql"].replace(
            'SUM("Sales")', 'SUM(CASE WHEN "Sales" < 0 THEN 1 ELSE 0 END)', 1
        )
        self.assertFalse(self.check(built)["valid"])


class LossDefinitionTests(unittest.TestCase):
    def definition(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": "loss",
            "table": "Orders",
            "column": "Sales",
            "operator": "<",
            "value": 0,
            "description": "An order line that lost money.",
        }
        data.update(overrides)
        return data

    def business(self, *definitions: dict[str, Any]) -> dict[str, Any]:
        return {"database": "demo", "concepts": [], "definitions": list(definitions)}

    def test_loss_definition_loads_and_appears_in_prompt_text(self) -> None:
        loaded = validate_business(self.business(self.definition()), make_resolved())
        self.assertEqual(len(loaded.definitions), 1)
        self.assertIn("loss = Orders.Sales < 0", loaded.prompt_text())

    def test_unknown_operator_is_rejected(self) -> None:
        with self.assertRaises(BusinessError):
            validate_business(self.business(self.definition(operator="LIKE")), make_resolved())

    def test_duplicate_name_is_rejected(self) -> None:
        with self.assertRaises(BusinessError):
            validate_business(self.business(self.definition(), self.definition()), make_resolved())

    def test_unknown_column_is_rejected(self) -> None:
        with self.assertRaises(BusinessError):
            validate_business(self.business(self.definition(column="Nope")), make_resolved())


if __name__ == "__main__":
    unittest.main()
