# Semantic Requirements: `_113thCongress`

## Purpose

Define the semantic mapping for the `_113thCongress` schema, so the Text-to-SQL layer can resolve user filters on its categorical, Boolean, and numeric columns. Only values stored in `databases/_113thCongress.duckdb` are mapped. The generic rules are in [semantic_requirements.md](../semantic_requirements.md).

## Inputs

- Database file: `databases/_113thCongress.duckdb`
- Physical mapping: `specs/properties/physical_mapping/_113thCongress.yaml`
- Manual synonyms: `specs/properties/semantic_mapping/synonyms.yaml` (not created yet; see Business synonyms).

## Output

- File: `specs/properties/semantic_mapping/_113thCongress.yaml`
- Top-level keys: `database`, `schema`, `database_file`, `schema_mapping`, `numeric_operators`, `columns`.
- Column keys: `database`, `schema`, `table`, `column`, `data_type`, `column_class`, then `values` (categorical and Boolean), `true_value` and `false_value` (Boolean; `false_value` is `null` when no stored value is false), `true_expressions` and `false_expressions` (Boolean), `numeric_rules` (numeric), `date_operators` (DATE and TIMESTAMP), `synonyms`, and `flags`.
- Output is deterministic: tables, columns, and values are sorted.

## Column classification

Each column gets one primary class, chosen in this order: boolean, categorical (30 or fewer distinct non-null values), numeric (numeric type, not an identifier, more than 30 values), other. A numeric column with 30 or fewer values is categorical and also gets numeric rules.

Result for `_113thCongress`: 7 categorical, 0 boolean, 6 numeric, 13 other.

| Table | Column | Data type | Class | Values | Numeric rules | Flags |
|---|---|---|---|---|---|---|
| `Bills` | `BillNumber` | VARCHAR | other | - | - | - |
| `Bills` | `Bill_Number` | VARCHAR | other | - | - | - |
| `Bills` | `Body` | VARCHAR | categorical | 2 | - | - |
| `Bills` | `Count` | BIGINT | numeric | - | yes | - |
| `Bills` | `Description` | VARCHAR | other | - | - | - |
| `Bills` | `Party` | VARCHAR | categorical | 2 | - | - |
| `Bills` | `Question` | VARCHAR | other | - | - | - |
| `Bills` | `Record` | BIGINT | other | - | - | - |
| `Bills` | `Required_for_Majority` | VARCHAR | categorical | 8 | - | suspect_value |
| `Bills` | `Role_Call_Number` | BIGINT | numeric | - | yes | - |
| `Bills` | `Session` | BIGINT | categorical | 2 | yes | - |
| `Bills` | `Source_URL` | VARCHAR | other | - | - | - |
| `Bills` | `Total_Abstain` | BIGINT | numeric | - | yes | - |
| `Bills` | `Total_No` | BIGINT | numeric | - | yes | - |
| `Bills` | `Total_Yes` | BIGINT | numeric | - | yes | - |
| `Bills` | `Vote` | VARCHAR | categorical | 3 | - | - |
| `Bills` | `Vote_Date` | VARCHAR | other | - | - | - |
| `Bills` | `Vote_Result` | VARCHAR | categorical | 19 | - | - |
| `Sunlight_Info` | `Pages` | DOUBLE | numeric | - | yes | - |
| `Sunlight_Info` | `Source_URL` | VARCHAR | other | - | - | - |
| `Sunlight_Info` | `Sponsor_Party` | VARCHAR | categorical | 3 | - | - |
| `Sunlight_Info` | `State` | VARCHAR | other | - | - | - |
| `Sunlight_Info` | `committee` | VARCHAR | other | - | - | - |
| `Sunlight_Info` | `official_text` | VARCHAR | other | - | - | - |
| `Sunlight_Info` | `sponsor` | VARCHAR | other | - | - | - |
| `Sunlight_Info` | `sponsor_pic` | VARCHAR | other | - | - | - |

## Value mappings

Only values that exist in the column are listed. Columns with more than 30 distinct values have no value mapping.

### `Bills.Body` (categorical, 2 values)

- `House of Representatives`
- `Senate`

### `Bills.Party` (categorical, 2 values)

- `Democrats`
- `Republicans`

### `Bills.Required_for_Majority` (categorical, 8 values)

- `2/3 RECORDED VOTE`
- `2/3 YEA-AND-NAY`
- `2013-01-02 00:00:00`
- `2013-03-05 00:00:00`
- `2014-01-02 00:00:00`
- `2014-03-05 00:00:00`
- `RECORDED VOTE`
- `YEA-AND-NAY`

### `Bills.Session` (categorical, 2 values)

- `1`
- `2`

### `Bills.Vote` (categorical, 3 values)

- `Abstain`
- `No`
- `Yes`

### `Bills.Vote_Result` (categorical, 19 values)

- `Agreed to`
- `Amendment Agreed to`
- `Amendment Rejected`
- `Bill Passed`
- `Cloture Motion Agreed to`
- `Cloture Motion Rejected`
- `Cloture on the Motion to Proceed Agreed to`
- `Cloture on the Motion to Proceed Rejected`
- `Concurrent Resolution Agreed to`
- `Conference Report Agreed to`
- `Failed`
- `Joint Resolution Passed`
- `Motion Agreed to`
- `Motion Rejected`
- `Motion for Attendance Agreed to`
- `Motion to Proceed Rejected`
- `Motion to Table Agreed to`
- `Motion to Table Failed`
- `Passed`

### `Sunlight_Info.Sponsor_Party` (categorical, 3 values)

- `D`
- `I`
- `R`

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
| `Bills.Count` | 0 | 230 | no | inclusive |
| `Bills.Role_Call_Number` | 2 | 641 | no | inclusive |
| `Bills.Session` | 1 | 2 | no | inclusive |
| `Bills.Total_Abstain` | 0 | 84 | no | inclusive |
| `Bills.Total_No` | 0 | 413 | no | inclusive |
| `Bills.Total_Yes` | 2 | 426 | no | inclusive |
| `Sunlight_Info.Pages` | 0.0 | 1795.0 | no | inclusive |

## Date operators

Every DATE or TIMESTAMP column gets `date_operators`: `year`, `month`, `year_month`, and `date_between`. The generator sets them from the physical data type, so no date column is listed by hand.

No date or timestamp columns in this schema.

## Business synonyms

No synonyms are defined for `_113thCongress`. Every column's `synonyms` list is empty. Synonyms are written by hand in `synonyms.yaml`. The generator reads that file, copies each entry into the matching column's `synonyms` list, and never creates the file. An entry that names a column missing from the physical mapping is an error.

## Physical names

- Database and schema: `_113thCongress`. Names are kept exactly as stored in DuckDB.
- Tables: `Bills`, `Sunlight_Info`.
- The generator never renames, re-cases, or quotes names in the YAML. Quoting is the SQL generator's job.

## Data quality flags

Flagged values are reported, not removed or corrected.

- `Bills.Required_for_Majority`: **suspect_value**. looks like a year or date, not a category; kept in values; review before use (values: 2013-01-02 00:00:00, 2013-03-05 00:00:00, 2014-01-02 00:00:00, 2014-03-05 00:00:00)

## Acceptance criteria

- The YAML covers all 26 columns in `_113thCongress`'s physical mapping, with the same names.
- Every categorical column has 30 or fewer values, and each value appears in its column in DuckDB.
- Boolean mappings only target stored values.
- Numeric rules use the operators listed above, and each includes its observed minimum and maximum.
- Flagged values appear in `flags` and remain in `values`.
- No data is altered.

## Open questions

Each has a recommended answer. Project-level questions (case matching, identifier list, synonym file location) are in [semantic_requirements.md](../semantic_requirements.md).

1. **Numeric columns with few values.** `Bills.Session` has 2 values, so it gets both categorical values and numeric rules. Should a numeric column with 30 or fewer values keep both? *Recommended: keep both, so "session 1" matches exactly and "at least 2" still works.*
2. **Suspect values.** `Bills.Required_for_Majority` holds date-like values such as `2013-01-02 00:00:00`. Should they stay in `values` with a `suspect_value` flag, or be removed? *Recommended: keep them with the flag, so filters on them still resolve and no data is hidden.*
