"""Catalog metric loading: semantic metrics, business overrides, and cache invalidation."""

import copy
import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from src.query import catalog as cat
from src.query import config as cfg

KEY = "sample_superstore"
REAL_CONFIG = cfg.CONFIG_PATH

PROFIT_MARGIN = {
    "name": "profit_margin",
    "synonyms": ["margin"],
    "status": "approved",
    "kind": "ratio",
    "numerator": {"table": "Orders", "column": "Profit", "aggregate": "sum"},
    "denominator": {"table": "Orders", "column": "Sales", "aggregate": "sum"},
    "unit": "ratio",
    "grain": "whole selection",
    "null_zero_policy": "NULL when SUM(Sales) is 0",
    "description": "Total profit as a share of total sales.",
}

BUSINESS_TOTAL_PROFIT = {
    "name": "total_profit",
    "synonyms": ["profit total"],
    "status": "approved",
    "kind": "aggregate",
    "expression": {"table": "Orders", "column": "Profit", "aggregate": "sum"},
    "unit": "currency",
    "grain": "whole selection",
    "null_zero_policy": "NULL when there are no rows",
    "description": "Sum of profit.",
}


def _minimal_physical() -> dict:
    return {
        "schema": "s",
        "file": "databases/s.duckdb",
        "tables": [
            {
                "name": "Orders",
                "source_sheet": "Orders",
                "row_count": 2,
                "columns": [
                    {"name": "Profit", "type": "DOUBLE", "nullable": False, "has_nulls": False, "is_unique": False},
                    {"name": "Sales", "type": "DOUBLE", "nullable": False, "has_nulls": False, "is_unique": False},
                ],
            }
        ],
        "relationships": [],
    }


def _minimal_semantic(metrics: list) -> dict:
    return {
        "database": "s",
        "database_file": "databases/s.duckdb",
        "columns": [
            {"table": "Orders", "column": "Profit", "data_type": "DOUBLE", "column_class": "numeric", "synonyms": []},
            {"table": "Orders", "column": "Sales", "data_type": "DOUBLE", "column_class": "numeric", "synonyms": []},
        ],
        "metrics": metrics,
    }


class BuildCatalogMetricTests(unittest.TestCase):
    def test_semantic_metrics_are_exposed_with_source(self):
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]))
        self.assertEqual([m.name for m in catalog.metrics], ["profit_margin"])
        self.assertEqual(catalog.metrics[0].source, "semantic")
        self.assertEqual(catalog.metrics[0].raw["numerator"]["column"], "Profit")

    def test_business_metric_is_appended_after_semantic(self):
        business = {"database": "s", "metrics": [BUSINESS_TOTAL_PROFIT]}
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]), (), business)
        self.assertEqual([m.name for m in catalog.metrics], ["profit_margin", "total_profit"])
        self.assertEqual(catalog.metrics[1].source, "business")

    def test_business_metric_with_same_name_replaces_semantic_in_place(self):
        override = dict(PROFIT_MARGIN, description="Business wording.", status="approved")
        business = {"database": "s", "metrics": [override]}
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]), (), business)
        self.assertEqual(len(catalog.metrics), 1)
        self.assertEqual(catalog.metrics[0].source, "business")
        self.assertEqual(catalog.metrics[0].raw["description"], "Business wording.")

    def test_metric_for_matches_name_and_synonym_ignoring_case_and_spaces(self):
        catalog = cat.build_catalog("s", _minimal_physical(), _minimal_semantic([PROFIT_MARGIN]))
        self.assertEqual(catalog.metric_for("profit_margin").name, "profit_margin")
        self.assertEqual(catalog.metric_for("Profit Margin").name, "profit_margin")
        self.assertEqual(catalog.metric_for("  MARGIN ").name, "profit_margin")
        self.assertIsNone(catalog.metric_for("revenue"))

    def test_catalog_without_metrics_key_has_empty_tuple(self):
        semantic = _minimal_semantic([])
        del semantic["metrics"]
        catalog = cat.build_catalog("s", _minimal_physical(), semantic)
        self.assertEqual(catalog.metrics, ())


class RealCatalogMetricTests(unittest.TestCase):
    def setUp(self):
        self.registry = cfg.load_registry(REAL_CONFIG)

    def test_real_databases_have_no_authored_metrics(self):
        for key in self.registry.databases:
            with self.subTest(database=key):
                catalog = cat.load_catalog(self.registry, key)
                self.assertEqual(catalog.metrics, ())


class BusinessFileTests(unittest.TestCase):
    def setUp(self):
        self.registry = cfg.load_registry(REAL_CONFIG)
        self.real = self.registry.databases[KEY]
        self.tmp = Path(tempfile.mkdtemp())
        self.business = self.tmp / "business.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry_with_business(self) -> cfg.Registry:
        entry = dataclasses.replace(self.real, business_mapping=self.business)
        return dataclasses.replace(self.registry, databases={**self.registry.databases, KEY: entry})

    def _write_business(self, data: dict) -> None:
        self.business.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def test_missing_business_file_is_optional(self):
        self.assertFalse(self.business.exists())
        catalog = cat.load_catalog(self._registry_with_business(), KEY)
        self.assertTrue(all(m.source == "semantic" for m in catalog.metrics))

    def test_accepted_business_metric_is_merged(self):
        self._write_business({"database": KEY, "metrics": [copy.deepcopy(BUSINESS_TOTAL_PROFIT)]})
        catalog = cat.load_catalog(self._registry_with_business(), KEY)
        found = catalog.metric_for("total profit")
        self.assertIsNotNone(found)
        self.assertEqual(found.source, "business")

    def test_business_file_for_another_database_is_rejected(self):
        self._write_business({"database": "airbnb", "metrics": []})
        with self.assertRaisesRegex(cfg.MetadataMismatchError, "registry key"):
            cat.load_catalog(self._registry_with_business(), KEY)

    def test_invalid_business_metric_is_rejected(self):
        bad = copy.deepcopy(BUSINESS_TOTAL_PROFIT)
        bad["expression"]["column"] = "NoSuchColumn"
        self._write_business({"database": KEY, "metrics": [bad]})
        with self.assertRaisesRegex(cfg.MetadataMismatchError, "NoSuchColumn"):
            cat.load_catalog(self._registry_with_business(), KEY)

    def test_fingerprint_includes_business_file_only_while_it_exists(self):
        without = cat.fingerprint_for(self._registry_with_business().databases[KEY])
        self._write_business({"database": KEY, "metrics": []})
        with_file = cat.fingerprint_for(self._registry_with_business().databases[KEY])
        self.assertEqual(len(with_file), len(without) + 1)
        self.assertIn(str(self.business), [entry[0] for entry in with_file])

    def test_creating_business_file_invalidates_cache(self):
        registry = self._registry_with_business()
        cache = cat.CatalogCache()
        cache.get(registry, KEY)
        self.assertEqual(cache.load_count(KEY), 1)
        cache.get(registry, KEY)
        self.assertEqual(cache.load_count(KEY), 1)

        self._write_business({"database": KEY, "metrics": [copy.deepcopy(BUSINESS_TOTAL_PROFIT)]})
        catalog = cache.get(registry, KEY)
        self.assertEqual(cache.load_count(KEY), 2)
        self.assertIsNotNone(catalog.metric_for("total_profit"))


if __name__ == "__main__":
    unittest.main()
