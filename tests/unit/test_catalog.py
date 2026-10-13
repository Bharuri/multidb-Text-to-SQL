import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from src.query import catalog
from src.query.catalog import CatalogError, resolve


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def build_project(root: Path, physical_file: str = "databases/demo.duckdb", semantic_file: str = "databases/demo.duckdb") -> None:
    (root / "databases").mkdir(parents=True, exist_ok=True)
    (root / "databases" / "demo.duckdb").write_bytes(b"")
    write_yaml(
        root / "config.yaml",
        {
            "databases": {
                "demo": {
                    "physical_mapping": "specs/physical/demo.yaml",
                    "semantic_mapping": "specs/semantic/demo.yaml",
                    "database_file": "databases/demo.duckdb",
                }
            }
        },
    )
    write_yaml(
        root / "specs" / "physical" / "demo.yaml",
        {
            "database": "demo",
            "schema": "demo",
            "file": physical_file,
            "tables": [{"name": "Orders", "columns": [{"name": "a"}, {"name": "b"}]}],
        },
    )
    write_yaml(root / "specs" / "semantic" / "demo.yaml", {"database": "demo", "schema": "demo", "database_file": semantic_file})


class ResolveRealRegistryTests(unittest.TestCase):
    def test_every_registry_database_resolves(self):
        config = yaml.safe_load((catalog.PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
        for name in config["databases"]:
            with self.subTest(database=name):
                resolved = resolve(name)
                self.assertGreater(resolved.table_count, 0)
                self.assertGreater(resolved.column_count, 0)

    def test_sample_superstore_counts(self):
        resolved = resolve("sample_superstore")
        self.assertEqual(resolved.table_count, 3)
        self.assertEqual(resolved.column_count, 25)
        self.assertEqual(resolved.db_file, "databases/sample_superstore.duckdb")

    def test_unknown_database_is_an_error(self):
        with self.assertRaisesRegex(CatalogError, "Unknown database: no_such_db"):
            resolve("no_such_db")


class ResolveFakeProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_project_resolves(self):
        build_project(self.root)
        resolved = resolve("demo", root=self.root)
        self.assertEqual(resolved.table_count, 1)
        self.assertEqual(resolved.column_count, 2)
        self.assertEqual(resolved.physical_file, "specs/physical/demo.yaml")

    def test_missing_database_file_is_an_error(self):
        build_project(self.root)
        (self.root / "databases" / "demo.duckdb").unlink()
        with self.assertRaisesRegex(CatalogError, "databases/demo.duckdb"):
            resolve("demo", root=self.root)

    def test_missing_yaml_file_is_an_error(self):
        build_project(self.root)
        (self.root / "specs" / "semantic" / "demo.yaml").unlink()
        with self.assertRaisesRegex(CatalogError, "specs/semantic/demo.yaml"):
            resolve("demo", root=self.root)

    def test_semantic_path_mismatch_is_an_error(self):
        build_project(self.root, semantic_file="databases/other.duckdb")
        with self.assertRaisesRegex(CatalogError, "semantic database_file"):
            resolve("demo", root=self.root)

    def test_physical_path_mismatch_is_an_error(self):
        build_project(self.root, physical_file="databases/other.duckdb")
        with self.assertRaisesRegex(CatalogError, "physical file"):
            resolve("demo", root=self.root)

    def test_wrong_database_key_is_an_error(self):
        build_project(self.root)
        write_yaml(self.root / "specs" / "semantic" / "demo.yaml", {"database": "other", "schema": "demo", "database_file": "databases/demo.duckdb"})
        with self.assertRaisesRegex(CatalogError, "Schema key mismatch for demo: semantic mapping database is other"):
            resolve("demo", root=self.root)

    def test_schema_key_mismatch_is_an_error(self):
        build_project(self.root)
        physical = self.root / "specs" / "physical" / "demo.yaml"
        data = yaml.safe_load(physical.read_text(encoding="utf-8"))
        write_yaml(physical, data)
        write_yaml(self.root / "specs" / "semantic" / "demo.yaml", {"database": "demo", "schema": "other", "database_file": "databases/demo.duckdb"})
        with self.assertRaisesRegex(CatalogError, "semantic schema is other"):
            resolve("demo", root=self.root)

    def test_malformed_yaml_is_a_catalog_error(self):
        build_project(self.root)
        (self.root / "specs" / "semantic" / "demo.yaml").write_text("database: [unclosed\n", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "Cannot read demo.yaml"):
            resolve("demo", root=self.root)

    def test_top_level_list_is_a_catalog_error(self):
        build_project(self.root)
        write_yaml(self.root / "specs" / "physical" / "demo.yaml", ["not", "a", "mapping"])
        with self.assertRaisesRegex(CatalogError, "top level must be a mapping"):
            resolve("demo", root=self.root)

    def test_duplicate_table_is_an_error(self):
        build_project(self.root)
        physical = self.root / "specs" / "physical" / "demo.yaml"
        data = yaml.safe_load(physical.read_text(encoding="utf-8"))
        data["tables"].append({"name": "Orders", "columns": [{"name": "c"}]})
        write_yaml(physical, data)
        write_yaml(self.root / "specs" / "semantic" / "demo.yaml", {"database": "demo", "schema": "demo", "database_file": "databases/demo.duckdb"})
        with self.assertRaisesRegex(CatalogError, "duplicate table Orders"):
            resolve("demo", root=self.root)

    def test_column_without_name_is_an_error(self):
        build_project(self.root)
        physical = self.root / "specs" / "physical" / "demo.yaml"
        data = yaml.safe_load(physical.read_text(encoding="utf-8"))
        data["tables"][0]["columns"].append({"type": "BIGINT"})
        write_yaml(physical, data)
        write_yaml(self.root / "specs" / "semantic" / "demo.yaml", {"database": "demo", "schema": "demo", "database_file": "databases/demo.duckdb"})
        with self.assertRaisesRegex(CatalogError, "every column in Orders needs a text name"):
            resolve("demo", root=self.root)
    def test_changed_file_reloads(self):
        build_project(self.root)
        self.assertEqual(resolve("demo", root=self.root).table_count, 1)

        physical = self.root / "specs" / "physical" / "demo.yaml"
        write_yaml(
            physical,
            {
                "database": "demo",
                "schema": "demo",
                "file": "databases/demo.duckdb",
                "tables": [
                    {"name": "Orders", "columns": [{"name": "a"}]},
                    {"name": "Returns", "columns": [{"name": "b"}]},
                ],
            },
        )
        later = physical.stat().st_mtime_ns + 10_000_000_000
        os.utime(physical, ns=(later, later))

        resolved = resolve("demo", root=self.root)
        self.assertEqual(resolved.table_count, 2)
        self.assertEqual(resolved.column_count, 2)

    def test_unchanged_file_is_not_parsed_again(self):
        build_project(self.root)
        resolve("demo", root=self.root)
        with mock.patch.object(catalog.yaml, "safe_load", wraps=catalog.yaml.safe_load) as parser:
            resolve("demo", root=self.root)
        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
