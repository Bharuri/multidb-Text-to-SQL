# Requirement: `netflix_titles` Schema Mapping

## Purpose

Describe the `netflix_titles` schema in `databases/netflix_titles.duckdb`: its 5 table(s), 17 columns, and the relationships between those tables. The SQL generator uses this file to recreate the schema. It is the per-schema part of [requirement.md](../requirement.md).

## Source

- Database file: `databases/netflix_titles.duckdb`
- Workbook: `data/netflix_titles.xlsx`
- Database and schema name: `netflix_titles` (the same name, so SQL uses three-part names)
- Physical mapping: `specs/properties/physical_mapping/netflix_titles.yaml`
- Semantic requirement: [netflix_titles.md](../semantic_requirements/netflix_titles.md)

## Tables

| Table | Source sheet | Rows | Columns |
|---|---|---|---|
| `netflix_titles` | `netflix_titles` | 6,236 | 9 |
| `netflix_titles_cast` | `netflix_titles_cast` | 44,311 | 2 |
| `netflix_titles_category` | `netflix_titles_category` | 13,670 | 2 |
| `netflix_titles_countries` | `netflix_titles_countries` | 7,179 | 2 |
| `netflix_titles_directors` | `netflix_titles_directors` | 4,852 | 2 |

## Columns

### `netflix_titles`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `duration_minutes` | VARCHAR | yes | yes | no |
| `duration_seasons` | VARCHAR | yes | yes | no |
| `type` | VARCHAR | yes | yes | no |
| `title` | VARCHAR | yes | yes | no |
| `date_added` | VARCHAR | yes | yes | no |
| `release_year` | DOUBLE | yes | yes | no |
| `rating` | VARCHAR | yes | yes | no |
| `description` | VARCHAR | yes | yes | no |
| `show_id` | DOUBLE | yes | yes | yes |

### `netflix_titles_cast`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `cast` | VARCHAR | yes | no | no |
| `show_id` | BIGINT | yes | no | no |

### `netflix_titles_category`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `listed_in` | VARCHAR | yes | no | no |
| `show_id` | BIGINT | yes | no | no |

### `netflix_titles_countries`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `country` | VARCHAR | yes | no | no |
| `show_id` | BIGINT | yes | no | no |

### `netflix_titles_directors`

| Column | Type | Nullable | Has NULLs | Unique |
|---|---|---|---|---|
| `director` | VARCHAR | yes | no | no |
| `show_id` | BIGINT | yes | no | no |

## Relationships between tables

| Relationship | Child (table.column) | Parent (table.column) | Cardinality | Enforceable now |
|---|---|---|---|---|
| `fk_netflix_titles_cast_show_id` | `netflix_titles_cast.show_id` | `netflix_titles.show_id` | many-to-one | no |
| `fk_netflix_titles_category_show_id` | `netflix_titles_category.show_id` | `netflix_titles.show_id` | many-to-one | no |
| `fk_netflix_titles_countries_show_id` | `netflix_titles_countries.show_id` | `netflix_titles.show_id` | many-to-one | no |
| `fk_netflix_titles_directors_show_id` | `netflix_titles_directors.show_id` | `netflix_titles.show_id` | many-to-one | no |

### `fk_netflix_titles_cast_show_id`

- Status: proposed
- Meaning: Each child row is a credit for one title in the parent table.
- Enforceable now: no
- Issues: type mismatch: show_id is BIGINT, show_id is DOUBLE; 10 child row(s) have no matching parent value in show_id
- Checks:
  - `show_id`: child BIGINT, parent DOUBLE, orphan rows 10

### `fk_netflix_titles_category_show_id`

- Status: proposed
- Meaning: Each child row is a credit for one title in the parent table.
- Enforceable now: no
- Issues: type mismatch: show_id is BIGINT, show_id is DOUBLE; 3 child row(s) have no matching parent value in show_id
- Checks:
  - `show_id`: child BIGINT, parent DOUBLE, orphan rows 3

### `fk_netflix_titles_countries_show_id`

- Status: proposed
- Meaning: Each child row is a credit for one title in the parent table.
- Enforceable now: no
- Issues: type mismatch: show_id is BIGINT, show_id is DOUBLE; 2 child row(s) have no matching parent value in show_id
- Checks:
  - `show_id`: child BIGINT, parent DOUBLE, orphan rows 2

### `fk_netflix_titles_directors_show_id`

- Status: proposed
- Meaning: Each child row is a credit for one title in the parent table.
- Enforceable now: no
- Issues: type mismatch: show_id is BIGINT, show_id is DOUBLE; 2 child row(s) have no matching parent value in show_id
- Checks:
  - `show_id`: child BIGINT, parent DOUBLE, orphan rows 2

## Key candidates

Single-column key candidates are columns that are unique and free of NULLs. A candidate is not a declared key; see the open questions.

- `netflix_titles`: none
- `netflix_titles_cast`: none
- `netflix_titles_category`: none
- `netflix_titles_countries`: none
- `netflix_titles_directors`: none

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

- Emit `CREATE SCHEMA` for `netflix_titles`, then one `CREATE TABLE` per table with the listed types and `NOT NULL` where `nullable` is false.
- Use three-part quoted names: `"netflix_titles"."netflix_titles"."<table>"`.
- Add `PRIMARY KEY` and `FOREIGN KEY` clauses only for relationships with `enforceable_now: true`, and only where the parent column is unique. Write unenforced relationships as SQL comments.
- Declare constraints inside `CREATE TABLE`. Do not add them to existing tables afterwards.

## Acceptance criteria

- The YAML loads with a standard YAML parser.
- It lists 5 table(s) and 17 column(s), matching the `netflix_titles` database in DuckDB.
- Each relationship has a check for each child column, and each relationship with `enforceable_now: false` has at least one issue.
- Relationships are a proposal. Nothing in the ingestion code changes because of this file.

## Open questions

Each has a recommended answer. Project-level questions are in [requirement.md](../requirement.md).

1. **show_id type and orphans.** The parent `netflix_titles.show_id` is DOUBLE, but all four child tables use BIGINT, and 17 child rows have no matching parent value. Should the SQL use BIGINT for `show_id`, keep all rows, and leave the four relationships unenforced? *Recommended: yes.*
2. **Parent NULLs.** `netflix_titles.show_id` is unique but holds 4 NULL values, so it is not a key candidate. Should those rows be kept, with the key declared only after the NULLs are explained? *Recommended: keep the rows, and leave `show_id` unkeyed until they are explained.*
