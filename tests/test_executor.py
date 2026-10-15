"""Tests for the read-only executor (E1). Uses temporary DuckDB files only."""

import tempfile
import time
import unittest
from pathlib import Path

import duckdb

from src.query.executor import ExecutionError, execute_read_only


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "fixture.duckdb"
        connection = duckdb.connect(str(self.db))
        connection.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) v(n, s)")
        connection.close()

    def tearDown(self):
        self._tmp.cleanup()

    def run_sql(self, sql, params=(), max_rows=1000, timeout=30):
        return execute_read_only(self.db, sql, params, max_rows, timeout)


class ResultTests(ExecutorTestBase):
    def test_returns_columns_rows_and_counts(self):
        result = self.run_sql("SELECT n, s FROM t ORDER BY n")
        self.assertEqual(result.columns, ["n", "s"])
        self.assertEqual(result.rows, [[1, "a"], [2, "b"], [3, "c"]])
        self.assertEqual(result.row_count, 3)
        self.assertFalse(result.truncated)

    def test_empty_result_keeps_columns(self):
        result = self.run_sql("SELECT n FROM t WHERE n > 99")
        self.assertEqual(result.columns, ["n"])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.row_count, 0)
        self.assertFalse(result.truncated)

    def test_rows_over_cap_are_truncated_and_flagged(self):
        result = self.run_sql("SELECT n FROM t ORDER BY n", max_rows=2)
        self.assertEqual(result.rows, [[1], [2]])
        self.assertEqual(result.row_count, 2)
        self.assertTrue(result.truncated)

    def test_result_exactly_at_cap_is_not_truncated(self):
        result = self.run_sql("SELECT n FROM t", max_rows=3)
        self.assertEqual(result.row_count, 3)
        self.assertFalse(result.truncated)


class ParameterTests(ExecutorTestBase):
    def test_params_are_bound_as_values_not_sql(self):
        hostile = "x'; DROP TABLE t; --"
        result = self.run_sql("SELECT ? AS value", [hostile])
        self.assertEqual(result.rows, [[hostile]])
        still_there = self.run_sql("SELECT count(*) FROM t")
        self.assertEqual(still_there.rows, [[3]])

    def test_positional_params_bind_in_order(self):
        result = self.run_sql("SELECT ? AS a, ? AS b", ["left", 7])
        self.assertEqual(result.rows, [["left", 7]])


class ReadOnlyTests(ExecutorTestBase):
    def assert_write_rejected(self, sql):
        with self.assertRaises(ExecutionError):
            self.run_sql(sql)
        connection = duckdb.connect(str(self.db), read_only=True)
        try:
            tables = [row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables").fetchall()]
            count = connection.execute("SELECT count(*) FROM t").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(tables, ["t"])
        self.assertEqual(count, 3)

    def test_create_table_is_rejected(self):
        self.assert_write_rejected("CREATE TABLE evil AS SELECT 1")

    def test_insert_is_rejected(self):
        self.assert_write_rejected("INSERT INTO t VALUES (9, 'z')")

    def test_delete_is_rejected(self):
        self.assert_write_rejected("DELETE FROM t")

    def test_attach_is_rejected(self):
        with self.assertRaises(ExecutionError):
            self.run_sql(f"ATTACH '{self.db.parent / 'other.duckdb'}' AS other")


class FailureTests(ExecutorTestBase):
    def test_runtime_error_is_safe_and_categorised(self):
        with self.assertRaises(ExecutionError) as ctx:
            self.run_sql("SELECT * FROM no_such_table_xyz")
        self.assertEqual(ctx.exception.category, "execution_error")
        self.assertEqual(ctx.exception.reason, "the query failed at runtime")
        self.assertNotIn("no_such_table_xyz", ctx.exception.reason)
        self.assertIn("no_such_table_xyz", ctx.exception.diagnostic)

    def test_missing_database_file_is_reported(self):
        missing = Path(self._tmp.name) / "absent.duckdb"
        with self.assertRaises(ExecutionError) as ctx:
            execute_read_only(missing, "SELECT 1", [], 10, 5)
        self.assertEqual(ctx.exception.reason, "the database could not be opened")

    def test_timeout_interrupts_long_query(self):
        started = time.monotonic()
        with self.assertRaises(ExecutionError) as ctx:
            self.run_sql("SELECT count(*) FROM range(100000000000)", timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertIn("timeout", ctx.exception.reason)
        self.assertLess(elapsed, 20)

    def test_file_is_usable_after_timeout(self):
        with self.assertRaises(ExecutionError):
            self.run_sql("SELECT count(*) FROM range(100000000000)", timeout=0.3)
        self.assertEqual(self.run_sql("SELECT count(*) FROM t").rows, [[3]])

    def test_invalid_limits_are_programming_errors(self):
        with self.assertRaises(ValueError):
            self.run_sql("SELECT 1", max_rows=0)
        with self.assertRaises(ValueError):
            self.run_sql("SELECT 1", timeout=0)


class SchemaTests(unittest.TestCase):
    """Tables live in the dataset's schema, so unqualified names must resolve through it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "fixture.duckdb"
        connection = duckdb.connect(str(self.db))
        connection.execute("CREATE SCHEMA ds")
        connection.execute("CREATE TABLE ds.Orders AS SELECT * FROM (VALUES (1), (2)) v(n)")
        connection.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unqualified_table_resolves_through_dataset_schema(self):
        result = execute_read_only(self.db, "SELECT count(*) AS c FROM orders", [], 10, 30, schema="ds")
        self.assertEqual(result.rows, [[2]])

    def test_unqualified_table_is_not_found_without_schema(self):
        with self.assertRaises(ExecutionError):
            execute_read_only(self.db, "SELECT count(*) FROM orders", [], 10, 30)


if __name__ == "__main__":
    unittest.main()
