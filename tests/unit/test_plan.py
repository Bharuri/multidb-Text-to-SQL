import json
import unittest

from src.query.plan import CannotAnswer, PlanError, parse_reply

VALID_PLAN = {
    "table": "Orders",
    "select": [],
    "aggregation": {"function": "COUNT", "column": "*"},
    "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones"}],
    "limit": None,
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
        raw = as_json({**VALID_PLAN, "order_by": "Sales"})
        with self.assertRaises(PlanError) as context:
            parse_reply(raw)
        self.assertIn("order_by", " ".join(context.exception.reasons))

    def test_rejects_a_missing_key(self) -> None:
        plan = {key: value for key, value in VALID_PLAN.items() if key != "limit"}
        with self.assertRaises(PlanError) as context:
            parse_reply(as_json(plan))
        self.assertIn("limit", " ".join(context.exception.reasons))

    def test_rejects_a_duplicate_key(self) -> None:
        raw = '{"table": "Orders", "table": "Other", "select": [], "aggregation": null, "filters": [], "limit": null}'
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

    def test_rejects_an_aggregation_with_extra_keys(self) -> None:
        plan = {**VALID_PLAN, "aggregation": {"function": "SUM", "column": "Sales", "alias": "t"}}
        with self.assertRaises(PlanError):
            parse_reply(as_json(plan))

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
