# Semantic Requirements: `netflix_titles`

## Purpose

Define the semantic mapping for the `netflix_titles` schema, so the Text-to-SQL layer can resolve user filters on its categorical, Boolean, and numeric columns. Only values stored in `databases/netflix_titles.duckdb` are mapped. The generic rules are in [semantic_requirements.md](../semantic_requirements.md).

## Inputs

- Database file: `databases/netflix_titles.duckdb`
- Physical mapping: `specs/properties/physical_mapping/netflix_titles.yaml`
- Manual synonyms: `specs/properties/semantic_mapping/synonyms.yaml` (not created yet; see Business synonyms).

## Output

- File: `specs/properties/semantic_mapping/netflix_titles.yaml`
- Top-level keys: `database`, `schema`, `database_file`, `schema_mapping`, `numeric_operators`, `columns`.
- Column keys: `database`, `schema`, `table`, `column`, `data_type`, `column_class`, then `values` (categorical and Boolean), `true_value` and `false_value` (Boolean; `false_value` is `null` when no stored value is false), `true_expressions` and `false_expressions` (Boolean), `numeric_rules` (numeric), `date_operators` (DATE and TIMESTAMP), `synonyms`, and `flags`.
- Output is deterministic: tables, columns, and values are sorted.

## Column classification

Each column gets one primary class, chosen in this order: boolean, categorical (30 or fewer distinct non-null values), numeric (numeric type, not an identifier, more than 30 values), other. A numeric column with 30 or fewer values is categorical and also gets numeric rules.

Result for `netflix_titles`: 3 categorical, 0 boolean, 1 numeric, 13 other.

| Table | Column | Data type | Class | Values | Numeric rules | Flags |
|---|---|---|---|---|---|---|
| `netflix_titles` | `date_added` | VARCHAR | other | - | - | - |
| `netflix_titles` | `description` | VARCHAR | other | - | - | - |
| `netflix_titles` | `duration_minutes` | VARCHAR | other | - | - | text_numeric |
| `netflix_titles` | `duration_seasons` | VARCHAR | categorical | 17 | - | text_numeric, suspect_value |
| `netflix_titles` | `rating` | VARCHAR | categorical | 14 | - | - |
| `netflix_titles` | `release_year` | DOUBLE | numeric | - | yes | suspect_range |
| `netflix_titles` | `show_id` | DOUBLE | other | - | - | - |
| `netflix_titles` | `title` | VARCHAR | other | - | - | - |
| `netflix_titles` | `type` | VARCHAR | categorical | 3 | - | suspect_value |
| `netflix_titles_cast` | `cast` | VARCHAR | other | - | - | - |
| `netflix_titles_cast` | `show_id` | BIGINT | other | - | - | - |
| `netflix_titles_category` | `listed_in` | VARCHAR | other | - | - | - |
| `netflix_titles_category` | `show_id` | BIGINT | other | - | - | - |
| `netflix_titles_countries` | `country` | VARCHAR | other | - | - | - |
| `netflix_titles_countries` | `show_id` | BIGINT | other | - | - | - |
| `netflix_titles_directors` | `director` | VARCHAR | other | - | - | - |
| `netflix_titles_directors` | `show_id` | BIGINT | other | - | - | - |

## Value mappings

Only values that exist in the column are listed. Columns with more than 30 distinct values have no value mapping.

### `netflix_titles.duration_seasons` (categorical, 17 values)

- `1`
- `10`
- `11`
- `12`
- `13`
- `14`
- `15`
- `2`
- `2017-03-31 00:00:00`
- `3`
- `4`
- `5`
- `6`
- `7`
- `8`
- `80188902`
- `9`

### `netflix_titles.rating` (categorical, 14 values)

- `G`
- `NC-17`
- `NR`
- `PG`
- `PG-13`
- `R`
- `TV-14`
- `TV-G`
- `TV-MA`
- `TV-PG`
- `TV-Y`
- `TV-Y7`
- `TV-Y7-FV`
- `UR`

### `netflix_titles.type` (categorical, 3 values)

- `1944`
- `Movie`
- `TV Show`

## Numeric rules

Comparison operators. Every numeric column in this schema uses this set:

| Operator | Phrases | Template |
|---|---|---|
| `>=` | at least {n}; {n} or more; minimum {n} | `{column} >= {value}` |
| `>` | more than {n}; over {n}; above {n} | `{column} > {value}` |
| `<=` | at most {n}; {n} or less; maximum {n} | `{column} <= {value}` |
| `<` | under {n}; less than {n}; below {n} | `{column} < {value}` |
| `=` | equal to {n}; exactly {n} | `{column} = {value}` |
| `BETWEEN` | between {a} and {b} | `{column} BETWEEN {low} AND {high}` |

| Column | Min | Max | NULL matches | BETWEEN bounds |
|---|---|---|---|---|
| `netflix_titles.release_year` | 1925.0 | 80119194.0 | no | inclusive |

## Date operators

Every DATE or TIMESTAMP column gets `date_operators`: `year`, `month`, `year_month`, and `date_between`. The generator sets them from the physical data type, so no date column is listed by hand.

No date or timestamp columns in this schema.

## Business synonyms

No synonyms are defined for `netflix_titles`. Every column's `synonyms` list is empty. Synonyms are written by hand in `synonyms.yaml`. The generator reads that file, copies each entry into the matching column's `synonyms` list, and never creates the file. An entry that names a column missing from the physical mapping is an error.

## Physical names

- Database and schema: `netflix_titles`. Names are kept exactly as stored in DuckDB.
- Tables: `netflix_titles`, `netflix_titles_cast`, `netflix_titles_category`, `netflix_titles_countries`, `netflix_titles_directors`.
- The generator never renames, re-cases, or quotes names in the YAML. Quoting is the SQL generator's job.

## Data quality flags

Flagged values are reported, not removed or corrected.

- `netflix_titles.duration_minutes`: **text_numeric**. stored as VARCHAR but holds numeric values; numeric rules not generated in this version (exceptions:  and probably will.", Flying Fortress")
- `netflix_titles.duration_seasons`: **text_numeric**. stored as VARCHAR but holds numeric values; numeric rules not generated in this version (exceptions: 2017-03-31 00:00:00)
- `netflix_titles.duration_seasons`: **suspect_value**. looks like a year or date, not a category; kept in values; review before use (values: 2017-03-31 00:00:00)
- `netflix_titles.release_year`: **suspect_range**. outside 1800..2026; min/max keep the stored value, but it is probably a data error (values: 80119194.0)
- `netflix_titles.type`: **suspect_value**. looks like a year or date, not a category; kept in values; review before use (values: 1944)

## Metric definitions

Format and lifecycle are in [requirement_retrival.md](../../retrival/requirement_retrival.md) §6.1 and [design.md](../../retrival/design.md) §3.3.

```yaml
metrics: []
```

## Acceptance criteria

- The YAML covers all 17 columns in `netflix_titles`'s physical mapping, with the same names.
- Every categorical column has 30 or fewer values, and each value appears in its column in DuckDB.
- Boolean mappings only target stored values.
- Numeric rules use the operators listed above, and each includes its observed minimum and maximum.
- Flagged values appear in `flags` and remain in `values`.
- No data is altered.

## Open questions

Each has a recommended answer. Project-level questions (case matching, identifier list, synonym file location) are in [semantic_requirements.md](../semantic_requirements.md).

1. **Misparsed type value.** `netflix_titles.type` holds `1944`, which is a year, not a content type. Should it stay in `values` with a `suspect_value` flag? *Recommended: yes, keep it with the flag.*
2. **Text numbers.** `duration_minutes` and `duration_seasons` are VARCHAR. Should a later version cast them to numbers and add numeric rules? *Recommended: yes, as a separate step after the cast rule is agreed. Until then, only the flag is written.*
3. **Year range.** `release_year` has a `suspect_range` flag that the combined requirement did not list. Should the flag stay? *Recommended: keep the flag with the observed minimum and maximum, and do not change the data.*
