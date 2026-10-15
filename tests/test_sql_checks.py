import dataclasses
import unittest

from src.query.catalog import Catalog, ColumnInfo, SemanticColumn, TableInfo
from src.query.sql_ast import SqlAstError
from src.query.sql_checks import check_sql_names_and_values, rewrite_column_names


def _col(name, type_="VARCHAR"):
    return ColumnInfo(name=name, type=type_, nullable=False, has_nulls=False, is_unique=False)


def _catalog():
    return Catalog(
        key="shop",
        schema="shop",
        database_file="unused.duckdb",
        tables={
            "Orders": TableInfo(
                name="Orders",
                source_sheet="Orders",
                row_count=3,
                columns={
                    "Region": _col("Region"),
                    "Sales": _col("Sales", "DOUBLE"),
                },
            )
        },
        semantic_columns=(
            SemanticColumn(
                table="Orders",
                column="Region",
                data_type="VARCHAR",
                column_class="categorical",
                synonyms=(),
                values=("West", "East"),
                flags=(),
            ),
        ),
        relationships=(),
        numeric_operators=(),
        fingerprint=(),
        metrics=(),
    )


def _catalog_with_order_date():
    catalog = _catalog()
    orders = catalog.tables["Orders"]
    columns = {**orders.columns, "Order_Date": _col("Order_Date", "DATE")}
    return Catalog(
        key=catalog.key,
        schema=catalog.schema,
        database_file=catalog.database_file,
        tables={"Orders": TableInfo(name="Orders", source_sheet="Orders", row_count=3, columns=columns)},
        semantic_columns=catalog.semantic_columns,
        relationships=catalog.relationships,
        numeric_operators=catalog.numeric_operators,
        fingerprint=catalog.fingerprint,
        metrics=catalog.metrics,
    )


def _catalog_with_orders_columns(*names):
    base = _catalog()
    columns = {name: _col(name, "DATE") for name in names}
    return dataclasses.replace(
        base, tables={"Orders": TableInfo(name="Orders", source_sheet="Orders", row_count=3, columns=columns)}
    )


class RewriteColumnNamesTests(unittest.TestCase):
    def test_spaced_quoted_identifier_is_rewritten_to_physical_name(self):
        sql, rewrites = rewrite_column_names('SELECT o."Order Date" FROM Orders AS o', _catalog_with_order_date())
        self.assertEqual(sql, 'SELECT o."Order_Date" FROM Orders AS o')
        self.assertEqual(rewrites, [("Order Date", "Order_Date")])

    def test_bare_identifier_without_separator_is_rewritten(self):
        sql, rewrites = rewrite_column_names("SELECT OrderDate FROM Orders", _catalog_with_order_date())
        self.assertEqual(sql, 'SELECT "Order_Date" FROM Orders')
        self.assertEqual(rewrites, [("OrderDate", "Order_Date")])

    def test_physical_name_is_left_unchanged(self):
        sql = 'SELECT "Order_Date" FROM Orders'
        self.assertEqual(rewrite_column_names(sql, _catalog_with_order_date()), (sql, []))

    def test_string_literal_is_not_rewritten(self):
        sql = "SELECT Region FROM Orders WHERE Region = 'Order Date'"
        self.assertEqual(rewrite_column_names(sql, _catalog_with_order_date()), (sql, []))

    def test_output_alias_is_not_rewritten(self):
        sql = 'SELECT Region AS "Order Date" FROM Orders ORDER BY "Order Date"'
        self.assertEqual(rewrite_column_names(sql, _catalog_with_order_date()), (sql, []))

    def test_ambiguous_match_is_left_for_rejection(self):
        catalog = _catalog_with_orders_columns("Order_Date", "OrderDate")
        sql = 'SELECT "Order Date" FROM Orders'
        self.assertEqual(rewrite_column_names(sql, catalog), (sql, []))

    def test_unmatched_name_is_left_for_rejection(self):
        sql = 'SELECT "Cost" FROM Orders'
        self.assertEqual(rewrite_column_names(sql, _catalog_with_order_date()), (sql, []))

    def test_placeholder_is_preserved(self):
        sql = 'SELECT "Order Date" FROM Orders WHERE Region = ?'
        rewritten, _ = rewrite_column_names(sql, _catalog_with_order_date())
        self.assertEqual(rewritten, 'SELECT "Order_Date" FROM Orders WHERE Region = ?')

    def test_unparseable_sql_is_returned_unchanged(self):
        sql = "SELECT FROM ("
        self.assertEqual(rewrite_column_names(sql, _catalog_with_order_date()), (sql, []))


class ColumnNameTests(unittest.TestCase):
    def test_physical_columns_are_accepted(self):
        check_sql_names_and_values(
            "SELECT Region, sum(Sales) AS total FROM Orders GROUP BY Region", [], _catalog()
        )

    def test_output_alias_can_be_ordered_by(self):
        check_sql_names_and_values(
            "SELECT Region, sum(Sales) AS total FROM Orders GROUP BY Region ORDER BY total DESC", [], _catalog()
        )

    def test_cte_output_column_is_accepted(self):
        check_sql_names_and_values(
            "WITH t AS (SELECT Region, Sales FROM Orders) SELECT Region FROM t", [], _catalog()
        )

    def test_qualified_cte_column_is_accepted(self):
        # A CTE reference parses as a BASE_TABLE, so its qualifier must not be looked up as a physical table.
        check_sql_names_and_values(
            "WITH t AS (SELECT Region, Sales FROM Orders) SELECT t.Region, t.Sales FROM t AS t", [], _catalog()
        )

    def test_unknown_column_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "unknown column 'Cost'"):
            check_sql_names_and_values("SELECT sum(Cost) FROM Orders", [], _catalog())

    def test_unknown_qualified_column_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "unknown column 'Cost' in table 'Orders'"):
            check_sql_names_and_values("SELECT o.Cost FROM Orders AS o", [], _catalog())

    def test_spaced_column_name_gets_physical_hint(self):
        # Mirrors the round-8 Q43 failure: "Order Date" must point the repair at Order_Date.
        with self.assertRaisesRegex(SqlAstError, "unknown column 'Order Date' .*did you mean 'Order_Date'"):
            check_sql_names_and_values('SELECT "Order Date" FROM Orders', [], _catalog_with_order_date())

    def test_all_unknown_columns_are_reported_together(self):
        # Mirrors the round-10 Q33 failure: the repair must see both bad names, not only the first.
        with self.assertRaises(SqlAstError) as ctx:
            check_sql_names_and_values(
                'SELECT "Order Date", "Ship Date" FROM Orders', [], _catalog_with_order_date()
            )
        self.assertIn("unknown column 'Order Date'", ctx.exception.reason)
        self.assertIn("unknown column 'Ship Date'", ctx.exception.reason)

    def test_qualified_column_missing_from_cte_output_is_rejected(self):
        # The CTE selects Region and Sales, so a reference to rank through its alias must fail.
        with self.assertRaisesRegex(SqlAstError, "unknown column 'rank' in CTE 't'"):
            check_sql_names_and_values(
                "WITH t AS (SELECT Region, Sales FROM Orders) SELECT x.rank FROM t AS x", [], _catalog()
            )

    def test_qualified_column_present_in_cte_output_is_accepted(self):
        check_sql_names_and_values(
            "WITH t AS (SELECT Region, Sales AS total FROM Orders) SELECT x.total FROM t AS x", [], _catalog()
        )

    def test_alias_reused_for_different_ctes_in_different_scopes_is_accepted(self):
        # Mirrors the Q99 false rejection: "p" names t in one scope and u in another, so its columns are not checked.
        check_sql_names_and_values(
            "WITH t AS (SELECT Region, Sales FROM Orders), "
            "u AS (SELECT Region, Sales AS total FROM Orders), "
            "v AS (SELECT p.Region FROM t AS p), "
            "w AS (SELECT p.total FROM u AS p) "
            "SELECT w.total FROM w",
            [],
            _catalog(),
        )

    def test_cte_column_alias_list_defines_output(self):
        check_sql_names_and_values(
            "WITH t(r, s) AS (SELECT Region, Sales FROM Orders) SELECT t.r, t.s FROM t", [], _catalog()
        )

    def test_cte_with_star_is_not_checked_by_output(self):
        check_sql_names_and_values(
            "WITH t AS (SELECT * FROM Orders) SELECT t.Region FROM t", [], _catalog()
        )

    def test_cte_on_union_uses_left_branch_outputs(self):
        check_sql_names_and_values(
            "WITH t AS (SELECT Region AS r FROM Orders UNION ALL SELECT Region FROM Orders) SELECT t.r FROM t",
            [],
            _catalog(),
        )
        with self.assertRaisesRegex(SqlAstError, "unknown column 'Sales' in CTE 't'"):
            check_sql_names_and_values(
                "WITH t AS (SELECT Region AS r FROM Orders UNION ALL SELECT Region FROM Orders) SELECT t.Sales FROM t",
                [],
                _catalog(),
            )


class LiteralValueTests(unittest.TestCase):
    def test_listed_value_is_accepted_regardless_of_case(self):
        check_sql_names_and_values("SELECT Sales FROM Orders WHERE Region = ?", ["west"], _catalog())

    def test_unlisted_value_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "not a listed value of Orders.Region"):
            check_sql_names_and_values("SELECT Sales FROM Orders WHERE Region = ?", ["North"], _catalog())

    def test_unlisted_value_in_an_in_list_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "Orders.Region"):
            check_sql_names_and_values(
                "SELECT Sales FROM Orders WHERE Region IN (?, ?)", ["West", "Mars"], _catalog()
            )

    def test_numeric_parameter_is_not_checked_against_values(self):
        check_sql_names_and_values("SELECT Region FROM Orders WHERE Sales > ?", [5], _catalog())

    def test_inline_number_is_not_checked(self):
        check_sql_names_and_values("SELECT Region FROM Orders WHERE Sales > 0", [], _catalog())

    def test_inline_listed_value_is_accepted(self):
        check_sql_names_and_values("SELECT Sales FROM Orders WHERE Region = 'west'", [], _catalog())

    def test_inline_unlisted_value_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "not a listed value of Orders.Region"):
            check_sql_names_and_values("SELECT Sales FROM Orders WHERE Region = 'Mars'", [], _catalog())

    def test_inline_unlisted_value_in_an_in_list_is_rejected(self):
        with self.assertRaisesRegex(SqlAstError, "Orders.Region"):
            check_sql_names_and_values(
                "SELECT Sales FROM Orders WHERE Region IN ('West', 'Mars')", [], _catalog()
            )

    def test_inline_string_outside_a_comparison_is_not_checked(self):
        check_sql_names_and_values("SELECT date_trunc('month', Region) AS m FROM Orders", [], _catalog())

    def test_column_without_listed_values_is_not_checked(self):
        check_sql_names_and_values("SELECT Region FROM Orders WHERE Sales = ?", ["anything"], _catalog())


if __name__ == "__main__":
    unittest.main()
