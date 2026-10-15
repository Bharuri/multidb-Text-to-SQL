import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from src.query import catalog as cat
from src.query import config as cfg

REAL_CONFIG = cfg.CONFIG_PATH
KEY = "sample_superstore"


class BuildCatalogTests(unittest.TestCase):
    def setUp(self):
        self.registry = cfg.load_registry(REAL_CONFIG)
        self.entry = self.registry.databases[KEY]

    def test_real_catalog_loads_tables_columns_and_relationships(self):
        catalog = cat.load_catalog(self.registry, KEY)
        self.assertEqual(catalog.key, KEY)
        self.assertEqual(catalog.schema, KEY)
        self.assertIn("Orders", catalog.tables)
        self.assertEqual(catalog.tables["Orders"].columns["Row_ID"].type, "BIGINT")
        self.assertTrue(catalog.tables["Orders"].columns["Row_ID"].is_unique)
        self.assertTrue(catalog.enabled_relationships)
        rel = catalog.enabled_relationships[0]
        self.assertIn(rel.join_policy, {"inner", "left"})

    def test_semantic_lookup_returns_column_metadata(self):
        catalog = cat.load_catalog(self.registry, KEY)
        found = catalog.semantic_for("Orders", "Category")
        self.assertIsNotNone(found)
        self.assertEqual(found.column_class, "categorical")
        self.assertIn("Furniture", found.values)
        self.assertIsNone(catalog.semantic_for("Orders", "NoSuchColumn"))

    def test_numeric_operators_are_exposed(self):
        catalog = cat.load_catalog(self.registry, KEY)
        self.assertTrue(any(op["operator"] == ">=" for op in catalog.numeric_operators))

    def test_build_catalog_is_pure_and_disables_unenabled_relationships(self):
        physical = {
            "schema": "s",
            "file": "databases/s.duckdb",
            "tables": [
                {
                    "name": "T",
                    "source_sheet": "T",
                    "row_count": 2,
                    "columns": [
                        {"name": "id", "type": "BIGINT", "nullable": False, "has_nulls": False, "is_unique": True}
                    ],
                }
            ],
            "relationships": [
                {
                    "name": "r",
                    "status": "proposed",
                    "meaning": "m",
                    "cardinality": "many_to_one",
                    "enabled": False,
                    "join_policy": "inner",
                    "normalization": {"child": [], "parent": []},
                    "fan_out": "none",
                    "child": {"table": "T", "column": "id"},
                    "parent": {"table": "T", "column": "id"},
                }
            ],
        }
        semantic = {"columns": [], "numeric_operators": []}
        built = cat.build_catalog("s", physical, semantic)
        self.assertEqual(built.tables["T"].row_count, 2)
        self.assertEqual(built.enabled_relationships, ())
        self.assertEqual(built.relationships[0].child, cat.Endpoint("T", "id"))


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.registry = cfg.load_registry(REAL_CONFIG)
        self.calls = []

        def counting_loader(registry, key):
            self.calls.append(key)
            return cat.load_catalog(registry, key)

        self.cache = cat.CatalogCache(loader=counting_loader)

    def test_repeated_requests_reuse_cached_catalog(self):
        first = self.cache.get(self.registry, KEY)
        second = self.cache.get(self.registry, KEY)
        third = self.cache.get(self.registry, KEY)
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(self.calls, [KEY])
        self.assertEqual(self.cache.load_count(KEY), 1)

    def test_changed_metadata_file_invalidates_only_that_database(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            physical = tmp / "physical.yaml"
            semantic = tmp / "semantic.yaml"
            real = self.registry.databases[KEY]
            shutil.copy(real.physical_mapping, physical)
            # Point the copied semantic file at the copied physical file so identity checks pass.
            text = real.semantic_mapping.read_text(encoding="utf-8")
            rel_physical = real.physical_mapping.relative_to(self.registry.root).as_posix()
            semantic.write_text(text.replace(rel_physical, physical.as_posix()), encoding="utf-8")
            entry = dataclasses.replace(real, physical_mapping=physical, semantic_mapping=semantic)
            registry = dataclasses.replace(
                self.registry, databases={**self.registry.databases, KEY: entry}
            )

            self.cache.get(registry, KEY)
            self.cache.get(registry, KEY)
            self.assertEqual(self.cache.load_count(KEY), 1)

            # Append a comment: changes size, so the fingerprint differs even on coarse clocks.
            with physical.open("a", encoding="utf-8") as handle:
                handle.write("\n# touched\n")
            self.cache.get(registry, KEY)
            self.assertEqual(self.cache.load_count(KEY), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_database_raises_and_is_not_cached(self):
        with self.assertRaises(cfg.UnknownDatabaseError):
            self.cache.get(self.registry, "does_not_exist")
        self.assertEqual(self.cache.load_count("does_not_exist"), 0)
        self.assertNotIn("does_not_exist", self.cache._entries)

    def test_clear_forces_reload(self):
        self.cache.get(self.registry, KEY)
        self.cache.clear()
        self.cache.get(self.registry, KEY)
        self.assertEqual(self.cache.load_count(KEY), 1)
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
