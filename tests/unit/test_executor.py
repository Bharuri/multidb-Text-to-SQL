import tempfile
import unittest
from pathlib import Path

import duckdb

from src.query.executor import ExecuteError, execute


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self._dir.name) / "shop.duckdb"
        connection = duckdb.connect(str(self.db_file))
        connection.execute('CREATE SCHEMA "shop"')
        connection.execute('CREATE TABLE "shop"."shop"."Orders" ("Sales" DOUBLE, "Category" VARCHAR)')
        connection.execute("INSERT INTO \"shop\".\"shop\".\"Orders\" VALUES (1, 'Phones'), (2, 'Chairs'), (3, 'Phones'), (4, 'Phones'), (5, 'Chairs')")
        connection.close()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_row_query_returns_columns_and_rows(self) -> None:
        built = {"sql": 'SELECT "Sales" FROM "shop"."shop"."Orders" WHERE LOWER("Category") = LOWER(?) LIMIT 10', "params": ["phones"]}
        result = execute(built, self.db_file, row_limit=1000, aggregate=False)
        self.assertEqual(result["columns"], ["Sales"])
        self.assertEqual(result["rows"], [[1], [3], [4]])
        self.assertEqual(result["row_count"], 3)
        self.assertFalse(result["truncated"])
        self.assertNotIn("value", result)

    def test_row_cap_truncates_and_reports_it(self) -> None:
        built = {"sql": 'SELECT "Sales" FROM "shop"."shop"."Orders" ORDER BY "Sales"', "params": []}
        result = execute(built, self.db_file, row_limit=2, aggregate=False)
        self.assertEqual(result["rows"], [[1], [2]])
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(result["truncated"])

    def test_row_cap_not_reported_when_exactly_at_limit(self) -> None:
        built = {"sql": 'SELECT "Sales" FROM "shop"."shop"."Orders" ORDER BY "Sales"', "params": []}
        result = execute(built, self.db_file, row_limit=5, aggregate=False)
        self.assertEqual(result["row_count"], 5)
        self.assertFalse(result["truncated"])

    def test_aggregate_returns_single_value(self) -> None:
        built = {"sql": 'SELECT COUNT(*) FROM "shop"."shop"."Orders" WHERE LOWER("Category") = LOWER(?)', "params": ["Phones"]}
        result = execute(built, self.db_file, row_limit=1000, aggregate=True)
        self.assertEqual(result["value"], 3)
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["truncated"])

    def test_write_statement_fails_on_read_only_connection(self) -> None:
        built = {"sql": 'DELETE FROM "shop"."shop"."Orders" WHERE "Sales" > ?', "params": [0]}
        with self.assertRaises(ExecuteError):
            execute(built, self.db_file, row_limit=1000, aggregate=False)
        connection = duckdb.connect(str(self.db_file), read_only=True)
        count = connection.execute('SELECT COUNT(*) FROM "shop"."shop"."Orders"').fetchone()[0]
        connection.close()
        self.assertEqual(count, 5)

    def test_bad_sql_raises_execute_error(self) -> None:
        built = {"sql": 'SELECT "Nope" FROM "shop"."shop"."Orders"', "params": []}
        with self.assertRaises(ExecuteError) as raised:
            execute(built, self.db_file, row_limit=1000, aggregate=False)
        self.assertTrue(raised.exception.message)

    def test_missing_file_raises_execute_error(self) -> None:
        built = {"sql": "SELECT 1", "params": []}
        with self.assertRaises(ExecuteError):
            execute(built, Path(self._dir.name) / "missing.duckdb", row_limit=1000, aggregate=True)


if __name__ == "__main__":
    unittest.main()
