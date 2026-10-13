import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.query.business_semantics import BusinessError, load_business, validate_business
from src.query.catalog import Resolved
from src.query.matcher import MatchError, match_plan


def make_resolved(database: str = "demo") -> Resolved:
    physical = {
        "database": database,
        "schema": database,
        "tables": [
            {"name": "Orders", "columns": [{"name": "Sales", "type": "DOUBLE"}, {"name": "Region", "type": "VARCHAR"}]},
        ],
    }
    return Resolved(
        database=database,
        registry_entry={},
        physical_file="specs/physical/demo.yaml",
        semantic_file="specs/semantic/demo.yaml",
        db_file="databases/demo.duckdb",
        physical=physical,
        semantic={"database": database, "schema": database, "columns": []},
    )


def business(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "database": "demo",
        "concepts": [
            {
                "name": "takings",
                "table": "Orders",
                "column": "Sales",
                "synonyms": ["turnover"],
                "description": "Gross sales value",
            }
        ],
    }
    base.update(overrides)
    return base


class BusinessValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved = make_resolved()

    def assertRejected(self, data: Any, *fragments: str) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_business(data, self.resolved)
        reasons = caught.exception.reasons
        for fragment in fragments:
            self.assertTrue(any(fragment in reason for reason in reasons), f"{fragment!r} not in {reasons}")

    def test_valid_file_loads_concepts(self) -> None:
        loaded = validate_business(business(), self.resolved)
        self.assertEqual(loaded.database, "demo")
        self.assertEqual(loaded.concepts[0].column, "Sales")

    def test_synonyms_for_returns_name_and_synonyms_of_the_column_only(self) -> None:
        loaded = validate_business(business(), self.resolved)
        self.assertEqual(loaded.synonyms_for("Orders", "Sales"), ("takings", "turnover"))
        self.assertEqual(loaded.synonyms_for("Orders", "Region"), ())

    def test_prompt_text_lists_each_concept(self) -> None:
        text = validate_business(business(), self.resolved).prompt_text()
        self.assertIn("takings = Orders.Sales", text)
        self.assertIn("also called: turnover", text)
        self.assertIn("Gross sales value", text)

    def test_empty_concepts_give_empty_prompt_text(self) -> None:
        self.assertEqual(validate_business(business(concepts=[]), self.resolved).prompt_text(), "")

    def test_wrong_database_is_rejected(self) -> None:
        self.assertRejected(business(database="other"), "expected demo")

    def test_column_missing_from_physical_mapping_is_rejected(self) -> None:
        data = business(concepts=[{"name": "x", "table": "Orders", "column": "Nope"}])
        self.assertRejected(data, "Orders.Nope", "not in the physical mapping")

    def test_term_pointing_at_two_columns_is_ambiguous(self) -> None:
        data = business(
            concepts=[
                {"name": "value", "table": "Orders", "column": "Sales"},
                {"name": "value", "table": "Orders", "column": "Region"},
            ]
        )
        self.assertRejected(data, "'value' is ambiguous")

    def test_unknown_concept_key_is_rejected(self) -> None:
        data = business(concepts=[{"name": "x", "table": "Orders", "column": "Sales", "aggregate": "SUM"}])
        self.assertRejected(data, "unknown keys: aggregate")

    def test_concepts_must_be_a_list(self) -> None:
        self.assertRejected(business(concepts={"name": "x"}), "concepts must be a list")

    def test_top_level_must_be_a_mapping(self) -> None:
        self.assertRejected(["not", "a", "mapping"], "must be a mapping")


class BusinessFileTests(unittest.TestCase):
    def test_missing_file_gives_no_terms(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            loaded = load_business(make_resolved("nofile"), Path(root))
        self.assertEqual(loaded.concepts, ())
        self.assertEqual(loaded.prompt_text(), "")

    def test_file_is_read_for_the_selected_database_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "specs" / "properties" / "business"
            folder.mkdir(parents=True)
            (folder / "demo.yaml").write_text("database: demo\nconcepts:\n  - name: takings\n    table: Orders\n    column: Sales\n", encoding="utf-8")
            (folder / "other.yaml").write_text("database: other\nconcepts: []\n", encoding="utf-8")
            loaded = load_business(make_resolved("demo"), Path(root))
        self.assertEqual([c.name for c in loaded.concepts], ["takings"])

    def test_unreadable_file_is_a_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "specs" / "properties" / "business"
            folder.mkdir(parents=True)
            (folder / "demo.yaml").write_bytes(b"concepts: [\xff")
            with self.assertRaises(BusinessError):
                load_business(make_resolved("demo"), Path(root))


class BusinessMatcherTests(unittest.TestCase):
    def test_business_term_matches_its_column(self) -> None:
        resolved = make_resolved()
        loaded = validate_business(business(), resolved)
        matched = match_plan({"table": "Orders", "select": ["turnover"], "limit": 5}, resolved, business=loaded)
        self.assertEqual(matched["select"], ["Sales"])

    def test_business_term_is_unknown_without_the_business_file(self) -> None:
        with self.assertRaises(MatchError):
            match_plan({"table": "Orders", "select": ["turnover"], "limit": 5}, make_resolved())


if __name__ == "__main__":
    unittest.main()
