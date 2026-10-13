"""Tests for the shared shapes and the strict reply parsers (task 3)."""

import unittest

from src.query.plan import CannotAnswer, PlanError, parse_reply
from src.query.shapes import CALCULATION_TYPES, DATE_GRAINS, METRIC_FUNCTIONS, PLAN_SCHEMA_VERSION, SQL_SCHEMA_VERSION, SORT_DIRECTIONS
from src.query.sql_reply import parse_sql_reply


class SqlReplyTests(unittest.TestCase):
    def test_accepts_sql_and_params(self):
        out = parse_sql_reply('{"sql": "SELECT 1 WHERE a = ?", "params": ["x"]}')
        self.assertEqual(out, {"sql": "SELECT 1 WHERE a = ?", "params": ["x"]})

    def test_accepts_fenced_reply(self):
        out = parse_sql_reply('```json\n{"sql": "SELECT 1", "params": []}\n```')
        self.assertEqual(out["sql"], "SELECT 1")

    def test_rejects_extra_key(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('{"sql": "SELECT 1", "params": [], "note": "x"}')

    def test_rejects_missing_params(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('{"sql": "SELECT 1"}')

    def test_rejects_empty_sql(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('{"sql": "  ", "params": []}')

    def test_rejects_non_list_params(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('{"sql": "SELECT 1", "params": {"a": 1}}')

    def test_rejects_text_outside_json(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('Here you go: {"sql": "SELECT 1", "params": []}')

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(PlanError):
            parse_sql_reply('{"sql": "A", "sql": "B", "params": []}')


class PlanReplyTests(unittest.TestCase):
    def test_accepts_minimal_plan(self):
        out = parse_reply(
            '{"table": "t", "select": [], "dimensions": [], "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}], '
            '"filters": [], "group_by": [], "having": [], "order_by": [], "calculations": [], "limit": null}'
        )
        self.assertEqual(out["table"], "t")

    def test_cannot_answer_reply(self):
        with self.assertRaises(CannotAnswer) as ctx:
            parse_reply('{"cannot_answer": true, "reason": "no data"}')
        self.assertEqual(ctx.exception.reason, "no data")

    def test_rejects_non_object(self):
        with self.assertRaises(PlanError):
            parse_reply("[1, 2]")


class ShapeTests(unittest.TestCase):
    def test_versions_are_set(self):
        self.assertEqual(PLAN_SCHEMA_VERSION, 1)
        self.assertEqual(SQL_SCHEMA_VERSION, 1)

    def test_supported_vocabulary(self):
        self.assertEqual(METRIC_FUNCTIONS, ("COUNT", "SUM", "AVG", "MIN", "MAX", "SHARE"))
        self.assertEqual(DATE_GRAINS, ("year", "quarter", "month", "year_month"))
        self.assertEqual(CALCULATION_TYPES, ("yoy_change", "percentage_growth"))
        self.assertEqual(SORT_DIRECTIONS, ("ASC", "DESC"))


if __name__ == "__main__":
    unittest.main()
