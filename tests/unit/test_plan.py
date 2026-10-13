import json
import unittest

from src.query.plan import CannotAnswer, PlanError, parse_reply

VALID_PLAN = {
    "table": "Orders",
    "select": [],
    "dimensions": [],
    "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}],
    "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones"}],
    "group_by": [],
    "having": [],
    "order_by": [],
    "calculations": [],
    "limit": None,
}

GROUPED_PLAN = {
    **VALID_PLAN,
    "dimensions": ["Sub_Category"],
    "metrics": [
        {"function": "SUM", "column": "Sales", "alias": "total"},
        {"function": "COUNT", "column": "*", "alias": "orders"},
    ],
    "filters": [],
    "group_by": ["Sub_Category"],
    "having": [{"metric": "orders", "operator": ">", "value": 10}],
    "order_by": [{"ref": "total", "direction": "DESC"}],
    "calculations": [
        {"type": "yoy_change", "metric": "total", "date_column": "Order_Date", "grain": "year", "year": 2014}
    ],
    "limit": 5,
}


def as_json(obj: object) -> str:
    return json.dumps(obj)


class ParseReplyTests(unittest.TestCase):
    def test_accepts_a_bare_plan(self) -> None:
        self.assertEqual(parse_reply(as_json(VALID_PLAN)), VALID_PLAN)

    def test_accepts_a_plan_inside_a_json_fence(self) -> None:
        raw = "```json\n" + as_json(VALID_PLAN) + "\n```"
        self.assertEqual(parse_reply(raw), VALID_PLAN)

    def test_accepts_a_fence_without_a_language_tag(self) -> None:
        raw = "```\n" + as_json(VALID_PLAN) + "\n```"
        self.assertEqual(parse_reply(raw), VALID_PLAN)

    def test_rejects_text_before_or_after_the_fence(self) -> None:
        for raw in (
            "Here is the plan:\n```json\n" + as_json(VALID_PLAN) + "\n```",
            "```json\n" + as_json(VALID_PLAN) + "\n```\nHope this helps",
        ):
            with self.subTest(raw=raw[:20]), self.assertRaises(PlanError):
                parse_reply(raw)

    def test_rejects_an_extra_key(self) -> None:
        raw = as_json({**VALID_PLAN, "sort": "Sales"})
        with self.assertRaises(PlanError) as context:
            parse_reply(raw)
        self.assertIn("sort", " ".join(context.exception.reasons))

    def test_accepts_a_grouped_plan_with_all_optional_parts(self) -> None:
        self.assertEqual(parse_reply(as_json(GROUPED_PLAN)), GROUPED_PLAN)

    def test_null_optional_lists_are_accepted(self) -> None:
        plan = {**VALID_PLAN, "dimensions": None, "metrics": None, "having": None, "order_by": None, "calculations": None}
        self.assertEqual(parse_reply(as_json(plan))["metrics"], None)

    def test_a_row_query_plan_is_accepted(self) -> None:
        plan = {**VALID_PLAN, "select": ["Sub_Category", "Sales"], "metrics": None}
        self.assertEqual(parse_reply(as_json(plan)), plan)

    def test_rejects_a_list_field_that_is_not_a_list(self) -> None:
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "dimensions": "Sub_Category", "group_by": 3}))
        self.assertIn("dimensions must be a list or null", context.exception.reasons)
        self.assertIn("group_by must be a list or null", context.exception.reasons)

    def test_rejects_a_missing_key(self) -> None:
        plan = {key: value for key, value in VALID_PLAN.items() if key != "limit"}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json(plan))
        self.assertIn("limit", " ".join(context.exception.reasons))

    def test_rejects_a_duplicate_key(self) -> None:
        raw = '{"table": "Orders", "table": "Other", "select": [], "dimensions": [], "metrics": null, "filters": [], "group_by": [], "having": [], "order_by": [], "calculations": [], "limit": null}'
        with self.assertRaises(PlanError) as context:
            parse_reply(raw)
        self.assertIn("duplicate key", " ".join(context.exception.reasons))

    def test_rejects_text_that_is_not_json(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply("I think the answer is 903.")

    def test_rejects_a_json_array(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply("[" + as_json(VALID_PLAN) + "]")

    def test_rejects_trailing_text_after_the_object(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply(as_json(VALID_PLAN) + " extra")

    def test_rejects_a_filter_with_an_extra_key(self) -> None:
        plan = {**VALID_PLAN, "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones", "case": "x"}]}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json(plan))
        self.assertIn("filters[0]", " ".join(context.exception.reasons))

    def test_rejects_a_non_list_select_and_filters(self) -> None:
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "select": "Sales", "filters": {}}))
        self.assertEqual(len(context.exception.reasons), 2)

    def test_rejects_a_metric_with_extra_keys(self) -> None:
        plan = {**VALID_PLAN, "metrics": [{"function": "SUM", "column": "Sales", "alias": "t", "format": "x"}]}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json(plan))
        self.assertIn("metrics[0]", " ".join(context.exception.reasons))

    def test_rejects_a_metric_without_an_alias(self) -> None:
        plan = {**VALID_PLAN, "metrics": [{"function": "SUM", "column": "Sales"}]}
        with self.assertRaises(PlanError):
            parse_reply(as_json(plan))

    def test_rejects_a_metric_that_is_not_an_object(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply(as_json({**VALID_PLAN, "metrics": ["SUM(Sales)"]}))

    def test_rejects_an_unsupported_calculation_type(self) -> None:
        calc = {"type": "median_change", "metric": "n", "date_column": "Order_Date", "grain": "year", "year": 2014}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "calculations": [calc]}))
        self.assertIn("calculation type 'median_change' is not supported", " ".join(context.exception.reasons))

    def test_rejects_a_calculation_with_a_non_integer_year(self) -> None:
        calc = {"type": "percentage_growth", "metric": "n", "date_column": "Order_Date", "grain": "year", "year": "2014"}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "calculations": [calc]}))
        self.assertIn("year must be a whole number", " ".join(context.exception.reasons))

    def test_rejects_a_calculation_with_missing_or_extra_keys(self) -> None:
        calc = {"type": "yoy_change", "metric": "n", "date_column": "Order_Date", "grain": "year", "extra": 1}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "calculations": [calc]}))
        text = " ".join(context.exception.reasons)
        self.assertIn("extra", text)
        self.assertIn("year must be a whole number", text)

    def test_rejects_a_having_clause_with_extra_keys(self) -> None:
        having = [{"metric": "n", "operator": ">", "value": 1, "logic": "or"}]
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "having": having}))
        self.assertIn("having[0]", " ".join(context.exception.reasons))

    def test_rejects_an_order_by_without_direction(self) -> None:
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json({**VALID_PLAN, "order_by": [{"ref": "n"}]}))
        self.assertIn("order_by[0]", " ".join(context.exception.reasons))

    def test_rejects_a_boolean_limit(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply(as_json({**VALID_PLAN, "limit": True}))

    def test_accepts_an_integer_limit(self) -> None:
        self.assertEqual(parse_reply(as_json({**VALID_PLAN, "limit": 50}))["limit"], 50)


class CannotAnswerTests(unittest.TestCase):
    def test_cannot_answer_reply_raises_with_the_reason(self) -> None:
        raw = as_json({"cannot_answer": True, "reason": "Grouping by month is not supported."})
        with self.assertRaises(CannotAnswer) as context:
            parse_reply(raw)
        self.assertEqual(context.exception.reason, "Grouping by month is not supported.")

    def test_cannot_answer_inside_a_fence(self) -> None:
        raw = "```json\n" + as_json({"cannot_answer": True, "reason": "No such column."}) + "\n```"
        with self.assertRaises(CannotAnswer):
            parse_reply(raw)

    def test_cannot_answer_false_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply(as_json({"cannot_answer": False, "reason": "x"}))

    def test_cannot_answer_with_extra_keys_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            parse_reply(as_json({"cannot_answer": True, "reason": "x", "table": "Orders"}))

    def test_cannot_answer_without_a_reason_is_rejected(self) -> None:
        for reason in ("", "   ", None):
            with self.subTest(reason=reason), self.assertRaises(PlanError):
                parse_reply(as_json({"cannot_answer": True, "reason": reason}))


if __name__ == "__main__":
    unittest.main()
