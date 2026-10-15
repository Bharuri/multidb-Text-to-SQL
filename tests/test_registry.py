"""Tests for the typed registry and database selection (B1)."""

import tempfile
import unittest
from pathlib import Path

from src.query import config as cfg

REAL_SUPERSTORE = cfg.ROOT / "specs" / "properties" / "physical_mapping" / "sample_superstore.yaml"
REAL_SUPERSTORE_SEM = cfg.ROOT / "specs" / "properties" / "semantic_mapping" / "sample_superstore.yaml"
SUPERSTORE_FILE = "databases/sample_superstore.duckdb"

BASE = """
input_dir: data
database_dir: databases
databases:
{databases}
retrieval:
  default_row_limit: 1000
  max_prompt_chars: 60000
  value_list_threshold: 30
limits:
  max_sql_chars: 20000
  max_ctes: 20
  max_nesting_depth: 12
  max_joins: 12
  max_window_functions: 20
  query_timeout_seconds: 30
planner_llm:
  provider: bedrock
  model_id: test-model
  region: us-east-1
  temperature: 0
  max_output_tokens: 2048
sql_generator_llm:
  provider: bedrock
  model_id: test-model
  region: us-east-1
  temperature: 0
  max_output_tokens: 2048
capabilities:
  group_by: true
  joins: false
logging:
  file: logs/query.log
"""


def entry(key, physical, semantic, database_file):
    return (
        f"  {key}:\n"
        f"    physical_mapping: {physical.as_posix()}\n"
        f"    semantic_mapping: {semantic.as_posix()}\n"
        f"    database_file: {database_file}\n"
    )


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, databases: str, extra: str = "") -> Path:
        path = self.dir / "config.yaml"
        path.write_text(BASE.format(databases=databases) + extra, encoding="utf-8")
        return path


class LoadRegistryTests(RegistryTestBase):
    def test_real_config_loads_all_four_databases(self):
        registry = cfg.load_registry()
        self.assertEqual(
            set(registry.databases),
            {"sample_superstore", "airbnb", "netflix_titles", "_113thCongress"},
        )
        self.assertEqual(registry.retrieval.default_row_limit, 1000)
        self.assertEqual(registry.planner_llm.provider, "bedrock")
        self.assertTrue(registry.capabilities["joins"])

    def test_paths_resolve_relative_to_repository_root(self):
        path = self.write_config(entry("demo", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        registry = cfg.load_registry(path)
        self.assertEqual(registry.databases["demo"].database_file, cfg.ROOT / SUPERSTORE_FILE)
        self.assertEqual(registry.databases["demo"].database_file_setting, SUPERSTORE_FILE)

    def test_missing_config_file_is_reported(self):
        with self.assertRaisesRegex(cfg.MissingFileError, "config file not found"):
            cfg.load_registry(self.dir / "nope.yaml")

    def test_invalid_yaml_is_reported(self):
        path = self.dir / "config.yaml"
        path.write_text("databases: [unclosed\n", encoding="utf-8")
        with self.assertRaisesRegex(cfg.ConfigError, "invalid YAML"):
            cfg.load_registry(path)

    def test_missing_required_key_is_named(self):
        path = self.write_config(entry("demo", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        text = path.read_text(encoding="utf-8").replace("  default_row_limit: 1000\n", "")
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(cfg.ConfigError, "retrieval: missing required key 'default_row_limit'"):
            cfg.load_registry(path)

    def test_non_positive_limit_is_rejected(self):
        path = self.write_config(entry("demo", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        path.write_text(path.read_text(encoding="utf-8").replace("default_row_limit: 1000", "default_row_limit: 0"), encoding="utf-8")
        with self.assertRaisesRegex(cfg.ConfigError, "default_row_limit: must be a positive integer"):
            cfg.load_registry(path)

    def test_temperature_out_of_range_is_rejected(self):
        path = self.write_config(entry("demo", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        path.write_text(path.read_text(encoding="utf-8").replace("temperature: 0\n  max_output_tokens: 2048\nsql", "temperature: 5\n  max_output_tokens: 2048\nsql", 1), encoding="utf-8")
        with self.assertRaisesRegex(cfg.ConfigError, "planner_llm.temperature"):
            cfg.load_registry(path)

    def test_empty_registry_is_rejected(self):
        path = self.write_config("  {}\n")
        with self.assertRaisesRegex(cfg.ConfigError, "at least one database"):
            cfg.load_registry(path)


class SelectDatabaseTests(RegistryTestBase):
    def test_unknown_database_fails_before_file_checks(self):
        registry = cfg.load_registry()
        with self.assertRaisesRegex(cfg.UnknownDatabaseError, "Unknown database 'nope'"):
            cfg.select_database(registry, "nope")

    def test_missing_duckdb_file_is_reported_with_relative_path(self):
        path = self.write_config(entry("demo", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, "databases/missing_demo.duckdb"))
        registry = cfg.load_registry(path)
        with self.assertRaisesRegex(cfg.MissingFileError, "DuckDB file not found at databases/missing_demo.duckdb"):
            cfg.select_database(registry, "demo")

    def test_missing_metadata_file_is_reported(self):
        path = self.write_config(entry("demo", self.dir / "absent.yaml", REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        registry = cfg.load_registry(path)
        with self.assertRaisesRegex(cfg.MissingFileError, "physical metadata not found"):
            cfg.select_database(registry, "demo")

    def test_metadata_for_another_database_is_rejected(self):
        path = self.write_config(entry("not_superstore", REAL_SUPERSTORE, REAL_SUPERSTORE_SEM, SUPERSTORE_FILE))
        registry = cfg.load_registry(path)
        with self.assertRaisesRegex(cfg.MetadataMismatchError, "database: is 'sample_superstore' but config key is 'not_superstore'"):
            cfg.select_database(registry, "not_superstore")

    def test_every_real_database_selects(self):
        registry = cfg.load_registry()
        for key in registry.databases:
            with self.subTest(database=key):
                self.assertEqual(cfg.select_database(registry, key).key, key)


if __name__ == "__main__":
    unittest.main()
