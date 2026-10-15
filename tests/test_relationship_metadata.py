"""Checks that the generated physical relationship metadata meets the A4 contract."""

import unittest
from pathlib import Path

import yaml

PHYSICAL_DIR = Path(__file__).resolve().parents[1] / "specs" / "properties" / "physical_mapping"
CARDINALITIES = {"one_to_one", "many_to_one", "one_to_many", "many_to_many", "unknown"}
JOIN_POLICIES = {"inner", "left"}


def load_relationships(name: str) -> list[dict]:
    with (PHYSICAL_DIR / f"{name}.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["relationships"]


class RelationshipMetadataTests(unittest.TestCase):
    def test_every_relationship_has_cardinality_and_enabled_status(self):
        for name in ("sample_superstore", "netflix_titles", "_113thCongress", "airbnb"):
            with self.subTest(database=name):
                for relationship in load_relationships(name):
                    self.assertIn(relationship["cardinality"], CARDINALITIES)
                    self.assertIsInstance(relationship["enabled"], bool)
                    self.assertIn(relationship["join_policy"], JOIN_POLICIES)

    def test_airbnb_has_no_relationships(self):
        self.assertEqual(load_relationships("airbnb"), [])

    def test_superstore_returns_join_runs_from_orders_to_returns(self):
        by_name = {r["name"]: r for r in load_relationships("sample_superstore")}
        returns_join = by_name["fk_returns_order_id"]
        self.assertEqual(returns_join["child"], {"table": "Orders", "column": "Order_ID"})
        self.assertEqual(returns_join["parent"], {"table": "Returns", "column": "Order_ID"})
        self.assertTrue(returns_join["enabled"])
        self.assertFalse(returns_join["enforceable_now"])

    def test_congress_bills_join_requires_normalization(self):
        by_name = {r["name"]: r for r in load_relationships("_113thCongress")}
        bills_join = by_name["fk_bills_source_url"]
        self.assertTrue(bills_join["enabled"])
        self.assertEqual(bills_join["normalization"]["child"], ["lower", "trim"])
        self.assertEqual(bills_join["normalization"]["parent"], ["lower", "trim"])

    def test_netflix_bridge_joins_cast_parent_key(self):
        for relationship in load_relationships("netflix_titles"):
            with self.subTest(relationship=relationship["name"]):
                self.assertTrue(relationship["enabled"])
                self.assertIn("cast BIGINT", relationship["normalization"]["parent"])
                self.assertEqual(relationship["normalization"]["child"], [])


if __name__ == "__main__":
    unittest.main()
