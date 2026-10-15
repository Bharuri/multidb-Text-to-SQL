"""Security tests: unsafe generated SQL is rejected before any database execution.

Each case drives the real pipeline with a scripted SQL generator. The executor is
patched to fail the test if it is ever reached, so a rejection proves the SQL never ran.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from src.query import pipeline
from src.query.catalog import Catalog, ColumnInfo, TableInfo
from src.query.config import DatabaseEntry, load_registry


def _column(name, type_="VARCHAR"):
    return ColumnInfo(name=name, type=type_, nullable=False, has_nulls=False, is_unique=False)


def _analysis_text():
    return json.dumps(
        {
            "status": "ready",
            "analysis": "Total sales per region from orders.",
            "clarification": None,
        }
    )


GOOD_SQL = (
    "SELECT region, sum(sales) AS total FROM orders GROUP BY region"
)


def _sql_json(sql, params=None):
    return json.dumps({"sql": sql, "params": params or [], "assumptions": []})


class FakeClient:
    def __init__(self, text):
        self.text = text

    def complete(self, prompt, on_chunk=None):
        return self.text


class UnsafeSqlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shop.duckdb")
        connection = duckdb.connect(self.db_path)
        connection.execute("CREATE TABLE orders (order_id VARCHAR, region VARCHAR, sales DOUBLE)")
        connection.execute("INSERT INTO orders VALUES ('o1', 'West', 10), ('o2', 'East', 20)")
        connection.close()

        self.registry = load_registry()
        self.catalog = Catalog(
            key="shop",
            schema="shop",
            database_file=self.db_path,
            tables={
                "orders": TableInfo(
                    name="orders",
                    source_sheet="orders",
                    row_count=2,
                    columns={c: _column(c) for c in ("order_id", "region", "sales")},
                )
            },
            semantic_columns=(),
            relationships=(),
            numeric_operators=(),
            fingerprint=(),
            metrics=(),
        )
        entry = DatabaseEntry(
            key="shop",
            physical_mapping=Path(self.db_path),
            semantic_mapping=Path(self.db_path),
            database_file=Path(self.db_path),
            database_file_setting="",
            business_mapping=None,
        )
        self.executor = mock.Mock(side_effect=AssertionError("unsafe SQL reached the executor"))
        patches = [
            mock.patch.object(pipeline, "select_database", return_value=entry),
            mock.patch.object(pipeline, "load_catalog", return_value=self.catalog),
            mock.patch.object(pipeline, "execute_read_only", self.executor),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_sql(self, sql, params=None):
        return pipeline.run_pipeline(
            "Total sales by region?",
            "shop",
            registry=self.registry,
            planner=FakeClient(_analysis_text()),
            sql_generator=FakeClient(_sql_json(sql, params)),
            request_id="security-test",
        )

    def assertRejected(self, sql, params=None):
        response = self.run_sql(sql, params)
        self.assertIn(response.status, {"rejected", "cannot_answer"}, response)
        self.assertIn(response.category, {"invalid_sql", "unsafe_sql", "unsupported"}, response)
        self.assertFalse(
            any(s.status == "ok" and s.stage == "execute" for s in response.trace), response
        )
        self.executor.assert_not_called()
        return response

    # Statement-level and write attempts

    def test_multiple_statements_are_rejected(self):
        self.assertRejected("SELECT region FROM orders; DROP TABLE orders")

    def test_insert_is_rejected(self):
        self.assertRejected("INSERT INTO orders VALUES ('o3', 'North', 5)")

    def test_update_is_rejected(self):
        self.assertRejected("UPDATE orders SET sales = 0")

    def test_delete_is_rejected(self):
        self.assertRejected("DELETE FROM orders")

    def test_create_table_is_rejected(self):
        self.assertRejected("CREATE TABLE leak AS SELECT * FROM orders")

    def test_drop_is_rejected(self):
        self.assertRejected("DROP TABLE orders")

    def test_copy_is_rejected(self):
        self.assertRejected("COPY orders TO '/tmp/out.csv'")

    def test_pragma_is_rejected(self):
        self.assertRejected("PRAGMA database_list")

    def test_attach_is_rejected(self):
        self.assertRejected("ATTACH '/tmp/other.duckdb' AS other")

    # External file and table-function access

    def test_read_csv_file_scan_is_rejected(self):
        self.assertRejected("SELECT * FROM read_csv('/etc/passwd')")

    def test_read_parquet_file_scan_is_rejected(self):
        self.assertRejected("SELECT * FROM read_parquet('secret.parquet')")

    def test_glob_file_scan_is_rejected(self):
        self.assertRejected("SELECT * FROM glob('*')")

    def test_duckdb_catalog_table_is_rejected(self):
        self.assertRejected("SELECT database_name FROM duckdb_databases()")

    # Unknown tables and columns

    def test_unknown_table_is_rejected(self):
        self.assertRejected(
            "SELECT region, sum(sales) AS total FROM secret_table GROUP BY region"
        )

    def test_unknown_column_is_rejected(self):
        self.assertRejected(
            "SELECT region, sum(cost) AS total FROM orders GROUP BY region"
        )

    # Unapproved functions and constructs

    def test_sample_clause_is_rejected(self):
        self.assertRejected(
            "WITH totals AS (SELECT region, sales FROM orders USING SAMPLE 10%) "
            "SELECT region, sum(sales) AS total FROM totals GROUP BY region"
        )

    # CTE bypass attempts

    def test_cte_wrapped_write_is_rejected(self):
        self.assertRejected(
            "WITH x AS (SELECT 1) INSERT INTO orders SELECT 'o9', 'X', 1"
        )

    def test_cte_named_like_a_base_table_does_not_grant_access(self):
        self.assertRejected(
            "WITH orders AS (SELECT * FROM read_csv('/etc/passwd')) "
            "SELECT region, sum(sales) AS total FROM orders GROUP BY region"
        )

    def test_cte_joined_to_unapproved_table_is_rejected(self):
        self.assertRejected(
            "WITH hidden AS (SELECT * FROM orders JOIN secret ON orders.order_id = secret.id) "
            "SELECT region, sum(sales) AS total FROM hidden GROUP BY region"
        )

    # Parameter mismatches

    def test_placeholder_without_matching_param_is_rejected(self):
        self.assertRejected(
            "SELECT region, sum(sales) AS total FROM orders WHERE region = ? GROUP BY region"
        )

    def test_extra_param_value_is_rejected(self):
        self.assertRejected(GOOD_SQL, params=["unused"])

    def test_mixed_placeholder_styles_are_rejected(self):
        self.assertRejected(
            "SELECT region, sum(sales) AS total FROM orders "
            "WHERE region = ? AND order_id = $oid GROUP BY region",
            params=["West"],
        )

    # Excess complexity

    def test_oversized_sql_is_rejected(self):
        padding = " " * 25000
        self.assertRejected(GOOD_SQL + padding)

    def test_deeply_nested_subqueries_are_rejected(self):
        inner = "SELECT region, sales FROM orders"
        for i in range(self.registry.limits.max_nesting_depth + 2):
            inner = f"SELECT region, sales FROM ({inner}) AS t{i}"
        self.assertRejected(
            f"SELECT region, sum(sales) AS total FROM ({inner}) AS top GROUP BY region"
        )

    def test_too_many_ctes_is_rejected(self):
        count = self.registry.limits.max_ctes + 1
        ctes = ", ".join(f"c{i} AS (SELECT region, sales FROM orders)" for i in range(count))
        self.assertRejected(
            f"WITH {ctes}, totals AS (SELECT region, sum(sales) AS total FROM orders GROUP BY region) "
            "SELECT region, total FROM totals"
        )


class SafeControlTests(unittest.TestCase):
    """Control: well-formed SQL must reach the executor, so the rejections above are meaningful."""

    def setUp(self):
        self.fixture = UnsafeSqlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)

    def test_valid_sql_reaches_the_executor(self):
        self.fixture.executor.side_effect = None
        self.fixture.executor.return_value = mock.Mock(
            columns=["region", "total"], rows=[("East", 20.0), ("West", 10.0)],
            row_count=2, truncated=False,
        )
        response = self.fixture.run_sql(GOOD_SQL)
        self.assertEqual(response.status, "ok", response)
        self.fixture.executor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
