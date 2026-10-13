import unittest

from src.query.catalog import PROJECT_ROOT, load_yaml

REQUIRED_LLM_KEYS = {"provider", "model_id", "region", "temperature", "max_output_tokens"}
CAPABILITY_FLAGS = {"group_by", "having", "order_by", "date_grains", "calculations", "joins", "simple_sql_builder"}


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_yaml(PROJECT_ROOT / "config.yaml")

    def test_planner_and_sql_generator_have_separate_llm_settings(self):
        for block in ("planner_llm", "sql_generator_llm"):
            self.assertTrue(REQUIRED_LLM_KEYS <= set(self.config[block]), block)
        self.assertNotIn("llm", self.config)

    def test_capability_flags_are_booleans(self):
        capabilities = self.config["capabilities"]
        self.assertEqual(set(capabilities), CAPABILITY_FLAGS)
        for name, value in capabilities.items():
            self.assertIsInstance(value, bool, name)

    def test_joins_are_off_by_default(self):
        self.assertFalse(self.config["capabilities"]["joins"])


if __name__ == "__main__":
    unittest.main()
