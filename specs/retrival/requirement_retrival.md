# Requirement: Answer a natural-language question over one database

## Purpose

Answer a plain-text question about one selected database. The answer is either rows or a single aggregate value (count, sum, average, minimum or maximum).

## Input

- `database`: a name from the registry in `config.yaml`
- `question`: plain text
- AWS credentials for the Bedrock call (or the standard AWS credential chain if empty)

## Requirements

1. **Database resolution.** Look up `database` in the registry. An unknown name or a missing file returns `status: error`, with no fallback to another database.
2. **LLM plan.** The LLM is called once. It receives the question, the physical metadata of the database, the stored values of categorical and Boolean columns, and the plan rules. It returns one JSON query plan, never SQL.
3. **Plan format.**

   ```json
   {
     "table": "<physical table>",
     "select": ["<column>"],
     "aggregation": null,
     "filters": [{"column": "<column>", "operator": "<op>", "value": "<value>"}],
     "limit": 1000
   }
   ```

   If the question cannot be answered, the LLM returns `{"cannot_answer": true, "reason": "..."}`.
4. **How the LLM maps a question to a plan.**

   | The question asks for | Plan |
   |---|---|
   | Rows ("get all", "show", "list") | `select` has columns or `["*"]`, `aggregation: null` |
   | A count ("how many") | `aggregation: COUNT` on `*` or a column, `select: []` |
   | A total, average, minimum or maximum | `aggregation: SUM`, `AVG`, `MIN` or `MAX` on a numeric column, `select: []` |
   | A category or Boolean condition | filter with `=` and one stored value |
   | A numeric condition | filter with one of `>=`, `>`, `<=`, `<`, `=`, `BETWEEN` and a number (`[low, high]` for `BETWEEN`) |
   | A year condition ("in 2025") | filter with `year` on a date column, value is an integer, e.g. `2025` |
   | A month condition ("in September") | filter with `month` on a date column, value is an integer from 1 to 12 |
   | A year and month condition ("in 2025 Sep") | filter with `year_month` on a date column, value is `[2025, 9]` |
   | A date range ("between 1 Jan 2025 and 31 Mar 2025") | filter with `date_between` on a date column, value is `["2025-01-01", "2025-03-31"]` (both dates inclusive) |
   | Date operators on a column | Only the date operators in that column's metadata entry (`date_operators`). A date column gets them from its data type, not from a hand-written list |
   | A condition on a column with more than 30 distinct values | filter with `=` and the user's word as written |
   | `GROUP BY`, `ORDER BY`, a join, or something not in the metadata | `cannot_answer` |

5. **Shape check.** The reply must be one JSON object with only the plan keys. Any other reply is rejected.
6. **Matching (code, no LLM).** Every table, column, value, operator and aggregation is checked against the metadata and replaced with its stored name or value, using exact, then normalized, then synonym matching. An item with no match, or two matches, rejects the plan with the closest candidates. Nothing is guessed.
7. **SQL build.** Code builds one DuckDB `SELECT` from the matched plan. Every value is a bound parameter. Text comparisons use `LOWER(col) = LOWER(?)`, and years use `EXTRACT(YEAR FROM col) = ?`. Row queries get a `LIMIT`, and aggregate queries do not.
8. **Validation.** The validator checks the SQL: single `SELECT`, mapped tables and columns only, no join, stored values only, and no user value in the SQL text. A failure rejects the request.
9. **Execution.** Run the validated SQL on a read-only connection. Return at most the row limit.
10. **No retry.** A plan that fails matching or validation is rejected. The LLM is not called again.
11. **Logging.** Log the question, the raw LLM reply, every match, the built SQL, the validator result and the row count. Never log credentials.

## Output

The frontend shows each stage's output as soon as that stage finishes, and shows the LLM reply as it is generated. A user does not have to wait for the whole request to see the plan JSON.


| Status | Returned |
|---|---|
| `ok` | LLM plan, matched plan, SQL with parameters, and the rows or the single aggregate value |
| `rejected` | Reasons, naming the item and the closest candidates |
| `cannot_answer` | The LLM's reason |
| `error` | A message only, with no stack trace |

## Acceptance criteria

- A row question returns rows with a `LIMIT`.
- A "how many", total or average question returns one value.
- A typo or related word maps to the stored value and gives the same result as the correct word.
- A value with no stored match is rejected, not guessed.
- A question needing `GROUP BY`, `ORDER BY` or a join returns `cannot_answer`.
- A plan with an unknown table, column or key is rejected, and nothing is built or run.
- The same plan and metadata always give the same matched plan.
- No user value appears in the SQL text, and no credential appears anywhere in the output or logs.
- "orders in 2025" filters on the year only, "orders in 2025 Sep" filters on year and month, and both use the same date column with the same rules.
- A month value outside 1 to 12, or a date range whose start is after its end, is rejected.
- Any date column in the metadata gets date operators, including columns not named in this spec.