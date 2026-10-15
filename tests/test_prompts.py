import unittest

from src.query.catalog import Catalog, Metric, TableInfo
from src.query.physical_md import SchemaMap
from src.query.prompts import (
    PLANNER_RULES,
    SQL_GENERATOR_RULES,
    build_planner_prompt,
    build_repair_prompt,
    build_sql_prompt,
    build_sql_repair_prompt,
)
from src.query.retrieval import ColumnHit, RetrievedContext

FULL_SCHEMA: SchemaMap = {"Orders": {"Sales": "DOUBLE", "Customer_ID": "VARCHAR"}, "People": {"Region": "VARCHAR"}}


def _context():
    return RetrievedContext(
        database="sample_superstore",
        tables=["Orders"],
        columns=[ColumnHit("Orders", "Sales", "DOUBLE", "numeric", "name match", ())],
    )


def _catalog():
    return Catalog(
        key="sample_superstore",
        schema="sample_superstore",
        database_file="unused.duckdb",
        tables={
            "Orders": TableInfo(
                name="Orders",
                source_sheet="Orders",
                row_count=1,
                columns={},
            )
        },
        semantic_columns=(),
        relationships=(),
        numeric_operators=(),
        fingerprint=(),
        metrics=(),
    )


class PlannerPromptTests(unittest.TestCase):
    def test_prompt_contains_question_and_context(self):
        prompt = build_planner_prompt("What are total sales?", _context(), full_schema=FULL_SCHEMA)
        self.assertIn("What are total sales?", prompt)
        self.assertIn("Database: sample_superstore", prompt)

    def test_prompt_includes_full_schema_beyond_retrieved_context(self):
        prompt = build_planner_prompt("q", _context(), full_schema=FULL_SCHEMA)
        self.assertIn("- Orders: Sales (DOUBLE), Customer_ID (VARCHAR)", prompt)
        self.assertIn("- People: Region (VARCHAR)", prompt)

    def test_prompt_asks_for_the_analysis_envelope(self):
        prompt = build_planner_prompt("q", _context(), full_schema=FULL_SCHEMA)
        self.assertIn("Reply with exactly one JSON object", prompt)
        for key in ('"status"', '"analysis"', '"clarification"'):
            self.assertIn(key, prompt)
        for status in ("ready", "needs_clarification", "cannot_answer"):
            self.assertIn(f'"{status}"', prompt)

    def test_prompt_does_not_reference_the_removed_plan_grammar(self):
        prompt = build_planner_prompt("q", _context(), full_schema=FULL_SCHEMA)
        self.assertNotIn("plan.py", prompt)
        self.assertNotIn("stage", prompt.lower())

    def test_approved_metrics_are_listed_with_synonyms(self):
        metric = Metric(
            name="PROFIT_MARGIN",
            status="approved",
            kind="ratio",
            source="business",
            synonyms=("margin",),
        )
        prompt = build_planner_prompt("q", _context(), [metric], full_schema=FULL_SCHEMA)
        self.assertIn("- PROFIT_MARGIN (kind=ratio, source=business) synonyms=['margin']", prompt)

    def test_no_metrics_is_stated_explicitly(self):
        prompt = build_planner_prompt("q", _context(), [], full_schema=FULL_SCHEMA)
        self.assertIn("(none approved for this database)", prompt)

    def test_full_schema_is_required(self):
        with self.assertRaises(TypeError):
            build_planner_prompt("q", _context())

    def test_planner_rules_forbid_inventing_names(self):
        self.assertIn("Never invent names", PLANNER_RULES)


class SqlPromptTests(unittest.TestCase):
    def test_prompt_contains_analysis_question_and_metadata(self):
        prompt = build_sql_prompt(
            "Total sales?",
            "Sum Sales from Orders.",
            _catalog(),
            _context(),
            {"subqueries": True, "set_operations": False},
        )
        self.assertIn("Sum Sales from Orders.", prompt)
        self.assertIn("Total sales?", prompt)
        self.assertIn("Table Orders:", prompt)
        self.assertIn("Dialect: DuckDB SQL", prompt)
        self.assertIn("julianday", prompt)
        self.assertIn("reserved word", prompt)

    def test_enabled_features_list_only_enabled_flags(self):
        prompt = build_sql_prompt(
            "q", "a", _catalog(), _context(), {"subqueries": True, "set_operations": False}
        )
        features_line = prompt.split("Enabled features:", 1)[1].split("\n", 1)[0]
        self.assertIn("subqueries", features_line)
        self.assertNotIn("set_operations", features_line)

    def test_generator_rules_allow_inline_strings_and_numbers(self):
        self.assertIn("may be written inline", SQL_GENERATOR_RULES)
        self.assertNotIn("Never write a string literal inline", SQL_GENERATOR_RULES)
        self.assertIn("Numbers may be written inline", SQL_GENERATOR_RULES)

    def test_output_contract_is_sql_params_assumptions(self):
        prompt = build_sql_prompt("q", "a", _catalog(), _context(), {})
        self.assertIn('"sql"', prompt)
        self.assertIn('"params"', prompt)
        self.assertIn('"assumptions"', prompt)

    def test_prompt_does_not_reference_the_removed_plan_grammar(self):
        prompt = build_sql_prompt("q", "a", _catalog(), _context(), {})
        self.assertNotIn("plan.py", prompt)
        self.assertNotIn("stage", prompt.lower())


class RepairPromptTests(unittest.TestCase):
    def test_planner_repair_prompt_carries_previous_reply_and_reason(self):
        text = build_repair_prompt("BASE", '{"status": "ready"', "not valid JSON")
        self.assertIn("BASE", text)
        self.assertIn('{"status": "ready"', text)
        self.assertIn("not valid JSON", text)
        self.assertIn("exactly one JSON object", text)

    def test_sql_repair_prompt_carries_previous_reply_and_reason(self):
        text = build_sql_repair_prompt("BASE", '{"sql": "SELECT 1"}', "inline value is not allowed")
        self.assertIn("BASE", text)
        self.assertIn("inline value is not allowed", text)
        self.assertIn('"sql", "params", and "assumptions"', text)


if __name__ == "__main__":
    unittest.main()
