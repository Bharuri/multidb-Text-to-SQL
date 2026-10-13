# Requirement: `airbnb` Schema Mapping

## Purpose

Describe the `airbnb` schema in `databases/airbnb.duckdb`: its 1 table(s), 13 columns, and the relationships between those tables. The SQL generator uses this file to recreate the schema. It is the per-schema part of [requirement.md](../requirement.md).

## Source

- Database file: `databases/airbnb.duckdb`
- Workbook: `data/airbnb.xlsx`
- Database and schema name: `airbnb` (the same name, so SQL uses three-part names)
- Physical mapping: `specs/properties/physical_mapping/airbnb.yaml`
- Semantic requirement: [airbnb.md](../semantic_requirements/airbnb.md)

## Tables

| Table | Source sheet | Rows | Columns |
|---|---|---|---|
| `AirBnB_NYC` | `AirBnB_NYC` | 30,478 | 13 |

## Columns

### `AirBnB_NYC`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Host_Id` | BIGINT | yes | no | no |
| `Host_Since` | TIMESTAMP | yes | yes | no |
| `Name` | VARCHAR | yes | no | no |
| `Neighbourhood` | VARCHAR | yes | no | no |
| `Property_Type` | VARCHAR | yes | yes | no |
| `Review_Scores_Rating_bin` | DOUBLE | yes | yes | no |
| `Room_Type` | VARCHAR | yes | no | no |
| `Zipcode` | DOUBLE | yes | yes | no |
| `Beds` | DOUBLE | yes | yes | no |
| `Number_of_Records` | BIGINT | yes | no | no |
| `Number_Of_Reviews` | BIGINT | yes | no | no |
| `Price` | BIGINT | yes | no | no |
| `Review_Scores_Rating` | DOUBLE | yes | yes | no |

## Relationships between tables

No relationship is approved between the tables in `airbnb`. The tables are not linked, so the SQL generator emits no `FOREIGN KEY` for them.

## Key candidates

Single-column key candidates are columns that are unique and free of NULLs. A candidate is not a declared key; see the open questions.

- `AirBnB_NYC`: none

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

- Emit `CREATE SCHEMA` for `airbnb`, then one `CREATE TABLE` per table with the listed types and `NOT NULL` where `nullable` is false.
- Use three-part quoted names: `"airbnb"."airbnb"."<table>"`.
- Add `PRIMARY KEY` and `FOREIGN KEY` clauses only for relationships with `enforceable_now: true`, and only where the parent column is unique. Write unenforced relationships as SQL comments.
- Declare constraints inside `CREATE TABLE`. Do not add them to existing tables afterwards.

## Acceptance criteria

- The YAML loads with a standard YAML parser.
- It lists 1 table(s) and 13 column(s), matching the `airbnb` database in DuckDB.
- Each relationship has a check for each child column, and each relationship with `enforceable_now: false` has at least one issue.
- Relationships are a proposal. Nothing in the ingestion code changes because of this file.

## Open questions

Each has a recommended answer. Project-level questions are in [requirement.md](../requirement.md).

1. **No key.** `AirBnB_NYC` has no single-column key candidate, because no column is both unique and free of NULLs. Should the table stay unkeyed, or should a composite key be proposed? *Recommended: leave it unkeyed for the POC.*
