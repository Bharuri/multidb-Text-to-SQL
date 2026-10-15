"""Checks the benchmark manifest against the source question file and config."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "benchmark" / "manifest.yaml"
QUESTIONS = ROOT / "specs" / "retrival" / "evaluation_question.md"
CONFIG = ROOT / "config.yaml"

ALLOWED_STATUS = {"answerable", "clarification", "needs_config", "profile_first", "gap"}
# Statuses that must explain themselves: a named blocker or a named config/profile need.
NEEDS_REASON = {
    "gap": "blockers",
    "needs_config": "config_needs",
    "profile_first": "profile_needs",
}


def _source_ids() -> list[str]:
    text = QUESTIONS.read_text(encoding="utf-8")
    return re.findall(r"^### ([A-Z]{2}\d{2}) ", text, flags=re.MULTILINE)


def _load_manifest() -> dict:
    with MANIFEST.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _config_databases() -> set[str]:
    with CONFIG.open(encoding="utf-8") as handle:
        return set(yaml.safe_load(handle)["databases"])


class BenchmarkManifestTests(unittest.TestCase):
    def test_covers_every_source_question_exactly_once(self):
        ids = [entry["id"] for entry in _load_manifest()["questions"]]
        source = _source_ids()
        self.assertEqual(len(source), 30)
        self.assertEqual(sorted(ids), sorted(source))
        self.assertEqual(len(ids), len(set(ids)), "duplicate manifest IDs")

    def test_every_database_exists_in_config(self):
        known = _config_databases()
        for entry in _load_manifest()["questions"]:
            with self.subTest(question=entry["id"]):
                self.assertIn(entry["database"], known)

    def test_every_status_is_allowed(self):
        for entry in _load_manifest()["questions"]:
            with self.subTest(question=entry["id"]):
                self.assertIn(entry["status"], ALLOWED_STATUS)

    def test_statuses_that_need_a_reason_have_one(self):
        for entry in _load_manifest()["questions"]:
            field = NEEDS_REASON.get(entry["status"])
            if field is None:
                continue
            with self.subTest(question=entry["id"]):
                self.assertTrue(entry[field], f"{entry['status']} needs a non-empty {field}")

    def test_clarification_names_the_term_or_formula(self):
        for entry in _load_manifest()["questions"]:
            if entry["status"] != "clarification":
                continue
            with self.subTest(question=entry["id"]):
                self.assertTrue(entry["metric_terms"], "clarification must name a metric term")

    def test_split_is_development_for_all(self):
        # User decision: no held-out split yet. Keep this explicit so a change is deliberate.
        manifest = _load_manifest()
        self.assertEqual(manifest["split"], "development")
        for entry in manifest["questions"]:
            with self.subTest(question=entry["id"]):
                self.assertNotIn("split", entry)


if __name__ == "__main__":
    unittest.main()
