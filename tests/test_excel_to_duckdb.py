import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
import pandas as pd
import yaml

from src.ingestion import main as app

try:
    import xlrd  # noqa: F401
    import xlwt
except ImportError:
    xlwt = None
    xlrd_available = False
else:
    xlrd_available = True


def write_xls(path: Path, sheets: dict[str, list[list]]) -> None:
    book = xlwt.Workbook()
    for sheet_name, rows in sheets.items():
        sheet = book.add_sheet(sheet_name)
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                sheet.write(row_index, col_index, value)
    book.save(str(path))


class ExcelToDuckDBTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "data"
        self.output_dir = self.root / "databases"
        self.input_dir.mkdir()
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump({"input_dir": str(self.input_dir), "database_dir": str(self.output_dir)}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def write_xlsx(self, name: str, sheets: dict[str, pd.DataFrame]) -> Path:
        path = self.input_dir / name
        with pd.ExcelWriter(path) as writer:
            for sheet_name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=sheet_name, index=False)
        return path

    def run_app(self) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(app, "CONFIG_PATH", self.config_path), contextlib.redirect_stdout(output):
            code = app.main()
        return code, output.getvalue()

    def query(self, database: str, sql: str) -> list[tuple]:
        connection = duckdb.connect(str(self.output_dir / f"{database}.duckdb"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def schemas(self, database: str) -> set[str]:
        rows = self.query(
            database,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'main')",
        )
        return {row[0] for row in rows}

    def tables(self, database: str, schema: str) -> set[str]:
        rows = self.query(
            database,
            f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}'",
        )
        return {row[0] for row in rows}

    def test_multiple_sheets_become_tables_in_workbook_schema(self):
        self.write_xlsx(
            "sales.xlsx",
            {
                "Orders": pd.DataFrame({"Order ID": [1, 2], "Amount": [10.5, None]}),
                "People": pd.DataFrame({"Name": ["Ann", "Ben"]}),
            },
        )

        code, _ = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(self.schemas("sales"), {"sales"})
        self.assertEqual(self.tables("sales", "sales"), {"Orders", "People"})
        self.assertEqual(
            self.query("sales", "SELECT Order_ID, Amount FROM sales.sales.Orders ORDER BY Order_ID"),
            [(1, 10.5), (2, None)],
        )
        self.assertEqual(
            self.query("sales", "SELECT Name FROM sales.sales.People ORDER BY Name"),
            [("Ann",), ("Ben",)],
        )

    def test_two_workbooks_create_separate_database_files(self):
        self.write_xlsx("alpha.xlsx", {"Data": pd.DataFrame({"x": [1]})})
        self.write_xlsx("beta.xlsx", {"Data": pd.DataFrame({"y": [2]})})

        code, _ = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(path.name for path in self.output_dir.glob("*.duckdb")),
            ["alpha.duckdb", "beta.duckdb"],
        )
        self.assertEqual(self.query("alpha", "SELECT x FROM alpha.alpha.Data"), [(1,)])
        self.assertEqual(self.query("beta", "SELECT y FROM beta.beta.Data"), [(2,)])
        self.assertEqual(self.schemas("alpha"), {"alpha"})
        self.assertEqual(self.schemas("beta"), {"beta"})

    def test_reprocessing_replaces_existing_table_data(self):
        self.write_xlsx("stock.xlsx", {"Items": pd.DataFrame({"qty": [1, 2, 3]})})
        self.run_app()

        self.write_xlsx("stock.xlsx", {"Items": pd.DataFrame({"qty": [9]})})
        code, _ = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(self.query("stock", "SELECT qty FROM stock.stock.Items"), [(9,)])

    def test_reprocessing_drops_sheets_removed_from_workbook(self):
        self.write_xlsx(
            "stock.xlsx",
            {"Items": pd.DataFrame({"qty": [1]}), "Old": pd.DataFrame({"x": [1]})},
        )
        self.run_app()

        self.write_xlsx("stock.xlsx", {"Items": pd.DataFrame({"qty": [2]})})
        code, _ = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(self.tables("stock", "stock"), {"Items"})

    def test_reprocessing_yields_identical_schema_and_row_counts(self):
        self.write_xlsx(
            "repeat.xlsx",
            {
                "Orders": pd.DataFrame({"Order ID": [1, 2, 2], "Amount": [10.5, None, 3.0]}),
                "People": pd.DataFrame({"Name": ["Ann", "Ben"]}),
            },
        )

        def fingerprint():
            columns = self.query(
                "repeat",
                "SELECT table_name, column_name, data_type, ordinal_position "
                "FROM information_schema.columns WHERE table_schema = 'repeat' ORDER BY 1, 4",
            )
            counts = [
                self.query("repeat", f'SELECT count(*) FROM repeat.repeat."{table}"')[0][0]
                for table in sorted({row[0] for row in columns})
            ]
            return columns, counts

        self.run_app()
        first = fingerprint()
        self.run_app()

        self.assertEqual(fingerprint(), first)
        self.assertEqual(first[1], [3, 2])

    def test_invalid_workbook_does_not_stop_valid_workbook(self):
        (self.input_dir / "broken.xlsx").write_text("not an excel file", encoding="utf-8")
        self.write_xlsx("good.xlsx", {"Data": pd.DataFrame({"v": [42]})})

        code, output = self.run_app()

        self.assertEqual(code, 1)
        self.assertIn("broken.xlsx", output)
        self.assertEqual(self.query("good", "SELECT v FROM good.good.Data"), [(42,)])

    def test_duplicate_and_invalid_column_names_are_made_unique(self):
        frame = pd.DataFrame([[1, 2, 3]], columns=["Order ID", "Order ID", "2020"])
        self.write_xlsx("cols.xlsx", {"Data": frame})

        self.run_app()

        columns = [row[0] for row in self.query("cols", "DESCRIBE cols.cols.Data")]
        self.assertEqual(columns, ["Order_ID", "Order_ID_1", "_2020"])

    def test_empty_sheet_is_skipped_and_reported(self):
        self.write_xlsx(
            "mixed.xlsx",
            {"Filled": pd.DataFrame({"a": [1]}), "Empty": pd.DataFrame()},
        )

        code, output = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(self.tables("mixed", "mixed"), {"Filled"})
        self.assertIn("Empty", output)

    def test_mixed_type_column_is_stored_as_text(self):
        self.write_xlsx("types.xlsx", {"Data": pd.DataFrame({"value": [1, "two", 3.0]})})

        self.run_app()

        self.assertEqual(
            self.query("types", "SELECT value FROM types.types.Data ORDER BY rowid"),
            [("1",), ("two",), ("3",)],
        )

    def test_database_name_conflict_is_reported_not_merged(self):
        self.write_xlsx("a b.xlsx", {"Data": pd.DataFrame({"v": [1]})})
        self.write_xlsx("a_b.xlsx", {"Data": pd.DataFrame({"v": [2]})})

        code, output = self.run_app()

        self.assertEqual(code, 1)
        self.assertIn("already used", output)
        self.assertEqual(self.query("a_b", "SELECT v FROM a_b.a_b.Data"), [(1,)])

    @unittest.skipUnless(xlrd_available, "requires xlrd and xlwt")
    def test_xls_workbook_is_loaded(self):
        write_xls(
            self.input_dir / "legacy.xls",
            {"Table": [["code", "score"], ["a", 1], ["b", 2]]},
        )

        code, _ = self.run_app()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.query("legacy", "SELECT code, score FROM legacy.legacy.Table ORDER BY code"),
            [("a", 1), ("b", 2)],
        )


if __name__ == "__main__":
    unittest.main()
