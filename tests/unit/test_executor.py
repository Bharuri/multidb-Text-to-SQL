import tempfile
import unittest
from pathlib import Path

import duckdb

from src.query.executor import ExecuteError, check_manual_sql, execute, run_manual_sql


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

    def test_grouped_query_honours_row_cap(self) -> None:
        built = {"sql": 'SELECT "Category", COUNT(*) AS n FROM "shop"."shop"."Orders" GROUP BY "Category" ORDER BY "Category" LIMIT 2', "params": []}
        result = execute(built, self.db_file, row_limit=1, aggregate=False)
        self.assertEqual(result["row_count"], 1)
        self.assertTrue(result["truncated"])

    def test_missing_file_message_hides_the_path(self) -> None:
        missing = Path(self._dir.name) / "missing.duckdb"
        with self.assertRaises(ExecuteError) as raised:
            execute({"sql": "SELECT 1", "params": []}, missing, row_limit=1000, aggregate=True)
        self.assertEqual(raised.exception.message, "The database could not be opened.")
        self.assertNotIn(str(missing), raised.exception.message)

    def test_query_error_message_has_no_traceback(self) -> None:
        built = {"sql": 'SELECT "Nope" FROM "shop"."shop"."Orders"', "params": []}
        with self.assertRaises(ExecuteError) as raised:
            execute(built, self.db_file, row_limit=1000, aggregate=False)
        self.assertNotIn("Traceback", raised.exception.message)
    def test_missing_file_raises_execute_error(self) -> None:
        built = {"sql": "SELECT 1", "params": []}
        with self.assertRaises(ExecuteError):
            execute(built, Path(self._dir.name) / "missing.duckdb", row_limit=1000, aggregate=True)


class ManualSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self._dir.name) / "shop.duckdb"
        connection = duckdb.connect(str(self.db_file))
        connection.execute('CREATE SCHEMA "shop"')
        connection.execute('CREATE TABLE "shop"."shop"."Orders" ("Sales" DOUBLE, "Category" VARCHAR)')
        connection.execute("INSERT INTO \"shop\".\"shop\".\"Orders\" VALUES (1, 'Phones'), (2, 'Chairs'), (3, 'Phones')")
        connection.close()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_select_runs_and_returns_rows(self) -> None:
        result = run_manual_sql('SELECT "Category", SUM("Sales") AS total FROM "shop"."shop"."Orders" GROUP BY 1 ORDER BY 1', self.db_file, row_limit=1000)
        self.assertEqual(result["columns"], ["Category", "total"])
        self.assertEqual(result["rows"], [["Chairs", 2.0], ["Phones", 4.0]])

    def test_with_clause_is_allowed(self) -> None:
        result = run_manual_sql('WITH t AS (SELECT "Sales" FROM "shop"."shop"."Orders") SELECT COUNT(*) FROM t', self.db_file, row_limit=1000)
        self.assertEqual(result["rows"], [[3]])

    def test_trailing_semicolon_and_whitespace_are_accepted(self) -> None:
        self.assertEqual(check_manual_sql("  SELECT 1;  "), "SELECT 1")

    def test_row_cap_applies_to_manual_query(self) -> None:
        result = run_manual_sql('SELECT "Sales" FROM "shop"."shop"."Orders"', self.db_file, row_limit=2)
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(result["truncated"])

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ExecuteError, "Enter a SQL query"):
            check_manual_sql("   ;  ")

    def test_multiple_statements_are_rejected(self) -> None:
        with self.assertRaisesRegex(ExecuteError, "one SQL statement"):
            check_manual_sql("SELECT 1; SELECT 2")

    def test_non_select_statements_are_rejected(self) -> None:
        for sql in ('DELETE FROM "shop"."shop"."Orders"', "DROP TABLE x", "COPY x TO 'out.csv'", "ATTACH 'other.duckdb'"):
            with self.subTest(sql=sql), self.assertRaisesRegex(ExecuteError, "read-only SELECT"):
                check_manual_sql(sql)

    def test_write_that_passes_check_still_fails_on_read_only_connection(self) -> None:
        with self.assertRaises(ExecuteError):
            run_manual_sql('WITH x AS (SELECT 1 AS a) INSERT INTO "shop"."shop"."Orders" SELECT 9, \'z\' FROM x', self.db_file, row_limit=10)

    def test_bad_column_reports_error_message(self) -> None:
        with self.assertRaisesRegex(ExecuteError, "could not be run"):
            run_manual_sql('SELECT nope FROM "shop"."shop"."Orders"', self.db_file, row_limit=10)


if __name__ == "__main__":
    unittest.main()
