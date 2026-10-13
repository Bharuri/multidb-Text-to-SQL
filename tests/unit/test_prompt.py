import unittest

from src.query.catalog import Resolved, resolve
from src.query.prompt import PromptError, build_prompt


class PromptForRealDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolved = resolve("sample_superstore")

    def build(self, question: str = "how many phones", max_chars: int = 60000, threshold: int = 30) -> str:
        return build_prompt(question, self.resolved, max_chars, threshold)

    def test_prompt_lists_every_physical_column(self) -> None:
        text = self.build()
        for table in self.resolved.physical["tables"]:
            self.assertIn(f"Table {table['name']}", text)
            for column in table["columns"]:
                self.assertIn(column["name"], text)

    def test_prompt_contains_the_question_and_rules(self) -> None:
        text = self.build("how many phones")
        self.assertIn("Question: how many phones", text)
        self.assertIn("Never write SQL", text)
        self.assertIn("cannot_answer", text)

    def test_date_columns_get_their_date_operators(self) -> None:
        text = self.build()
        self.assertIn("date operators: year, month, year_month, date_between", text)

    def test_numeric_columns_get_the_numeric_operators(self) -> None:
        text = self.build()
        self.assertIn("numeric operators: >=, >", text)

    def test_stored_values_of_a_boolean_column_are_listed(self) -> None:
        text = self.build()
        returned_line = next(line for line in text.splitlines() if "- Returned (" in line)
        self.assertIn("stored values: Yes", returned_line)

    def test_stored_values_of_a_categorical_column_are_listed(self) -> None:
        text = self.build()
        sub_category_line = next(line for line in text.splitlines() if "- Sub_Category (" in line)
        self.assertIn("stored values:", sub_category_line)
        self.assertIn("Phones", sub_category_line)

    def test_oversized_prompt_raises_with_size_and_limit(self) -> None:
        with self.assertRaises(PromptError) as raised:
            self.build(max_chars=100)
        self.assertEqual(raised.exception.limit, 100)
        self.assertGreater(raised.exception.chars, 100)
        self.assertIn("over the limit of 100", raised.exception.reason)

    def test_prompt_at_exact_limit_is_accepted(self) -> None:
        text = self.build()
        self.assertEqual(len(self.build(max_chars=len(text))), len(text))


class PromptThresholdTests(unittest.TestCase):
    def fake_resolved(self, values: list[str]) -> Resolved:
        physical = {
            "tables": [{"name": "Items", "columns": [{"name": "Color", "type": "VARCHAR"}]}],
        }
        semantic = {
            "columns": [
                {"table": "Items", "column": "Color", "column_class": "categorical", "values": values},
            ],
            "numeric_operators": [],
        }
        return Resolved(
            database="shop",
            registry_entry={},
            physical_file="",
            semantic_file="",
            db_file="",
            physical=physical,
            semantic=semantic,
        )

    def test_values_at_the_threshold_are_listed(self) -> None:
        values = [f"v{i}" for i in range(30)]
        text = build_prompt("q", self.fake_resolved(values), 60000, 30)
        self.assertIn("stored values: v0, v1", text)

    def test_values_over_the_threshold_are_not_listed(self) -> None:
        values = [f"v{i}" for i in range(31)]
        text = build_prompt("q", self.fake_resolved(values), 60000, 30)
        self.assertNotIn("stored values:", text)
        self.assertNotIn("v0", text)


if __name__ == "__main__":
    unittest.main()
