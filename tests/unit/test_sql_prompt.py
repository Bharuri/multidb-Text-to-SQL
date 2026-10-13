import unittest

from src.query.catalog import Resolved
from src.query.prompt import PromptError
from src.query.sql_prompt import build_sql_prompt


def make_resolved() -> Resolved:
    physical = {
        "schema": "main",
        "tables": [
            {
                "name": "Orders",
                "columns": [{"name": "Sales", "type": "DOUBLE"}, {"name": "Sub_Category", "type": "VARCHAR"}],
            }
        ]
    }
    return Resolved(
        database="demo",
        registry_entry={},
        physical_file="specs/physical/demo.yaml",
        semantic_file="specs/semantic/demo.yaml",
        db_file="databases/demo.duckdb",
        physical=physical,
        semantic={"columns": []},
    )


MATCHED = {
    "table": "Orders",
    "select": [],
    "dimensions": [],
    "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}],
    "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones"}],
    "group_by": [],
    "having": [],
    "order_by": [],
    "calculations": [],
    "limit": None,
    "matches": [{"kind": "diagnostic"}],
}


class SqlPromptTests(unittest.TestCase):
    def build(self, **overrides) -> str:
        args = {
            "question": "How many phones?",
            "matched": MATCHED,
            "resolved": make_resolved(),
            "max_chars": 60000,
            "default_row_limit": 1000,
        }
        args.update(overrides)
        return build_sql_prompt(**args)

    def test_asks_for_the_strict_sql_and_params_reply(self) -> None:
        text = self.build()
        self.assertIn('"sql" and "params"', text)
        self.assertIn("one SELECT statement", text)

    def test_includes_question_schema_and_plan(self) -> None:
        text = self.build()
        self.assertIn("Question: How many phones?", text)
        self.assertIn("Table Orders", text)
        self.assertIn("Sales DOUBLE", text)
        self.assertIn('"function": "COUNT"', text)

    def test_share_rendering_is_stated(self) -> None:
        text = self.build()
        self.assertIn("COUNT(*) FILTER (WHERE", text)
        self.assertIn("NULLIF(COUNT(*), ?)", text)

    def test_text_equality_and_from_rules_are_stated(self) -> None:
        text = self.build()
        self.assertIn('LOWER("column") = LOWER(?)', text)
        self.assertIn("(FROM: ", text)

    def test_plan_diagnostics_are_not_sent(self) -> None:
        self.assertNotIn("diagnostic", self.build())

    def test_default_row_limit_is_filled_in(self) -> None:
        text = self.build(default_row_limit=250)
        self.assertIn("LIMIT 250", text)
        self.assertNotIn("{default_row_limit}", text)

    def test_business_definitions_are_included_when_given(self) -> None:
        self.assertIn("Business definitions:\nrevenue = Orders.Sales", self.build(business_definitions="revenue = Orders.Sales"))

    def test_no_business_section_without_definitions(self) -> None:
        self.assertNotIn("Business definitions:", self.build())

    def test_oversize_prompt_is_refused(self) -> None:
        with self.assertRaises(PromptError) as caught:
            self.build(max_chars=100)
        self.assertEqual(caught.exception.limit, 100)
        self.assertGreater(caught.exception.chars, 100)


if __name__ == "__main__":
    unittest.main()
