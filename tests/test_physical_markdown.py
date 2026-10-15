import unittest

from src.query.config import MetadataMismatchError
from src.query.physical_md import check_against_physical, parse_physical_markdown

MD = """# Physical

## Tables

| name | kind |
|---|---|
| `Orders` | table |

## Columns

| column | type | nullable | has_nulls | unique |
|---|---|---|---|---|

### `Orders`

| column | type | nullable | has_nulls | unique |
|---|---|---|---|---|
| `Sales` | DOUBLE | yes | no | no |
| `Customer_ID` | VARCHAR | yes | no | no |

## Relationships between tables

| from | to |
|---|---|
| `Orders` | `People` |
"""


class PhysicalMarkdownTests(unittest.TestCase):
    def test_reads_only_columns_section_and_skips_header_rows(self):
        schema = parse_physical_markdown(MD, "orders.md")
        self.assertEqual(schema, {"Orders": {"Sales": "DOUBLE", "Customer_ID": "VARCHAR"}})

    def test_crlf_input_parses_the_same(self):
        schema = parse_physical_markdown(MD.replace("\n", "\r\n"), "orders.md")
        self.assertEqual(schema["Orders"]["Customer_ID"], "VARCHAR")

    def test_duplicate_table_is_rejected(self):
        text = MD.replace("### `Orders`", "### `Orders`\n\n### `Orders`", 1)
        with self.assertRaises(MetadataMismatchError):
            parse_physical_markdown(text, "orders.md")

    def test_no_tables_is_rejected(self):
        with self.assertRaises(MetadataMismatchError):
            parse_physical_markdown("## Columns\n", "empty.md")

    def test_cross_check_passes_when_both_sources_agree(self):
        physical = {"Orders": {"Sales": "DOUBLE", "Customer_ID": "VARCHAR"}}
        check_against_physical(parse_physical_markdown(MD, "orders.md"), physical, "orders.md")

    def test_cross_check_reports_missing_column_in_either_direction(self):
        md = {"Orders": {"Sales": "DOUBLE"}}
        physical = {"Orders": {"Sales": "DOUBLE", "Customer_ID": "VARCHAR"}}
        with self.assertRaisesRegex(MetadataMismatchError, "Orders.Customer_ID"):
            check_against_physical(md, physical, "orders.md")
        with self.assertRaisesRegex(MetadataMismatchError, "Orders.Customer_ID"):
            check_against_physical(physical, md, "orders.md")

    def test_cross_check_reports_type_mismatch(self):
        md = {"Orders": {"Sales": "DOUBLE"}}
        physical = {"Orders": {"Sales": "INTEGER"}}
        with self.assertRaisesRegex(MetadataMismatchError, "Orders.Sales"):
            check_against_physical(md, physical, "orders.md")


if __name__ == "__main__":
    unittest.main()
