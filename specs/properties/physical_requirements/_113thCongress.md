# Requirement: `_113thCongress` Schema Mapping

## Purpose

Describe the `_113thCongress` schema in `databases/_113thCongress.duckdb`: its 2 table(s), 26 columns, and the relationships between those tables. The SQL generator uses this file to recreate the schema. It is the per-schema part of [requirement.md](../requirement.md).

## Source

- Database file: `databases/_113thCongress.duckdb`
- Workbook: `data/113thCongress.xlsx`
- Database and schema name: `_113thCongress` (the same name, so SQL uses three-part names)
- Physical mapping: `specs/properties/physical_mapping/_113thCongress.yaml`
- Semantic requirement: [_113thCongress.md](../semantic_requirements/_113thCongress.md)

## Tables

| Table | Source sheet | Rows | Columns |
|---|---|---|---|
| `Bills` | `Bills` | 4,410 | 18 |
| `Sunlight_Info` | `Sunlight Info` | 286 | 8 |

## Columns

### `Bills`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Record` | BIGINT | yes | no | no |
| `Vote_Date` | VARCHAR | yes | no | no |
| `Question` | VARCHAR | yes | no | no |
| `Bill_Number` | VARCHAR | yes | no | no |
| `Description` | VARCHAR | yes | yes | no |
| `Role_Call_Number` | BIGINT | yes | no | no |
| `Required_for_Majority` | VARCHAR | yes | no | no |
| `Total_Yes` | BIGINT | yes | no | no |
| `Total_No` | BIGINT | yes | no | no |
| `Total_Abstain` | BIGINT | yes | no | no |
| `Session` | BIGINT | yes | no | no |
| `Body` | VARCHAR | yes | no | no |
| `BillNumber` | VARCHAR | yes | no | no |
| `Source_URL` | VARCHAR | yes | no | no |
| `Vote_Result` | VARCHAR | yes | no | no |
| `Vote` | VARCHAR | yes | no | no |
| `Party` | VARCHAR | yes | no | no |
| `Count` | BIGINT | yes | no | no |

### `Sunlight_Info`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `Source_URL` | VARCHAR | yes | no | yes |
| `official_text` | VARCHAR | yes | yes | yes |
| `sponsor` | VARCHAR | yes | no | no |
| `Sponsor_Party` | VARCHAR | yes | no | no |
| `State` | VARCHAR | yes | no | no |
| `sponsor_pic` | VARCHAR | yes | no | no |
| `committee` | VARCHAR | yes | yes | no |
| `Pages` | DOUBLE | yes | yes | no |

## Relationships between tables

No relationship is approved between the tables in `_113thCongress`. The tables are not linked, so the SQL generator emits no `FOREIGN KEY` for them.

## Key candidates

Single-column key candidates are columns that are unique and free of NULLs. A candidate is not a declared key; see the open questions.

- `Bills`: none
- `Sunlight_Info`: `Source_URL`

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

- Emit `CREATE SCHEMA` for `_113thCongress`, then one `CREATE TABLE` per table with the listed types and `NOT NULL` where `nullable` is false.
- Use three-part quoted names: `"_113thCongress"."_113thCongress"."<table>"`.
- Add `PRIMARY KEY` and `FOREIGN KEY` clauses only for relationships with `enforceable_now: true`, and only where the parent column is unique. Write unenforced relationships as SQL comments.
- Declare constraints inside `CREATE TABLE`. Do not add them to existing tables afterwards.

## Acceptance criteria

- The YAML loads with a standard YAML parser.
- It lists 2 table(s) and 26 column(s), matching the `_113thCongress` database in DuckDB.
- Each relationship has a check for each child column, and each relationship with `enforceable_now: false` has at least one issue.
- Relationships are a proposal. Nothing in the ingestion code changes because of this file.

## Open questions

Each has a recommended answer. Project-level questions are in [requirement.md](../requirement.md).

1. **Bills key.** `Bills.Record` is not unique: 735 distinct values across 4,410 rows. What identifies one row in `Bills`? If there is no natural key, `Bills` has no primary key. *Recommended: leave `Bills` unkeyed until you confirm a key.*
2. **Sunlight_Info key.** `Sunlight_Info.Source_URL` is the only single-column key candidate. Should it be declared `PRIMARY KEY`? *Recommended: no, until you confirm it identifies one sponsor record.*
