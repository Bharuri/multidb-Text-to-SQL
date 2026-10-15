"""Metric resolution, proposal generation (offline proposer), and accept-to-business-file."""

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from src.query import catalog as cat
from src.query import config as cfg
from src.query import metrics as mx
from tests.test_catalog_metrics import (
    PROFIT_MARGIN,
    _minimal_physical,
    _minimal_semantic,
)

KEY = "sample_superstore"

GOOD_PROPOSAL = {
    "name": "total_profit",
    "synonyms": ["profit total"],
    "kind": "aggregate",
    "expression": {"table": "Orders", "column": "Profit", "aggregate": "sum"},
    "unit": "currency",
    "grain": "whole selection",
    "null_zero_policy": "NULL when there are no rows",
    "description": "Sum of profit.",
}


def fake_proposer(reply):
    calls = []

    def propose(prompt):
        calls.append(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)

    propose.calls = calls
    return propose


class MetricTestBase(unittest.TestCase):
    def setUp(self):
        self.registry = cfg.load_registry(cfg.CONFIG_PATH)
        self.tmp = Path(tempfile.mkdtemp())
        self.business = self.tmp / "sample_superstore.yaml"
        self.entry = dataclasses.replace(self.registry.databases[KEY], business_mapping=self.business)
        self.catalog = cat.load_catalog(self.registry, KEY)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ResolutionTests(MetricTestBase):
    def setUp(self):
        super().setUp()
        self.catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]))

    def test_approved_metric_resolves_and_is_usable_in_sql(self):
        resolution = mx.resolve_term(self.catalog, "margin")
        self.assertEqual(resolution.outcome, mx.APPROVED)
        self.assertEqual(resolution.metric.name, "profit_margin")

    def test_unknown_term_is_undefined(self):
        self.assertEqual(mx.resolve_term(self.catalog, "revenue").outcome, mx.UNDEFINED)

    def test_proposed_metric_is_never_usable_in_sql(self):
        proposed = dict(PROFIT_MARGIN, name="margin_draft", synonyms=["draft margin"], status="proposed")
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([proposed]))
        self.assertEqual(mx.resolve_term(catalog, "draft margin").outcome, mx.PROPOSED)


class ProposalTests(MetricTestBase):
    def test_proposal_is_marked_proposed_and_term_added_as_synonym(self):
        proposer = fake_proposer(GOOD_PROPOSAL)
        definition = mx.propose_metric(self.catalog, "Total Profit", "what is total profit?", proposer)
        self.assertEqual(definition["status"], "proposed")
        self.assertEqual(definition["name"], "total_profit")
        self.assertIn("Total Profit", definition["synonyms"])
        self.assertIn("profit total", definition["synonyms"])

    def test_prompt_lists_real_columns_and_term(self):
        proposer = fake_proposer(GOOD_PROPOSAL)
        mx.propose_metric(self.catalog, "total profit", "what is total profit?", proposer)
        prompt = proposer.calls[0]
        self.assertIn("total profit", prompt)
        self.assertIn('"column": "Profit"', prompt)

    def test_json_wrapped_in_prose_is_accepted(self):
        reply = "Here you go:\n" + json.dumps(GOOD_PROPOSAL) + "\nHope it helps."
        definition = mx.propose_metric(self.catalog, "total profit", "q", fake_proposer(reply))
        self.assertEqual(definition["kind"], "aggregate")

    def test_proposal_with_unknown_column_is_rejected(self):
        bad = json.loads(json.dumps(GOOD_PROPOSAL))
        bad["expression"]["column"] = "Revenue"
        with self.assertRaisesRegex(mx.ProposalError, "Revenue"):
            mx.propose_metric(self.catalog, "total profit", "q", fake_proposer(bad))

    def test_non_json_reply_is_rejected(self):
        with self.assertRaisesRegex(mx.ProposalError, "JSON"):
            mx.propose_metric(self.catalog, "total profit", "q", fake_proposer("I cannot help with that."))

    def test_already_defined_term_does_not_call_proposer(self):
        proposer = fake_proposer(GOOD_PROPOSAL)
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]))
        with self.assertRaises(mx.MetricError):
            mx.propose_metric(catalog, "margin", "q", proposer)
        self.assertEqual(proposer.calls, [])

    def test_proposal_alone_writes_nothing(self):
        mx.propose_metric(self.catalog, "total profit", "q", fake_proposer(GOOD_PROPOSAL))
        self.assertFalse(self.business.exists())


class AcceptTests(MetricTestBase):
    def _proposal(self):
        return mx.propose_metric(self.catalog, "total profit", "q", fake_proposer(GOOD_PROPOSAL))

    def test_accept_creates_business_file_with_approved_metric(self):
        path = mx.accept_proposal(self.catalog, self.entry, self._proposal())
        self.assertEqual(path, self.business)
        data = yaml.safe_load(self.business.read_text(encoding="utf-8"))
        self.assertEqual(data["database"], KEY)
        self.assertEqual([m["status"] for m in data["metrics"]], ["approved"])

    def test_accepted_metric_loads_as_business_source_and_is_sql_usable(self):
        mx.accept_proposal(self.catalog, self.entry, self._proposal())
        reloaded = cat.load_catalog(dataclasses.replace(self.registry, databases={KEY: self.entry}), KEY)
        self.assertEqual(mx.resolve_term(reloaded, "profit total").metric.source, "business")

    def test_accept_keeps_existing_keys_and_metrics(self):
        self.business.write_text(
            yaml.safe_dump({"database": KEY, "concepts": {"x": "y"}, "metrics": []}, sort_keys=False),
            encoding="utf-8",
        )
        mx.accept_proposal(self.catalog, self.entry, self._proposal())
        data = yaml.safe_load(self.business.read_text(encoding="utf-8"))
        self.assertEqual(data["concepts"], {"x": "y"})
        self.assertEqual(len(data["metrics"]), 1)

    def test_accept_replaces_same_name_instead_of_duplicating(self):
        mx.accept_proposal(self.catalog, self.entry, self._proposal())
        mx.accept_proposal(self.catalog, self.entry, self._proposal())
        data = yaml.safe_load(self.business.read_text(encoding="utf-8"))
        self.assertEqual([m["name"] for m in data["metrics"]], ["total_profit"])

    def test_accept_refuses_non_proposed_definition(self):
        definition = dict(self._proposal(), status="approved")
        with self.assertRaisesRegex(mx.MetricError, "only a proposed"):
            mx.accept_proposal(self.catalog, self.entry, definition)
        self.assertFalse(self.business.exists())

    def test_accept_refuses_business_file_for_another_database(self):
        self.business.write_text(yaml.safe_dump({"database": "airbnb", "metrics": []}), encoding="utf-8")
        with self.assertRaisesRegex(mx.MetricError, "registry key"):
            mx.accept_proposal(self.catalog, self.entry, self._proposal())

    def test_rejected_proposal_is_not_saved(self):
        self._proposal()  # the UI calls propose, then discards it on reject
        self.assertFalse(self.business.exists())


class PromptAndProposerTests(unittest.TestCase):
    def test_bedrock_proposer_builds_without_network(self):
        settings = cfg.LlmSettings(
            provider="bedrock", model_id="m", region="us-east-1", temperature=0.0, max_output_tokens=100
        )
        proposer = mx.bedrock_proposer(settings, profile_name=None)
        self.assertTrue(callable(proposer))


if __name__ == "__main__":
    unittest.main()
