"""Streamlit AppTest checks for the metric confirmation panel (offline proposer, temp business file)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from streamlit.testing.v1 import AppTest


def _panel_script():
    import dataclasses
    import json
    import os
    from pathlib import Path

    import streamlit as st

    from src.query import catalog as cat
    from src.query import config as cfg
    from src.ui.metric_confirmation import render_metric_confirmation

    registry = cfg.load_registry(cfg.CONFIG_PATH)
    key = "sample_superstore"
    entry = dataclasses.replace(
        registry.databases[key], business_mapping=Path(os.environ["MC_BUSINESS"])
    )
    registry = dataclasses.replace(registry, databases={key: entry})
    catalog = cat.load_catalog(registry, key)

    proposal = {
        "name": "total_profit",
        "synonyms": ["profit total"],
        "kind": "aggregate",
        "expression": {"table": "Orders", "column": "Profit", "aggregate": "sum"},
        "unit": "currency",
        "grain": "whole selection",
        "null_zero_policy": "NULL when there are no rows",
        "description": "Sum of profit.",
    }

    def proposer(prompt):
        return json.dumps(proposal)

    render_metric_confirmation(st, catalog, entry, os.environ["MC_TERM"], "what is total profit?", proposer)


class MetricPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.business = self.tmp / "sample_superstore.yaml"
        self._old = {k: os.environ.get(k) for k in ("MC_BUSINESS", "MC_TERM")}
        os.environ["MC_BUSINESS"] = str(self.business)
        os.environ["MC_TERM"] = "total profit"

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self) -> AppTest:
        return AppTest.from_function(_panel_script, default_timeout=60).run()

    def _labels(self, at):
        return [b.label for b in at.button]

    def test_undefined_term_offers_proposal_and_saves_nothing_until_accept(self):
        at = self._app()
        self.assertFalse(at.exception)
        self.assertEqual(self._labels(at), ["Propose a definition"])

        at.button[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self._labels(at), ["Propose a definition", "Accept", "Reject"])
        self.assertFalse(self.business.exists(), "a proposal must not be saved before the user accepts it")

    def test_accept_saves_approved_metric_and_next_run_resolves_it(self):
        at = self._app()
        at.button[0].click().run()
        accept = next(b for b in at.button if b.label == "Accept")
        accept.click().run()
        self.assertFalse(at.exception)

        data = yaml.safe_load(self.business.read_text(encoding="utf-8"))
        self.assertEqual(data["database"], "sample_superstore")
        self.assertEqual([m["status"] for m in data["metrics"]], ["approved"])

        fresh = self._app()
        self.assertFalse(fresh.exception)
        self.assertEqual(self._labels(fresh), [])
        self.assertTrue(any("approved" in c.value for c in fresh.caption))

    def test_reject_saves_nothing_and_offers_propose_again(self):
        at = self._app()
        at.button[0].click().run()
        reject = next(b for b in at.button if b.label == "Reject")
        reject.click().run()
        self.assertFalse(at.exception)
        self.assertFalse(self.business.exists())
        self.assertEqual(self._labels(at), ["Propose a definition"])


if __name__ == "__main__":
    unittest.main()
