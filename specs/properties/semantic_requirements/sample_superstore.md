# Semantic Requirements: `sample_superstore`

## Purpose

Define the semantic mapping for the `sample_superstore` schema, so the Text-to-SQL layer can resolve user filters on its categorical, Boolean, and numeric columns. Only values stored in `databases/sample_superstore.duckdb` are mapped. The generic rules are in [semantic_requirements.md](../semantic_requirements.md).

## Inputs

- Database file: `databases/sample_superstore.duckdb`
- Physical mapping: `specs/properties/physical_mapping/sample_superstore.yaml`
- Manual synonyms: `specs/properties/semantic_mapping/synonyms.yaml` (not created yet; see Business synonyms).

## Output

- File: `specs/properties/semantic_mapping/sample_superstore.yaml`
- Top-level keys: `database`, `schema`, `database_file`, `schema_mapping`, `numeric_operators`, `columns`.
- Column keys: `database`, `schema`, `table`, `column`, `data_type`, `column_class`, then `values` (categorical and Boolean), `true_value` and `false_value` (Boolean; `false_value` is `null` when no stored value is false), `true_expressions` and `false_expressions` (Boolean), `numeric_rules` (numeric), `date_operators` (DATE and TIMESTAMP), `synonyms`, and `flags`.
- Output is deterministic: tables, columns, and values are sorted.

## Column classification

Each column gets one primary class, chosen in this order: boolean, categorical (30 or fewer distinct non-null values), numeric (numeric type, not an identifier, more than 30 values), other. A numeric column with 30 or fewer values is categorical and also gets numeric rules.

Result for `sample_superstore`: 10 categorical, 1 boolean, 2 numeric, 12 other.

| Table | Column | Data type | Class | Values | Numeric rules | Flags |
|---|---|---|---|---|---|---|
| `Orders` | `Category` | VARCHAR | categorical | 3 | - | - |
| `Orders` | `City` | VARCHAR | other | - | - | - |
| `Orders` | `Country_Region` | VARCHAR | categorical | 2 | - | - |
| `Orders` | `Customer_ID` | VARCHAR | other | - | - | - |
| `Orders` | `Customer_Name` | VARCHAR | other | - | - | - |
| `Orders` | `Discount` | DOUBLE | categorical | 12 | yes | - |
| `Orders` | `Order_Date` | TIMESTAMP | other | - | - | - |
| `Orders` | `Order_ID` | VARCHAR | other | - | - | - |
| `Orders` | `Postal_Code` | VARCHAR | other | - | - | - |
| `Orders` | `Product_ID` | VARCHAR | other | - | - | - |
| `Orders` | `Product_Name` | VARCHAR | other | - | - | - |
| `Orders` | `Profit` | DOUBLE | numeric | - | yes | - |
| `Orders` | `Quantity` | BIGINT | categorical | 14 | yes | - |
| `Orders` | `Region` | VARCHAR | categorical | 4 | - | - |
| `Orders` | `Row_ID` | BIGINT | other | - | - | - |
| `Orders` | `Sales` | DOUBLE | numeric | - | yes | - |
| `Orders` | `Segment` | VARCHAR | categorical | 3 | - | - |
| `Orders` | `Ship_Date` | TIMESTAMP | other | - | - | - |
| `Orders` | `Ship_Mode` | VARCHAR | categorical | 4 | - | - |
| `Orders` | `State_Province` | VARCHAR | other | - | - | - |
| `Orders` | `Sub_Category` | VARCHAR | categorical | 17 | - | - |
| `People` | `Region` | VARCHAR | categorical | 4 | - | - |
| `People` | `Regional_Manager` | VARCHAR | categorical | 4 | - | - |
| `Returns` | `Order_ID` | VARCHAR | other | - | - | - |
| `Returns` | `Returned` | VARCHAR | boolean | 1 | - | - |

## Value mappings

Only values that exist in the column are listed. Columns with more than 30 distinct values have no value mapping.

### `Orders.Category` (categorical, 3 values)

- `Furniture`
- `Office Supplies`
- `Technology`

### `Orders.Country_Region` (categorical, 2 values)

- `Canada`
- `United States`

### `Orders.Discount` (categorical, 12 values)

- `0.0`
- `0.1`
- `0.15`
- `0.2`
- `0.3`
- `0.32`
- `0.4`
- `0.45`
- `0.5`
- `0.6`
- `0.7`
- `0.8`

### `Orders.Quantity` (categorical, 14 values)

- `1`
- `2`
- `3`
- `4`
- `5`
- `6`
- `7`
- `8`
- `9`
- `10`
- `11`
- `12`
- `13`
- `14`

### `Orders.Region` (categorical, 4 values)

- `Central`
- `East`
- `South`
- `West`

### `Orders.Segment` (categorical, 3 values)

- `Consumer`
- `Corporate`
- `Home Office`

### `Orders.Ship_Mode` (categorical, 4 values)

- `First Class`
- `Same Day`
- `Second Class`
- `Standard Class`

### `Orders.Sub_Category` (categorical, 17 values)

- `Accessories`
- `Appliances`
- `Art`
- `Binders`
- `Bookcases`
- `Chairs`
- `Copiers`
- `Envelopes`
- `Fasteners`
- `Furnishings`
- `Labels`
- `Machines`
- `Paper`
- `Phones`
- `Storage`
- `Supplies`
- `Tables`

### `People.Region` (categorical, 4 values)

- `Central`
- `East`
- `South`
- `West`

### `People.Regional_Manager` (categorical, 4 values)

- `Chuck Magee`
- `Fred Suzuki`
- `Roxanne Rodriguez`
- `Sadie Pawthorne`

### `Returns.Returned` (boolean, 1 value)

- `Yes`

- True value: `Yes`
- False value: none (no stored value is false)
- True expressions: `returned`, `true`, `yes`, `y`, `1`, `on`
- False expressions: `not returned`, `false`, `no`, `n`, `0`, `off`

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
| `Orders.Discount` | 0.0 | 0.8 | no | inclusive |
| `Orders.Profit` | -6599.978000000001 | 8399.975999999999 | no | inclusive |
| `Orders.Quantity` | 1 | 14 | no | inclusive |
| `Orders.Sales` | 0.44399999999999995 | 22638.48 | no | inclusive |

## Date operators

Every DATE or TIMESTAMP column gets `date_operators`: `year`, `month`, `year_month`, and `date_between`. The generator sets them from the physical data type, so no date column is listed by hand.

## Business synonyms

No synonyms are defined for `sample_superstore`. Every column's `synonyms` list is empty. Synonyms are written by hand in `synonyms.yaml`. The generator reads that file, copies each entry into the matching column's `synonyms` list, and never creates the file. An entry that names a column missing from the physical mapping is an error.

## Physical names

- Database and schema: `sample_superstore`. Names are kept exactly as stored in DuckDB.
- Tables: `Orders`, `People`, `Returns`.
- The generator never renames, re-cases, or quotes names in the YAML. Quoting is the SQL generator's job.

## Data quality flags

Flagged values are reported, not removed or corrected.

None in this schema.

## Metric definitions

Format and lifecycle are in [requirement_retrival.md](../../retrival/requirement_retrival.md) §6.1 and [design.md](../../retrival/design.md) §3.3.

No metrics are authored for this database. Metrics are proposed from the schema and the question, then saved only when the user accepts them in the UI.

```yaml
metrics: []
```

## Acceptance criteria

- The YAML covers all 25 columns in `sample_superstore`'s physical mapping, with the same names.
- Every categorical column has 30 or fewer values, and each value appears in its column in DuckDB.
- Boolean mappings only target stored values.
- Numeric rules use the operators listed above, and each includes its observed minimum and maximum.
- Flagged values appear in `flags` and remain in `values`.
- No data is altered.

## Open questions

Each has a recommended answer. Project-level questions (case matching, identifier list, synonym file location) are in [semantic_requirements.md](../semantic_requirements.md).

1. **Boolean with one stored value.** `Returns.Returned` holds only `Yes`. "Returned", "yes", and "true" map to `Yes`, while "no" and "not returned" stay unresolved. Is that correct? *Recommended: yes. Unresolved is safer than mapping to a value that does not exist.*
2. **Numeric categorical columns.** `Orders.Discount` and `Orders.Quantity` have 30 or fewer values and also get numeric rules. Should exact values and numeric phrases both apply? *Recommended: yes.*
