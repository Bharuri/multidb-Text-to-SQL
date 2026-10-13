# Design: Natural-language question over one database

## 1. Overview

A question goes through eleven stages. Each stage takes the previous stage's output, returns a `StageResult`, and the pipeline stops at the first failure. Two separate LLM calls are made: the planner at stage 4 and the SQL generator at stage 8. Neither call is retried. Every stage output is kept in a trace, so the UI can show what each step produced.

```
 1 Submit      request check (database, question, credentials flag)
 2 Resolve     registry lookup, load physical and semantic metadata
 3 Prompt      build prompt, measure size
 4 LLM         Bedrock call, raw reply
 5 Plan        strip fence, shape check, parse plan or cannot_answer
 6 Match       code matches names and values to metadata
 7 SQL prompt  build the SQL-generator prompt from the plan, metadata and business terms
 8 SQL gen     Bedrock call, one SELECT with bound parameters (builder path when enabled)
 9 Validate    structure and value rules
10 Execute     read-only run, row cap
11 Respond     final status and result
```

## 2. Modules

| Module | Responsibility |
|---|---|
| `config.yaml` | Registry of databases (name, physical YAML path, semantic YAML path, DB file), `default_row_limit`, `max_prompt_chars`, `threshold` for stored value lists, `planner_llm` and `sql_generator_llm` blocks (provider, model, region, `temperature`, `max_output_tokens`), and `capabilities` feature flags (`group_by`, `having`, `order_by`, `date_grains`, `calculations`, `joins`, `simple_sql_builder`) |
| `src/query/catalog.py` | Look up a database in the registry, load both YAML files, check their file paths agree, cache by modified time |
| `src/query/prompt.py` | Build the LLM prompt from the question, the physical context, stored values, year filter markers and plan rules. Measure its size |
| `src/query/llm.py` | One Bedrock Converse call (Nova 2 Lite, `us-east-1`). Credentials from the request, or the AWS credential chain if empty |
| `src/query/plan.py` | Remove a code fence, parse JSON, check keys, detect `cannot_answer` |
| `src/query/matcher.py` | Normalize text, match table, column, value, operator and aggregation. Return the matched plan with a `matches` list |
| `src/query/lookup.py` | Bounded, parameterized value lookup for columns with more than the threshold of distinct values |
| `src/query/sql_prompt.py` | Build the SQL-generator prompt from the plan, physical metadata and business terms |
| `src/query/sql_reply.py` | Strictly parse the SQL generator reply into `{sql, params}` |
| `src/query/sql_builder.py` | Optional deterministic `SELECT` builder for simple plans (off by default) |
| `src/query/validator.py` | Check the built SQL before it runs |
| `src/query/executor.py` | Run validated SQL on a read-only DuckDB connection, cap rows |
| `src/query/pipeline.py` | Run the eleven stages in order, collect the trace, write the log |
| `src/ui/app.py` | Sidebar credentials, Ask tab, Run SQL tab (manual read-only SQL through `executor.run_manual_sql`), one panel per stage |

## 3. Data shapes

```python
StageResult = {
    stage: str,                       # "submit", "resolve", ...
    status: "ok" | "failed" | "skipped",
    output: dict,                     # what the UI shows for this stage
    reason: str | None,
    duration_ms: int,
}

Plan        = {table, select, aggregation, filters, limit}
MatchedPlan = Plan + {matches: [{item, kind, result, source}]}
BuiltQuery  = {sql: str, params: list}
Response    = {status: "ok" | "rejected" | "cannot_answer" | "error",
               trace: [StageResult], result: rows | value | None, reasons: list[str]}
```

`status` in `Response` is derived from the trace:
- `ok`: every stage succeeds.
- `cannot_answer`: stage 5 receives a `cannot_answer` reply.
- `rejected`: stages 5 to 9 fail on the plan or the SQL.
- `error`: any other failure.

## 4. Stage details

| Stage | Input | Rule | Output |
|---|---|---|---|
| 1 Submit | Form values | Credentials are reduced to `credentials_supplied: true/false`. Empty question stops here with "Enter a question first." | `database`, `question`, `credentials_supplied` |
| 2 Resolve | Database name | Name must be in the registry. Both YAML files and the DB file must exist. Physical `file` must equal semantic `database_file`, and the schema key in both YAML files must equal the database name. A mismatch at either check stops with `error` | Registry entry, file paths, table and column counts |
| 3 Prompt | Question, physical metadata, stored values | Lists every table and column with its physical type and column class, stored values for columns with at most the threshold of distinct values, the numeric operators for numeric columns, and the date operators each date column supports. A prompt over `max_prompt_chars` stops with `error` | Prompt text, character count, limit |
| 4 LLM | Prompt, credentials | Single `ConverseStream` call. Each text chunk goes to the UI as it arrives. The full reply is kept at the end. Failure or rejected credentials stop with `error` | Raw reply, streamed chunk by chunk, then the full text |
| 5 Plan | Raw reply | One JSON object only, code fence allowed. Unknown keys or extra text reject. `cannot_answer` ends the flow | Parsed plan, or `cannot_answer` reason |
| 6 Match | Plan, metadata | Every item matched, or the plan is rejected with candidates (see section 5) | Matched plan and `matches` |
| 7 SQL prompt | Matched plan, metadata, business terms | Builds the SQL-generator prompt with the rules for one `SELECT`, bound parameters and the `LIMIT` rule | Prompt text |
| 8 SQL gen | SQL prompt, credentials | Single Bedrock call, streamed to the UI. The reply must parse as `{sql, params}`. When `simple_sql_builder` is on and the plan is not a calculation, the deterministic builder is used instead | SQL text, parameter list, `path` (`llm` or `builder`) |
| 9 Validate | SQL, parameters, metadata | Rules in section 6. Failure rejects | `valid`, `reasons` |
| 10 Execute | Validated SQL | Read-only connection. Row cap applied to row queries | Row count and rows, or the single value |
| 11 Respond | Trace | Final status and result | `Response` |

When a stage fails, its `StageResult` has `status: failed` and a reason. Later stages are recorded as `skipped`.

## 5. Matching

**Normalization:** lowercase, remove spaces and underscores, remove one trailing `s`. So `Sub_Category`, `subcategory` and `sub category` are equal, and `Phone` and `Phones` are equal.

**Columns and tables**, in this order: exact physical name, normalized physical name, normalized synonym. Each rung must give exactly one candidate.

A row query whose `select` is `["*"]` expands to every physical column of the table, in metadata order. `"*"` mixed with other names is rejected. The SQL that reaches the validator always lists explicit columns.

**Values on a column with a stored value list:** exact stored value, normalized stored value, normalized synonym. The LLM already chose a stored value, so this step only confirms it.

**Values on a column above the threshold:** one bounded lookup, `SELECT DISTINCT` with `LOWER(col) = LOWER(?)` and `LIMIT 2`. One row accepts. Zero or two rows reject.

**Operators:** must be one of the `numeric_operators`, or a date operator on a column whose metadata lists it in `date_operators`. Numeric values must parse as numbers. `BETWEEN` takes two numbers with low at most high.

**Date operators:**
- `year`: integer value. Builds `EXTRACT(YEAR FROM col) = ?`.
- `month`: integer from 1 to 12. Builds `EXTRACT(MONTH FROM col) = ?`.
- `year_month`: `[year, month]`. Builds both `EXTRACT` conditions.
- `date_between`: `[start, end]`, ISO dates, start not after end. Builds `col >= CAST(? AS DATE) AND col < CAST(? AS DATE)`. The second parameter is the day after `end`, computed in code. The end date is inclusive, and the SQL text has no literal.

Code does not read month names. The LLM turns "September" or "Sep" into `9`, so the mapping lives in one place (the LLM), and code only checks the integer.

The generator sets `date_operators` on every `DATE` or `TIMESTAMP` column from the physical data type. Nothing is written by hand per column.

**Aggregation:** `COUNT` takes `*` or any column. `SUM`, `AVG`, `MIN` and `MAX` need a numeric column.

Any item with no candidate, or with two candidates, rejects the plan. The reason names the item and lists the closest candidates. Nothing is guessed.

## 6. Validation rules

The validator runs on the built SQL and its parameters:

1. The statement is a single `SELECT`. Checked with DuckDB's `json_serialize_sql`, so no new dependency.
2. Every table and column is a physical name from the metadata.
3. No join.
4. Text comparisons use `LOWER(col) = LOWER(?)`. No `ILIKE`.
5. Every parameter value equals a stored value of its column (ignoring case), or is the single lookup result for columns above the threshold.
6. Numeric comparisons use a numeric column and an allowed operator. Date filters use a column that lists the operator in `date_operators`, and their SQL matches the forms in section 5.
7. Row queries and grouped aggregates have a `LIMIT`, taken from the plan or set to `default_row_limit`. A single-value aggregate (no dimensions) has no `LIMIT`.
8. No user value appears in the SQL text. The only literals allowed are the integer `LIMIT` and the date part keyword `year` or `month` inside `EXTRACT`. Both are fixed words chosen by the builder, not user input.

A failure rejects the request, and the SQL is not executed.

## 7. Security

- AWS credentials live only in session state, go straight to the Bedrock client, and are never logged, stored, put in the prompt, or passed to the executor. The trace records only `credentials_supplied`.
- The request cannot supply file paths or table names. Only the registry does.
- The executor opens DuckDB with `read_only=True`, so a write cannot run even if validation misses it.

## 8. Logging and trace

- Each stage writes one JSON log line to `logs/query.log` (logger `multidb.query`) as soon as it completes. The line is built from the same `StageResult` as the trace, so the log and the UI trace match. The log leaves out the prompt text and result rows. It keeps the prompt size (`chars`) and the row count.
- An unexpected exception inside a stage is caught. That stage becomes `failed` with a reason naming only the exception type. The traceback goes to the log, and later stages are `skipped`.
- The UI updates while the request runs: one placeholder per stage, filled in when that stage finishes. The LLM reply streams in at stage 4, and the plan appears as soon as stage 5 parses it.
- An empty question shows "Enter a question first." and sends no request.
- The Ask tab shows the final status, the final SQL with its parameters, and the result table (or the single value for an aggregate) directly below the Ask button. The Step plan tab shows one expandable panel per stage, with its `output` and `reason`. A failed query shows the first failed stage's reason in the Ask tab.
- Credentials never enter a `StageResult`.

## 9. Error handling

| Case | Status | Stage that reports it |
|---|---|---|
| Unknown database, missing file, mismatched paths | `error` | 2 |
| Prompt too large | `error` | 3 |
| Bad credentials, Bedrock failure | `error` | 4 |
| Reply is not a plan and not `cannot_answer` | `rejected` | 5 |
| `cannot_answer` | `cannot_answer` | 5 |
| Unmatched or ambiguous item | `rejected` | 6 |
| Validation failure | `rejected` | 8 |
| Database runtime error | `error` | 9 |

## Versioned JSON schemas

Both schemas are checked in code (`src/query/shapes.py`). Any other key, a wrong shape, or text outside the JSON object rejects the reply. A Markdown code fence around the whole reply may be removed first.

### Planner reply (`plan_schema_version` 1)

Either a plan or a cannot-answer reply. Keys not listed are rejected.

```json
{
  "table": "orders",
  "select": ["order_id"],
  "dimensions": ["region"],
  "metrics": [{"function": "SUM", "column": "sales", "alias": "total_sales"}],
  "filters": [{"column": "category", "operator": "=", "value": "Technology"}],
  "group_by": ["region"],
  "having": [{"metric": "total_sales", "operator": ">", "value": 1000}],
  "order_by": [{"ref": "total_sales", "direction": "DESC"}],
  "calculations": [{"type": "yoy_change", "metric": "total_sales", "date_column": "order_date", "grain": "year", "year": 2024}],
  "limit": 100
}
```

- `metrics[].function` is one of `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `SHARE`. `column` is `*` only for `COUNT` and `SHARE`.
- `SHARE` is the fraction of table rows matching every condition in its `where` list (0 to 1). It renders as `COUNT(*) FILTER (WHERE ...) / NULLIF(COUNT(*), ?)`, with the zero denominator bound as a parameter. `where` is allowed only on `SHARE`.
- Business definitions (for example `loss = Orders.Profit < 0`) are listed in the business YAML under `definitions` and shown to the planner, which turns a matching term into a `SHARE` `where`.
- `repeat_purchase` is an optional top-level key: `{"customer": "<column>", "date": "<date column>", "dimension": "<column or null>"}`, or null when absent. It answers first-to-second purchase questions (average days between a customer's first and second purchase by the first purchase's dimension value, and how many customers never returned). A purchase is a distinct (customer, date) pair. A customer counts once per dimension value on their first purchase date. When `repeat_purchase` is set, every other plan part must be empty or null.
- The repeat plan is rendered only by the fixed template in `sql_builder.py`. The validator accepts it only when the SQL equals that template for the matched plan. Date arithmetic in LLM-written SQL remains outside the allowed functions.
- `calculations[].type` is `yoy_change` or `percentage_growth`. `grain` is `year`, `quarter`, `month`, or `year_month`.
- Cannot-answer reply: `{"cannot_answer": true, "reason": "..."}`.

### SQL-generator reply (`sql_schema_version` 1)

```json
{"sql": "SELECT region, SUM(sales) AS total_sales FROM sample_superstore.sample_superstore.orders WHERE category = ? GROUP BY region", "params": ["Technology"]}
```

- Exactly the keys `sql` and `params`.
- `sql` uses `?` placeholders only. `params` lists one value per placeholder, in order.
- User values never appear inside `sql` text.