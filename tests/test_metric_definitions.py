"""Tests for metric definition parsing and validation."""

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from src.mapping import requirements_to_yaml as gen
from src.mapping.metadata_validator import validate_metrics

FILE = "demo.md"

SEMANTIC_COLUMNS = [
    {"table": "Orders", "column": "Sales", "data_type": "DOUBLE", "column_class": "numeric", "values": []},
    {"table": "Orders", "column": "Row_ID", "data_type": "BIGINT", "column_class": "numeric", "values": []},
    {"table": "Orders", "column": "Profit", "data_type": "DOUBLE", "column_class": "numeric", "values": []},
    {"table": "Orders", "column": "Discount", "data_type": "DOUBLE", "column_class": "categorical", "values": ["0.0", "0.2"]},
    {"table": "Orders", "column": "Region", "data_type": "VARCHAR", "column_class": "categorical", "values": ["East", "West"]},
]

VALID_METRICS = [
    {
        "name": "profit_margin",
        "synonyms": ["margin"],
        "status": "approved",
        "kind": "ratio",
        "unit": "ratio",
        "grain": "whole selection",
        "null_zero_policy": "NULL when denominator is 0",
        "description": "Profit over sales.",
        "numerator": {"table": "Orders", "column": "Profit", "aggregate": "sum"},
        "denominator": {"table": "Orders", "column": "Sales", "aggregate": "sum"},
    },
    {
        "name": "weighted_discount",
        "synonyms": [],
        "status": "proposed",
        "kind": "weighted_average",
        "unit": "ratio",
        "grain": "whole selection",
        "null_zero_policy": "NULL when weight is 0",
        "description": "Discount weighted by sales.",
        "value": {"table": "Orders", "column": "Discount"},
        "weight": {"table": "Orders", "column": "Sales"},
    },
    {
        "name": "loss_lines",
        "synonyms": [],
        "status": "approved",
        "kind": "aggregate",
        "unit": "count",
        "grain": "order line",
        "null_zero_policy": "0 when no rows match",
        "description": "Lines with a loss.",
        "expression": {
            "table": "Orders",
            "column": "Row_ID",
            "aggregate": "count",
            "filters": [{"column": "Profit", "operator": "<", "value": 0}],
        },
    },
    {
        "name": "east_share",
        "synonyms": [],
        "status": "approved",
        "kind": "ratio",
        "unit": "ratio",
        "grain": "order line",
        "null_zero_policy": "NULL when denominator is 0",
        "description": "Share of lines in East.",
        "numerator": {
            "table": "Orders",
            "column": "Sales",
            "aggregate": "sum",
            "filters": [{"column": "Region", "operator": "=", "value": "East"}],
        },
        "denominator": {"table": "Orders", "column": "Sales", "aggregate": "sum"},
    },
]


def errors_for(metrics):
    return validate_metrics(copy.deepcopy(metrics), FILE, SEMANTIC_COLUMNS)


def contains(errors, *fragments):
    return any(all(fragment in error for fragment in fragments) for error in errors)


class ValidateMetricsTests(unittest.TestCase):
    def test_valid_metrics_have_no_errors(self):
        self.assertEqual(errors_for(VALID_METRICS), [])

    def test_empty_list_is_valid(self):
        self.assertEqual(errors_for([]), [])

    def test_metrics_must_be_a_list(self):
        self.assertTrue(contains(validate_metrics({"name": "x"}, FILE, SEMANTIC_COLUMNS), "must be a list"))

    def test_unknown_column_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[0]["numerator"]["column"] = "Margin"
        self.assertTrue(contains(errors_for(metrics), "Orders.Margin is not in the semantic columns"))

    def test_sum_on_text_column_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[0]["numerator"] = {"table": "Orders", "column": "Region", "aggregate": "sum"}
        self.assertTrue(contains(errors_for(metrics), "sum needs a numeric column", "VARCHAR"))

    def test_weighted_average_accepts_numeric_type_even_if_categorical(self):
        # Discount is DOUBLE with 2 values, so the SQL works and the rule must allow it.
        self.assertEqual(errors_for([VALID_METRICS[1]]), [])

    def test_weighted_average_rejects_text_value(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[1]["value"] = {"table": "Orders", "column": "Region"}
        self.assertTrue(contains(errors_for(metrics), "must be numeric for a weighted average"))

    def test_categorical_filter_rejects_range_operator(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[3]["numerator"]["filters"][0]["operator"] = ">"
        self.assertTrue(contains(errors_for(metrics), "not allowed on categorical columns"))

    def test_categorical_filter_value_must_be_stored(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[3]["numerator"]["filters"][0]["value"] = "Central"
        self.assertTrue(contains(errors_for(metrics), "'Central' is not a stored value"))

    def test_numeric_filter_value_must_be_a_number(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[2]["expression"]["filters"][0]["value"] = "zero"
        self.assertTrue(contains(errors_for(metrics), "must be a number for numeric column"))

    def test_unknown_aggregate_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[0]["numerator"]["aggregate"] = "median"
        self.assertTrue(contains(errors_for(metrics), "aggregate: must be one of"))

    def test_duplicate_name_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[1]["name"] = "profit_margin"
        self.assertTrue(contains(errors_for(metrics), "'profit_margin' is defined more than once"))

    def test_name_must_be_snake_case(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[0]["name"] = "Profit Margin"
        self.assertTrue(contains(errors_for(metrics), "snake_case"))

    def test_unknown_status_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        metrics[0]["status"] = "draft"
        self.assertTrue(contains(errors_for(metrics), "status: must be one of"))

    def test_missing_kind_key_is_rejected(self):
        metrics = copy.deepcopy(VALID_METRICS)
        del metrics[0]["denominator"]
        self.assertTrue(contains(errors_for(metrics), "denominator"))


class ParseMetricsTests(unittest.TestCase):
    def setUp(self):
        self.block = [
            "## Metric definitions",
            "",
            "```yaml",
            "metrics:",
            "- name: profit_margin",
            "  status: approved",
            "```",
            "",
            "## Acceptance criteria",
            "",
            "- done",
        ]

    def test_parses_one_fenced_block(self):
        metrics = gen.parse_metrics(self.block)
        self.assertEqual(metrics, [{"name": "profit_margin", "status": "approved"}])

    def test_empty_metrics_list_is_allowed(self):
        lines = ["## Metric definitions", "", "```yaml", "metrics: []", "```"]
        self.assertEqual(gen.parse_metrics(lines), [])

    def test_missing_section_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing '## Metric definitions'"):
            gen.parse_metrics(["## Acceptance criteria", "- done"])

    def test_missing_fenced_block_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "one fenced yaml block"):
            gen.parse_metrics(["## Metric definitions", "", "no block here"])

    def test_wrong_top_level_key_is_rejected(self):
        lines = ["## Metric definitions", "```yaml", "metric: []", "```"]
        with self.assertRaisesRegex(ValueError, "'metrics' list"):
            gen.parse_metrics(lines)

    def test_crlf_file_parses_through_read_lines(self):
        text = "\r\n".join(self.block) + "\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.md"
            path.write_bytes(text.encode("utf-8"))
            metrics = gen.parse_metrics(gen.read_lines(path))
        self.assertEqual(metrics, [{"name": "profit_margin", "status": "approved"}])

    def test_yes_filter_value_stays_a_string_when_quoted(self):
        lines = ["## Metric definitions", "```yaml", "metrics:", '- value: "Yes"', "```"]
        metrics = gen.parse_metrics(lines)
        self.assertEqual(metrics[0]["value"], "Yes")


class GeneratedSeedMetricsTests(unittest.TestCase):
    """No dataset ships hand-authored metrics. Every generated semantic YAML holds an empty list."""

    def load(self, stem):
        path = gen.SEMANTIC_YAML_DIR / f"{stem}.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_no_authored_metrics_per_database(self):
        for stem in ("sample_superstore", "airbnb", "_113thCongress", "netflix_titles"):
            self.assertEqual(self.load(stem)["metrics"], [], stem)


if __name__ == "__main__":
    unittest.main()
