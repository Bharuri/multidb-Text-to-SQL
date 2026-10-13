# Semantic Requirements: `airbnb`

## Purpose

Define the semantic mapping for the `airbnb` schema, so the Text-to-SQL layer can resolve user filters on its categorical, Boolean, and numeric columns. Only values stored in `databases/airbnb.duckdb` are mapped. The generic rules are in [semantic_requirements.md](../semantic_requirements.md).

## Inputs

- Database file: `databases/airbnb.duckdb`
- Physical mapping: `specs/properties/physical_mapping/airbnb.yaml`
- Manual synonyms: `specs/properties/semantic_mapping/synonyms.yaml` (not created yet; see Business synonyms).

## Output

- File: `specs/properties/semantic_mapping/airbnb.yaml`
- Top-level keys: `database`, `schema`, `database_file`, `schema_mapping`, `numeric_operators`, `columns`.
- Column keys: `database`, `schema`, `table`, `column`, `data_type`, `column_class`, then `values` (categorical and Boolean), `true_value` and `false_value` (Boolean; `false_value` is `null` when no stored value is false), `true_expressions` and `false_expressions` (Boolean), `numeric_rules` (numeric), `date_operators` (DATE and TIMESTAMP), `synonyms`, and `flags`.
- Output is deterministic: tables, columns, and values are sorted.

## Column classification

Each column gets one primary class, chosen in this order: boolean, categorical (30 or fewer distinct non-null values), numeric (numeric type, not an identifier, more than 30 values), other. A numeric column with 30 or fewer values is categorical and also gets numeric rules.

Result for `airbnb`: 6 categorical, 0 boolean, 3 numeric, 4 other.

| Table | Column | Data type | Class | Values | Numeric rules | Flags |
|---|---|---|---|---|---|---|
| `AirBnB_NYC` | `Beds` | DOUBLE | categorical | 14 | yes | - |
| `AirBnB_NYC` | `Host_Id` | BIGINT | other | - | - | - |
| `AirBnB_NYC` | `Host_Since` | TIMESTAMP | other | - | - | - |
| `AirBnB_NYC` | `Name` | VARCHAR | other | - | - | - |
| `AirBnB_NYC` | `Neighbourhood` | VARCHAR | categorical | 5 | - | - |
| `AirBnB_NYC` | `Number_Of_Reviews` | BIGINT | numeric | - | yes | - |
| `AirBnB_NYC` | `Number_of_Records` | BIGINT | categorical | 1 | yes | - |
| `AirBnB_NYC` | `Price` | BIGINT | numeric | - | yes | - |
| `AirBnB_NYC` | `Property_Type` | VARCHAR | categorical | 19 | - | - |
| `AirBnB_NYC` | `Review_Scores_Rating` | DOUBLE | numeric | - | yes | - |
| `AirBnB_NYC` | `Review_Scores_Rating_bin` | DOUBLE | categorical | 15 | yes | - |
| `AirBnB_NYC` | `Room_Type` | VARCHAR | categorical | 3 | - | - |
| `AirBnB_NYC` | `Zipcode` | DOUBLE | other | - | - | - |

## Value mappings

Only values that exist in the column are listed. Columns with more than 30 distinct values have no value mapping.

### `AirBnB_NYC.Beds` (categorical, 14 values)

- `0.0`
- `1.0`
- `2.0`
- `3.0`
- `4.0`
- `5.0`
- `6.0`
- `7.0`
- `8.0`
- `9.0`
- `10.0`
- `11.0`
- `12.0`
- `16.0`

### `AirBnB_NYC.Neighbourhood` (categorical, 5 values)

- `Bronx`
- `Brooklyn`
- `Manhattan`
- `Queens`
- `Staten Island`

### `AirBnB_NYC.Number_of_Records` (categorical, 1 value)

- `1`

### `AirBnB_NYC.Property_Type` (categorical, 19 values)

- `Apartment`
- `Bed & Breakfast`
- `Boat`
- `Bungalow`
- `Cabin`
- `Camper/RV`
- `Castle`
- `Chalet`
- `Condominium`
- `Dorm`
- `House`
- `Hut`
- `Lighthouse`
- `Loft`
- `Other`
- `Tent`
- `Townhouse`
- `Treehouse`
- `Villa`

### `AirBnB_NYC.Review_Scores_Rating_bin` (categorical, 15 values)

- `20.0`
- `30.0`
- `40.0`
- `45.0`
- `50.0`
- `55.0`
- `60.0`
- `65.0`
- `70.0`
- `75.0`
- `80.0`
- `85.0`
- `90.0`
- `95.0`
- `100.0`

### `AirBnB_NYC.Room_Type` (categorical, 3 values)

- `Entire home/apt`
- `Private room`
- `Shared room`

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
| `AirBnB_NYC.Beds` | 0.0 | 16.0 | no | inclusive |
| `AirBnB_NYC.Number_Of_Reviews` | 0 | 257 | no | inclusive |
| `AirBnB_NYC.Number_of_Records` | 1 | 1 | no | inclusive |
| `AirBnB_NYC.Price` | 10 | 10000 | no | inclusive |
| `AirBnB_NYC.Review_Scores_Rating` | 20.0 | 100.0 | no | inclusive |
| `AirBnB_NYC.Review_Scores_Rating_bin` | 20.0 | 100.0 | no | inclusive |

## Date operators

Every DATE or TIMESTAMP column gets `date_operators`: `year`, `month`, `year_month`, and `date_between`. The generator sets them from the physical data type, so no date column is listed by hand.

## Business synonyms

No synonyms are defined for `airbnb`. Every column's `synonyms` list is empty. Synonyms are written by hand in `synonyms.yaml`. The generator reads that file, copies each entry into the matching column's `synonyms` list, and never creates the file. An entry that names a column missing from the physical mapping is an error.

## Physical names

- Database and schema: `airbnb`. Names are kept exactly as stored in DuckDB.
- Tables: `AirBnB_NYC`.
- The generator never renames, re-cases, or quotes names in the YAML. Quoting is the SQL generator's job.

## Data quality flags

Flagged values are reported, not removed or corrected.

None in this schema.

## Acceptance criteria

- The YAML covers all 13 columns in `airbnb`'s physical mapping, with the same names.
- Every categorical column has 30 or fewer values, and each value appears in its column in DuckDB.
- Boolean mappings only target stored values.
- Numeric rules use the operators listed above, and each includes its observed minimum and maximum.
- Flagged values appear in `flags` and remain in `values`.
- No data is altered.

## Open questions

Each has a recommended answer. Project-level questions (case matching, identifier list, synonym file location) are in [semantic_requirements.md](../semantic_requirements.md).

1. **Numeric columns with few values.** `AirBnB_NYC.Number_of_Records` has a single value, so it gets categorical values and numeric rules. Should it keep both? *Recommended: keep both, as for `Bills.Session`.*
