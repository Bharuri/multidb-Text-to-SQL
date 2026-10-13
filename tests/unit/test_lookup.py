import copy
import tempfile
import unittest
from pathlib import Path

import duckdb

from src.query.catalog import Resolved
from src.query.lookup import LOOKUP_LIMIT, lookup_values, make_lookup
from src.query.matcher import MatchError, match_plan

SCHEMA = "shop"


def make_db(path: Path) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
        connection.execute(f'CREATE TABLE "{SCHEMA}"."{SCHEMA}"."Orders" ("Ship_Mode" VARCHAR, "Customer" VARCHAR)')
        connection.execute(
            f'INSERT INTO "{SCHEMA}"."{SCHEMA}"."Orders" VALUES '
            "('Standard Class', 'Ann'), ('Standard Class', 'Bob'), "
            "('Second Class', 'Cy'), ('Air', 'Dee'), ('AIR', 'Eve')"
        )
    finally:
        connection.close()


class LookupValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "shop.duckdb"
        make_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_one_distinct_value_is_returned(self) -> None:
        values = lookup_values(self.db_path, SCHEMA, "Orders", "Ship_Mode", "second class")
        self.assertEqual(values, ["Second Class"])

    def test_result_is_bounded_even_with_many_matches(self) -> None:
        connection = duckdb.connect(str(self.db_path))
        try:
            connection.execute(
                f'INSERT INTO "{SCHEMA}"."{SCHEMA}"."Orders" VALUES ('
                "'air', 'F'), ('Air ', 'G'), ('AIR', 'H')"
            )
        finally:
            connection.close()
        values = lookup_values(self.db_path, SCHEMA, "Orders", "Ship_Mode", "air")
        self.assertEqual(len(values), LOOKUP_LIMIT)

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(lookup_values(self.db_path, SCHEMA, "Orders", "Ship_Mode", "Teleport"), [])

    def test_two_case_variants_are_returned_so_the_caller_can_reject(self) -> None:
        values = lookup_values(self.db_path, SCHEMA, "Orders", "Ship_Mode", "air")
        self.assertEqual(sorted(values), ["AIR", "Air"])

    def test_user_text_is_a_parameter_not_sql(self) -> None:
        hostile = "x' OR '1'='1"
        self.assertEqual(lookup_values(self.db_path, SCHEMA, "Orders", "Ship_Mode", hostile), [])
        connection = duckdb.connect(str(self.db_path), read_only=True)
        try:
            count = connection.execute(f'SELECT COUNT(*) FROM "{SCHEMA}"."{SCHEMA}"."Orders"').fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 5)


class MakeLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "databases").mkdir()
        make_db(self.root / "databases" / "shop.duckdb")
        physical = {
            "schema": SCHEMA,
            "file": "databases/shop.duckdb",
            "tables": [{"name": "Orders", "columns": [{"name": "Ship_Mode"}, {"name": "Customer"}]}],
        }
        semantic = {
            "columns": [
                {"table": "Orders", "column": "Ship_Mode", "data_type": "VARCHAR", "column_class": "other"},
            ],
        }
        self.resolved = Resolved(
            database="demo",
            registry_entry={},
            physical_file="unused",
            semantic_file="unused",
            db_file="databases/shop.duckdb",
            physical=physical,
            semantic=semantic,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lookup_uses_db_file_under_root(self) -> None:
        lookup = make_lookup(self.resolved, root=self.root)
        self.assertEqual(lookup("Orders", "Ship_Mode", "second class"), ["Second Class"])

    def test_matcher_accepts_one_row_and_rejects_two(self) -> None:
        lookup = make_lookup(self.resolved, root=self.root)
        plan = {
            "table": "Orders",
            "select": [],
            "dimensions": [],
            "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}],
            "filters": [{"column": "Ship_Mode", "operator": "=", "value": "second class"}],
            "group_by": [],
            "having": [],
            "order_by": [],
            "calculations": [],
            "limit": None,
        }
        matched = match_plan(plan, self.resolved, lookup=lookup)
        self.assertEqual(matched["filters"][0]["value"], "Second Class")

        ambiguous = copy.deepcopy(plan)
        ambiguous["filters"][0]["value"] = "air"
        with self.assertRaises(MatchError):
            match_plan(ambiguous, self.resolved, lookup=lookup)


if __name__ == "__main__":
    unittest.main()
