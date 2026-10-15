"""Tests for strict metadata validation (A5)."""

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from src.mapping import metadata_validator as mv

FILE = "physical.yaml"
SEM_FILE = "semantic.yaml"

VALID_PHYSICAL = {
    "schema": "demo",
    "database": "demo",
    "file": "databases/demo.duckdb",
    "workbook": "data/demo.xlsx",
    "tables": [
        {
            "name": "Orders",
            "source_sheet": "Orders",
            "row_count": 3,
            "columns": [
                {"name": "Order_ID", "type": "VARCHAR", "nullable": True, "has_nulls": False, "is_unique": False},
                {"name": "Region", "type": "VARCHAR", "nullable": True, "has_nulls": False, "is_unique": False},
            ],
        },
        {
            "name": "People",
            "source_sheet": "People",
            "row_count": 2,
            "columns": [
                {"name": "Region", "type": "VARCHAR", "nullable": True, "has_nulls": False, "is_unique": True},
            ],
        },
    ],
    "relationships": [
        {
            "name": "fk_orders_region",
            "status": "approved",
            "meaning": "Each order belongs to one region.",
            "cardinality": "many_to_one",
            "enforceable_now": True,
            "enabled": True,
            "join_policy": "inner",
            "normalization": {"child": [], "parent": []},
            "fan_out": "none",
            "child": {"table": "Orders", "column": "Region"},
            "parent": {"table": "People", "column": "Region"},
            "issues": [],
            "checks": [{"column": "Region", "child_type": "VARCHAR", "parent_type": "VARCHAR", "orphan_rows": 0}],
        }
    ],
}

VALID_SEMANTIC = {
    "database": "demo",
    "schema": "demo",
    "database_file": "databases/demo.duckdb",
    "schema_mapping": "specs/properties/physical_mapping/demo.yaml",
    "numeric_operators": [{"operator": ">=", "phrases": ["at least {n}"], "template": "{column} >= {value}"}],
    "metrics": [],
    "columns": [
        {
            "database": "demo",
            "schema": "demo",
            "table": "Orders",
            "column": "Region",
            "data_type": "VARCHAR",
            "column_class": "categorical",
            "values": ["East", "West"],
            "synonyms": [],
            "flags": [],
        }
    ],
}


def physical_errors(data):
    errors, _ = mv.validate_physical(copy.deepcopy(data), FILE)
    return errors


def semantic_errors(semantic, physical=VALID_PHYSICAL):
    _, column_types = mv.validate_physical(copy.deepcopy(physical), FILE)
    return mv.validate_semantic(copy.deepcopy(semantic), SEM_FILE, column_types, "demo", "databases/demo.duckdb")


def contains(errors, *fragments):
    return any(all(fragment in error for fragment in fragments) for error in errors)


class PhysicalValidationTests(unittest.TestCase):
    def test_valid_fixture_has_no_errors(self):
        self.assertEqual(physical_errors(VALID_PHYSICAL), [])

    def test_unknown_top_level_key_is_rejected(self):
        data = dict(VALID_PHYSICAL, surprise=1)
        self.assertTrue(contains(physical_errors(data), "unknown key 'surprise'"))

    def test_missing_column_key_is_rejected(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        del data["tables"][0]["columns"][0]["is_unique"]
        self.assertTrue(contains(physical_errors(data), "missing required key 'is_unique'"))

    def test_unsupported_type_is_rejected(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["tables"][0]["columns"][0]["type"] = "TEXT"
        self.assertTrue(contains(physical_errors(data), "unsupported type 'TEXT'"))

    def test_bad_cardinality_is_rejected_with_allowed_values(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["cardinality"] = "many-to-one"
        errors = physical_errors(data)
        self.assertTrue(contains(errors, "cardinality", "many-to-one", "unknown"))

    def test_relationship_to_missing_column_is_rejected(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["parent"]["column"] = "Area"
        self.assertTrue(contains(physical_errors(data), "column 'Area' is not in table 'People'"))

    def test_enabled_relationship_must_be_approved(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["status"] = "proposed"
        self.assertTrue(contains(physical_errors(data), "enabled but status is 'proposed'"))

    def test_join_policy_must_be_inner_or_left(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["join_policy"] = "outer"
        self.assertTrue(contains(physical_errors(data), "join_policy", "'outer'"))

    def test_recorded_check_type_must_match_physical_type(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["checks"][0]["parent_type"] = "BIGINT"
        self.assertTrue(contains(physical_errors(data), "parent_type", "Re-profile"))

    def test_nulls_with_nullable_false_is_rejected(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        column = data["tables"][0]["columns"][0]
        column.update(nullable=False, has_nulls=True)
        self.assertTrue(contains(physical_errors(data), "has NULLs but nullable is false"))

    def test_normalization_must_be_child_and_parent_lists(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["normalization"] = "none"
        self.assertTrue(contains(physical_errors(data), "normalization"))

    def test_normalization_op_must_be_allowed(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["normalization"] = {"child": ["upper"], "parent": []}
        self.assertTrue(contains(physical_errors(data), "upper"))

    def test_normalization_cast_type_must_be_allowed(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["normalization"] = {"child": [], "parent": ["cast TEXT"]}
        self.assertTrue(contains(physical_errors(data), "cast TEXT"))

    def test_normalization_lower_trim_cast_are_accepted(self):
        data = copy.deepcopy(VALID_PHYSICAL)
        data["relationships"][0]["normalization"] = {"child": ["lower", "trim"], "parent": ["cast BIGINT"]}
        self.assertEqual(physical_errors(data), [])


class SemanticValidationTests(unittest.TestCase):
    def test_valid_fixture_has_no_errors(self):
        self.assertEqual(semantic_errors(VALID_SEMANTIC), [])

    def test_column_missing_from_physical_mapping_is_rejected(self):
        data = copy.deepcopy(VALID_SEMANTIC)
        data["columns"][0]["column"] = "Missing"
        self.assertTrue(contains(semantic_errors(data), "column 'Missing' does not exist in table 'Orders'"))

    def test_data_type_must_match_physical_type(self):
        data = copy.deepcopy(VALID_SEMANTIC)
        data["columns"][0]["data_type"] = "BIGINT"
        self.assertTrue(contains(semantic_errors(data), "physical type is 'VARCHAR'"))

    def test_wrong_database_file_is_rejected(self):
        data = dict(copy.deepcopy(VALID_SEMANTIC), database_file="databases/other.duckdb")
        self.assertTrue(contains(semantic_errors(data), "database_file", "config expects"))

    def test_operator_template_needs_column_placeholder(self):
        data = copy.deepcopy(VALID_SEMANTIC)
        data["numeric_operators"][0]["template"] = "value >= {value}"
        self.assertTrue(contains(semantic_errors(data), "placeholder '{column}'"))


class FileLoadingTests(unittest.TestCase):
    def test_invalid_yaml_file_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("tables: [unclosed\n", encoding="utf-8")
            errors = []
            self.assertIsNone(mv._load_yaml(bad, errors))
            self.assertTrue(contains(errors, "invalid YAML"))

    def test_missing_file_reports_actionable_error(self):
        errors = []
        self.assertIsNone(mv._load_yaml(Path("does/not/exist.yaml"), errors))
        self.assertTrue(contains(errors, "file not found"))

    def test_configured_metadata_is_valid(self):
        self.assertEqual(mv.validate_all(), [])

    def test_round_trip_through_yaml_keeps_validity(self):
        text = yaml.safe_dump(VALID_PHYSICAL, sort_keys=False)
        self.assertEqual(physical_errors(yaml.safe_load(text)), [])


if __name__ == "__main__":
    unittest.main()
