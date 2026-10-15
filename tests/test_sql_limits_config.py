import tempfile
import unittest
from pathlib import Path

from src.query.config import CONFIG_PATH, ROOT, ConfigError, load_registry
from src.query.sql_parser import MAX_SQL_OUTPUT_CHARS


class LimitsConfigTests(unittest.TestCase):
    def test_limits_block_loads_from_real_config(self):
        limits = load_registry().limits
        self.assertEqual(limits.max_sql_chars, 20000)
        self.assertEqual(limits.max_ctes, 20)
        self.assertEqual(limits.max_nesting_depth, 12)
        self.assertEqual(limits.max_joins, 12)
        self.assertEqual(limits.max_window_functions, 20)
        self.assertEqual(limits.query_timeout_seconds, 30)

    def test_parser_cap_matches_config(self):
        self.assertEqual(MAX_SQL_OUTPUT_CHARS, load_registry().limits.max_sql_chars)

    def test_missing_key_is_rejected(self):
        self.assertLoaderRejects(lambda text: text.replace("  max_ctes: 20\n", ""))

    def test_non_positive_value_is_rejected(self):
        self.assertLoaderRejects(lambda text: text.replace("max_joins: 12", "max_joins: 0"))

    def assertLoaderRejects(self, mutate):
        text = CONFIG_PATH.read_text(encoding="utf-8")
        mutated = mutate(text)
        self.assertNotEqual(mutated, text, "mutation did not change the config")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(mutated, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_registry(path, ROOT)


if __name__ == "__main__":
    unittest.main()
