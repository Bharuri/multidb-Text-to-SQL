# Requirement: `sample_superstore` Schema Mapping

## Purpose

Describe the `sample_superstore` schema in `databases/sample_superstore.duckdb`: its 3 table(s), 25 columns, and the relationships between those tables. The SQL generator uses this file to recreate the schema. It is the per-schema part of [requirement.md](../requirement.md).

## Source

- Database file: `databases/sample_superstore.duckdb`
- Workbook: `data/sample_-_superstore.xls`
- Database and schema name: `sample_superstore` (the same name, so SQL uses three-part names)
- Physical mapping: `specs/properties/physical_mapping/sample_superstore.yaml`
- Semantic requirement: [sample_superstore.md](../semantic_requirements/sample_superstore.md)

## Tables

| Table | Source sheet | Rows | Columns |
|---|---|---|---|
| `Orders` | `Orders` | 10,194 | 21 |
| `People` | `People` | 4 | 2 |
| `Returns` | `Returns` | 296 | 2 |

## Columns

### `Orders`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Row_ID` | BIGINT | yes | no | yes |
| `Order_ID` | VARCHAR | yes | no | no |
| `Order_Date` | TIMESTAMP | yes | no | no |
| `Ship_Date` | TIMESTAMP | yes | no | no |
| `Ship_Mode` | VARCHAR | yes | no | no |
| `Customer_ID` | VARCHAR | yes | no | no |
| `Customer_Name` | VARCHAR | yes | no | no |
| `Segment` | VARCHAR | yes | no | no |
| `Country_Region` | VARCHAR | yes | no | no |
| `City` | VARCHAR | yes | no | no |
| `State_Province` | VARCHAR | yes | no | no |
| `Postal_Code` | VARCHAR | yes | no | no |
| `Region` | VARCHAR | yes | no | no |
| `Product_ID` | VARCHAR | yes | no | no |
| `Category` | VARCHAR | yes | no | no |
| `Sub_Category` | VARCHAR | yes | no | no |
| `Product_Name` | VARCHAR | yes | no | no |
| `Sales` | DOUBLE | yes | no | no |
| `Quantity` | BIGINT | yes | no | no |
| `Discount` | DOUBLE | yes | no | no |
| `Profit` | DOUBLE | yes | no | no |

### `People`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Regional_Manager` | VARCHAR | yes | no | yes |
| `Region` | VARCHAR | yes | no | yes |

### `Returns`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Order_ID` | VARCHAR | yes | no | yes |
| `Returned` | VARCHAR | yes | no | no |

## Relationships between tables

| Relationship | Child (table.column) | Parent (table.column) | Cardinality | Enforceable now |
|---|---|---|---|---|
| `fk_returns_order_id` | `Orders.Order_ID` | `Returns.Order_ID` | many-to-one | no |
| `fk_orders_region` | `Orders.Region` | `People.Region` | many-to-one | yes |

### `fk_returns_order_id`

- Status: approved
- Meaning: Each order line may have a return. Returns has one row per returned order, so Order_ID is unique on the parent side. Only 296 of the 5,111 distinct orders have a return, so the link is optional.
- Enforceable now: no
- Enabled for joins: yes
- Join policy: left
- Normalization child: none
- Normalization parent: none
- Fan-out: Returns is unique per Order_ID, so Orders-to-Returns does not multiply rows. The reverse path (Returns to Orders) repeats each return once per order line (800 lines match). Return-side metrics must use `count(distinct Order_ID)` or pre-aggregate before joining.
- Issues: none for the join direction. Foreign key is not declared because 9,394 order lines have no matching return, which is expected for an optional parent.
- Checks:
  - `Order_ID`: child VARCHAR, parent VARCHAR, orphan rows 9394

### `fk_orders_region`

- Status: approved
- Meaning: Each order line belongs to one regional manager region listed in People.
- Enforceable now: yes
- Enabled for joins: yes
- Join policy: inner
- Normalization child: none
- Normalization parent: none
- Fan-out: none; Region is unique in People.
- Issues: none
- Checks:
  - `Region`: child VARCHAR, parent VARCHAR, orphan rows 0

## Key candidates

Single-column key candidates are columns that are unique and free of NULLs. A candidate is not a declared key; see the open questions.

- `Orders`: `Row_ID`
- `People`: `Regional_Manager`, `Region`
- `Returns`: `Order_ID`

## Rules

1. Names are exactly as stored in DuckDB, including case and underscores. Quoting is the SQL generator's job.
2. Types are DuckDB type names from `information_schema.columns`.
3. `nullable` is the declared nullability. `has_nulls` means the stored data contains a NULL. `is_unique` means all non-NULL values are distinct.
4. `source_sheet` is the original worksheet name, kept for tracing only.
5. Relationships are inferred from the data, not declared in the workbook, and are marked `status: proposed`.
6. A relationship is enforceable only when child and parent types match, parent values are unique among non-NULL values, and every non-NULL child value exists in the parent.
7. Relationships are listed only within this schema, because DuckDB cannot enforce a foreign key across database files.
8. The file holds no credentials, row data, or sample values.

## Use in SQL creation

- Emit `CREATE SCHEMA` for `sample_superstore`, then one `CREATE TABLE` per table with the listed types and `NOT NULL` where `nullable` is false.
- Use three-part quoted names: `"sample_superstore"."sample_superstore"."<table>"`.
- Add `PRIMARY KEY` and `FOREIGN KEY` clauses only for relationships with `enforceable_now: true`, and only where the parent column is unique. Write unenforced relationships as SQL comments.
- Declare constraints inside `CREATE TABLE`. Do not add them to existing tables afterwards.

## Acceptance criteria

- The YAML loads with a standard YAML parser.
- It lists 3 table(s) and 25 column(s), matching the `sample_superstore` database in DuckDB.
- Each relationship has a check for each child column, and each relationship with `enforceable_now: false` has at least one issue.
- Relationships are a proposal. Nothing in the ingestion code changes because of this file.

## Open questions

Each has a recommended answer. Project-level questions are in [requirement.md](../requirement.md).

1. **Returns to Orders.** `Orders.Order_ID` repeats, because `Orders` has one row per line item. Should a derived `Orders` parent with one row per `Order_ID` be created so the relationship can be enforced? *Recommended: no. That is a business transformation, so leave the relationship unenforced.*
2. **Primary keys.** The single-column key candidates are `Orders.Row_ID`, `People.Region`, `People.Regional_Manager`, and `Returns.Order_ID`. Which should be declared `PRIMARY KEY`? *Recommended: `Orders.Row_ID` and `People.Region`. Leave the others unkeyed until confirmed.*
