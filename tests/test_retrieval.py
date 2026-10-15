import unittest

from src.query import catalog as cat
from src.query import config as cfg
from src.query import retrieval as ret

KEY = "sample_superstore"


def column(name, col_type="VARCHAR"):
    return {"name": name, "type": col_type, "nullable": False, "has_nulls": False, "is_unique": False}


def semantic(table, col, cls, values=(), synonyms=()):
    return {
        "table": table,
        "column": col,
        "data_type": "VARCHAR",
        "column_class": cls,
        "values": list(values),
        "synonyms": list(synonyms),
        "flags": [],
    }


def relationship(name, child, parent, enabled=True):
    return {
        "name": name,
        "status": "approved",
        "meaning": "m",
        "cardinality": "many_to_one",
        "enabled": enabled,
        "join_policy": "left",
        "normalization": {"child": [], "parent": []},
        "fan_out": "none",
        "child": {"table": child[0], "column": child[1]},
        "parent": {"table": parent[0], "column": parent[1]},
    }


def make_catalog(key="alpha"):
    physical = {
        "schema": key,
        "file": f"databases/{key}.duckdb",
        "tables": [
            {"name": "Orders", "source_sheet": "Orders", "row_count": 3,
             "columns": [column("Order_ID"), column("Region"), column("Sales", "DOUBLE")]},
            {"name": "Returns", "source_sheet": "Returns", "row_count": 1,
             "columns": [column("Order_ID"), column("Reason")]},
            {"name": "Unrelated", "source_sheet": "Unrelated", "row_count": 1,
             "columns": [column("Widget")]},
        ],
        "relationships": [
            relationship("orders_returns", ("Orders", "Order_ID"), ("Returns", "Order_ID")),
            relationship("disabled", ("Orders", "Region"), ("Unrelated", "Widget"), enabled=False),
        ],
    }
    sem = {
        "columns": [
            semantic("Orders", "Region", "categorical", values=["North", "South"], synonyms=["territory"]),
            semantic("Returns", "Reason", "categorical", values=["Damaged"]),
        ],
        "numeric_operators": [],
    }
    return cat.build_catalog(key, physical, sem)


class RetrieveTests(unittest.TestCase):
    def setUp(self):
        self.catalog = make_catalog()

    def test_selects_tables_and_columns_matching_question_terms(self):
        ctx = ret.retrieve(self.catalog, "total Sales by Region")
        self.assertEqual(ctx.tables, ["Orders"])
        names = {c.column for c in ctx.columns}
        self.assertEqual(names, {"Region", "Sales"})

    def test_unrelated_tables_are_excluded(self):
        ctx = ret.retrieve(self.catalog, "total Sales by Region")
        self.assertNotIn("Unrelated", ctx.tables)

    def test_value_mention_selects_categorical_column_with_values(self):
        ctx = ret.retrieve(self.catalog, "orders where the territory is North")
        hit = next(c for c in ctx.columns if c.column == "Region")
        self.assertIn("North", hit.values)

    def test_value_list_over_threshold_is_not_exposed(self):
        # Threshold 0 means no value list may be used for matching or exposure.
        ctx = ret.retrieve(self.catalog, "damaged returns", value_list_threshold=0)
        self.assertFalse(any(c.values for c in ctx.columns))
        self.assertNotIn("Reason", {c.column for c in ctx.columns})

    def test_only_enabled_relationships_with_both_endpoints_selected_are_included(self):
        ctx = ret.retrieve(self.catalog, "Sales and Returns Reason per Order_ID")
        names = [r.name for r in ctx.relationships]
        self.assertIn("orders_returns", names)
        self.assertNotIn("disabled", names)

    def test_no_match_returns_empty_context(self):
        ctx = ret.retrieve(self.catalog, "zebra population")
        self.assertEqual(ctx.columns, [])
        self.assertEqual(ctx.tables, [])

    def test_retrieval_never_leaves_its_database(self):
        other = make_catalog("beta")
        ctx = ret.retrieve(other, "Sales by Region")
        self.assertEqual(ctx.database, "beta")
        self.assertTrue(all(r.name != "disabled" for r in ctx.relationships))


class NormalizationTests(unittest.TestCase):
    """Plural question words match singular names, and the reverse (design §4, step 5b)."""

    def test_singular_rules(self):
        cases = {
            "customers": "customer",
            "categories": "category",
            "boxes": "box",
            "classes": "class",
            "titles": "title",
            "status": "status",
            "address": "address",
            "yes": "yes",
            "id": "id",
        }
        for word, expected in cases.items():
            with self.subTest(word=word):
                self.assertEqual(ret._singular(word), expected)

    def test_question_plural_matches_singular_column_name(self):
        physical = {
            "schema": "gamma",
            "file": "databases/gamma.duckdb",
            "tables": [
                {"name": "Orders", "source_sheet": "Orders", "row_count": 1,
                 "columns": [column("Customer_ID"), column("Sales", "DOUBLE")]},
            ],
            "relationships": [],
        }
        catalog = cat.build_catalog("gamma", physical, {"columns": [], "numeric_operators": []})
        ctx = ret.retrieve(catalog, "distinct customers")
        self.assertEqual({c.column for c in ctx.columns}, {"Customer_ID"})

    def test_plural_question_word_matches_singular_column_in_fixture(self):
        ctx = ret.retrieve(make_catalog(), "total orders by region")
        names = {c.column for c in ctx.columns}
        self.assertIn("Order_ID", names)
        self.assertIn("Region", names)

    def test_plural_question_word_matches_singular_synonym(self):
        ctx = ret.retrieve(make_catalog(), "sales per territories")
        hit = next(c for c in ctx.columns if c.column == "Region")
        self.assertIn("territory", hit.reason)

    def test_stopwords_are_dropped_after_normalization(self):
        self.assertEqual(ret.tokenize("shows the counts"), set())


class TimeSignalTests(unittest.TestCase):
    """Temporal columns are included when a question has a time signal (design §4, step 5a)."""

    def setUp(self):
        physical = {
            "schema": "alpha",
            "file": "databases/alpha.duckdb",
            "tables": [
                {"name": "Orders", "source_sheet": "Orders", "row_count": 3,
                 "columns": [column("Order_ID"), column("Region"), column("Sales", "DOUBLE"),
                             column("Order_Date", "DATE"), column("Ship_Date", "DATE")]},
                {"name": "Titles", "source_sheet": "Titles", "row_count": 2,
                 "columns": [column("Title"), column("date_added"), column("Release")]},
                {"name": "Shipments", "source_sheet": "Shipments", "row_count": 1,
                 "columns": [column("Shipment_ID"), column("Shipped_At", "TIMESTAMP")]},
                {"name": "Unrelated", "source_sheet": "Unrelated", "row_count": 1,
                 "columns": [column("Widget")]},
            ],
            "relationships": [],
        }
        sem = {
            "columns": [
                semantic("Orders", "Region", "categorical", values=["North"]),
                semantic("Titles", "date_added", "date"),
            ],
            "numeric_operators": [],
        }
        self.catalog = cat.build_catalog("alpha", physical, sem)

    def _columns(self, ctx):
        return {(c.table, c.column) for c in ctx.columns}

    def test_year_in_question_adds_temporal_columns_of_selected_table(self):
        ctx = ret.retrieve(self.catalog, "total sales by region in 2024")
        cols = self._columns(ctx)
        self.assertIn(("Orders", "Order_Date"), cols)
        self.assertIn(("Orders", "Ship_Date"), cols)
        hit = next(c for c in ctx.columns if c.column == "Order_Date")
        self.assertEqual(hit.reason, "time_signal")

    def test_temporal_word_adds_temporal_columns(self):
        ctx = ret.retrieve(self.catalog, "sales trend by region")
        self.assertIn(("Orders", "Order_Date"), self._columns(ctx))

    def test_varchar_date_column_is_found_by_semantic_class(self):
        ctx = ret.retrieve(self.catalog, "titles added over time")
        self.assertIn(("Titles", "date_added"), self._columns(ctx))

    def test_timestamp_column_is_found_by_physical_type(self):
        ctx = ret.retrieve(self.catalog, "shipments per month")
        self.assertIn(("Shipments", "Shipped_At"), self._columns(ctx))

    def test_no_time_signal_adds_no_temporal_columns(self):
        ctx = ret.retrieve(self.catalog, "total Sales by Region")
        self.assertEqual({c.column for c in ctx.columns}, {"Region", "Sales"})

    def test_time_signal_with_no_lexical_match_uses_all_tables(self):
        ctx = ret.retrieve(self.catalog, "zebra counts over time")
        cols = self._columns(ctx)
        self.assertIn(("Shipments", "Shipped_At"), cols)
        self.assertIn(("Orders", "Order_Date"), cols)
        self.assertNotIn(("Unrelated", "Widget"), cols)

    def test_four_digit_year_is_a_time_signal_but_other_numbers_are_not(self):
        self.assertEqual(ret.time_signal_terms({"2024"}), {"2024"})
        self.assertEqual(ret.time_signal_terms({"1234"}), set())
        self.assertEqual(ret.time_signal_terms({"region", "sales"}), set())

    def test_temporal_columns_are_not_added_for_unrelated_tables(self):
        ctx = ret.retrieve(self.catalog, "total Sales by Region in 2024")
        self.assertNotIn("Shipments", ctx.tables)


class BudgetTests(unittest.TestCase):
    def test_over_budget_raises_and_never_truncates(self):
        ctx = ret.retrieve(make_catalog(), "total Sales by Region")
        with self.assertRaises(ret.ContextTooLargeError):
            ret.enforce_budget(ctx, max_chars=10)

    def test_within_budget_returns_context_unchanged(self):
        ctx = ret.retrieve(make_catalog(), "total Sales by Region")
        self.assertIs(ret.enforce_budget(ctx, max_chars=100000), ctx)

    def test_prompt_text_includes_every_selected_column(self):
        ctx = ret.retrieve(make_catalog(), "Sales and Region")
        text = ctx.to_prompt_text()
        self.assertIn("Sales", text)
        self.assertIn("Region", text)


class RealSuperstoreTests(unittest.TestCase):
    def test_real_question_stays_inside_superstore_and_includes_region(self):
        registry = cfg.load_registry(cfg.CONFIG_PATH)
        catalog = cat.load_catalog(registry, KEY)
        ctx = ret.retrieve(catalog, "total Sales by Region")
        self.assertEqual(ctx.database, KEY)
        self.assertTrue(all(t in catalog.tables for t in ctx.tables))
        self.assertTrue(any(c.column == "Sales" for c in ctx.columns))


if __name__ == "__main__":
    unittest.main()
