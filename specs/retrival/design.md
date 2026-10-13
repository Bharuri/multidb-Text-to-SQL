

Ran a command, shared files
Ran a command, shared files
I wrote the test file tests/unit/test_sql_builder.py and updated task 7 in tasks.md to point to it.

The file holds the two hand-written matched plans, MATCHED_COUNT (the furniture count for 2025 Sep) and MATCHED_ROWS (phone orders in 2025, with a limit). It also has five tests:

The count query has the expected SQL, the bound parameters ['Furniture', 2025, 9], and no LIMIT.
The row query lists its columns, has the expected parameters, and LIMIT 1000.
No user value appears in the SQL text.
date_between includes the end date.
A null aggregation with listed columns builds a column list and uses the given limit.
The tests import build_sql from query.sql_builder, which doesn't exist yet, so they will fail until task 7 is built. Also, the matched plan doesn't carry the database name, so build_sql takes the schema as a second argument and the catalog (the DuckDB file name without extension) as a third. A two-part name is ambiguous in DuckDB when the file and schema share a name, so the SQL uses `"catalog"."schema"."table"`. I noted that in task 7.


Test sql builder
PY 

Tasks
Document · MD 
what is the output of task 7

Task 7's output is the SQL text and its parameter list, as a StageResult for stage 7.

For the furniture count, the output is:

json
{
  "stage": "build_sql",
  "status": "ok",
  "output": {
    "sql": "SELECT COUNT(*) FROM \"sample_superstore\".\"sample_superstore\".\"Orders\" WHERE LOWER(\"Category\") = LOWER(?) AND EXTRACT(YEAR FROM \"Order_Date\") = ? AND EXTRACT(MONTH FROM \"Order_Date\") = ?",
    "params": ["Furniture", 2025, 9]
  },
  "reason": null,
  "duration_ms": 1
}
Two things to note:

The SQL has placeholders, not values. The user's words never appear in the text. The values are in params, and DuckDB binds them when the query runs in stage 9.
The SQL is not checked yet. Checking it is stage 8, the validator, so stage 7 only builds it.
If the builder fails, for example because a plan has an operator it doesn't know, the output is status: failed with a reason, and the later stages are skipped.

1791831098128_image.png
what step it will get updated in task\


Thought for 4s
Thought for 4s
This screenshot is the output of stage 2, Resolve, which is the step where the registry entry is loaded. The output shows a bug: database is airbnb, but all three file paths point to sample_superstore. The tables and columns counts are probably from the wrong file too.

The current check only compares the physical and semantic files with each other. Both files are for sample_superstore, so they agree, and the check passes. Nothing checks that the files match the database name. That gap is why the bug got through.

The fix belongs in two tasks:

Task 1 (config.yaml): the airbnb entry should point to the airbnb YAML files and databases/airbnb.duckdb.
Task 4 (catalog.py): add a check that the schema key inside each YAML file equals the database name, so a wrong entry fails at stage 2 instead of later.
I'll update both tasks and the stage 2 rule in the design.


Ran a command, shared files
Ran a command, shared files
The screenshot is the output of stage 2, Resolve. It shows a bug: the database is airbnb, but all three file paths point to sample_superstore. The tables and columns counts are probably from the wrong file too.

The existing check compares only the physical and semantic files with each other. Both belong to sample_superstore, so the check passes, and nothing confirms that the files match the database name.

The fix goes in two tasks:

Task 1 (config.yaml): the airbnb entry must point to its own YAML files and databases/airbnb.duckdb.
Task 4 (catalog.py): check that the schema key in each YAML file equals the database name. A mismatch then fails at stage 2, not later.
I updated both tasks in tasks.md, and added the same schema-key rule to stage 2 in design.md.


Tasks
Document · MD 

Design
Document · MD 





Claude is AI and can make mistakes. Please double-check responses.


Design · MD
# Design: Natural-language question over one database
 
## 1. Overview
 
A question goes through ten stages. Each stage takes the previous stage's output, returns a `StageResult`, and the pipeline stops at the first failure. The LLM is called once, at stage 4. Every stage output is kept in a trace, so the UI can show what each step produced.
 
```
 1 Submit      request check (database, question, credentials flag)
 2 Resolve     registry lookup, load physical and semantic metadata
 3 Prompt      build prompt, measure size
 4 LLM         Bedrock call, raw reply
 5 Plan        strip fence, shape check, parse plan or cannot_answer
 6 Match       code matches names and values to metadata
 7 Build SQL   one SELECT with bound parameters
 8 Validate    structure and value rules
 9 Execute     read-only run, row cap
10 Respond     final status and result
```
 
## 2. Modules
 
| Module | Responsibility |
|---|---|
| `config.yaml` | Registry of databases (name, physical YAML path, semantic YAML path, DB file), `default_row_limit`, `max_prompt_chars`, `threshold` for stored value lists, and the `llm` block (provider, model, region, `temperature`, `max_output_tokens`) |
| `src/query/catalog.py` | Look up a database in the registry, load both YAML files, check their file paths agree, cache by modified time |
| `src/query/prompt.py` | Build the LLM prompt from the question, the physical context, stored values, year filter markers and plan rules. Measure its size |
| `src/query/llm.py` | One Bedrock Converse call (Nova 2 Lite, `us-east-1`). Credentials from the request, or the AWS credential chain if empty |
| `src/query/plan.py` | Remove a code fence, parse JSON, check keys, detect `cannot_answer` |
| `src/query/matcher.py` | Normalize text, match table, column, value, operator and aggregation. Return the matched plan with a `matches` list |
| `src/query/lookup.py` | Bounded, parameterized value lookup for columns with more than the threshold of distinct values |
| `src/query/sql_builder.py` | Build one DuckDB `SELECT` and its parameter list from a matched plan |
| `src/query/validator.py` | Check the built SQL before it runs |
| `src/query/executor.py` | Run validated SQL on a read-only DuckDB connection, cap rows |
| `src/query/pipeline.py` | Run the ten stages in order, collect the trace, write the log |
| `src/ui/app.py` | Sidebar credentials, Ask tab, Run SQL tab, one panel per stage |
 
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
 
`status` in `Response` is derived from the trace: `ok` when every stage succeeds, `cannot_answer` when stage 5 receives a `cannot_answer` reply, `rejected` when stages 5 to 8 fail on the plan, and `error` for any other failure.
 
## 4. Stage details
 
| Stage | Input | Rule | Output |
|---|---|---|---|
| 1 Submit | Form values | Credentials are reduced to `credentials_supplied: true/false`. Empty question stops here with "Enter a question first." | `database`, `question`, `credentials_supplied` |
| 2 Resolve | Database name | Name must be in the registry. Both YAML files and the DB file must exist. Physical `file` must equal semantic `database_file`, and the schema key in both YAML files must equal the database name. A mismatch at either check stops with `error` | Registry entry, file paths, table and column counts |
| 3 Prompt | Question, physical metadata, stored values | Lists every table and column with its physical type and column class, stored values for columns with at most the threshold of distinct values, the numeric operators for numeric columns, and the date operators each date column supports. Prompt over `max_prompt_chars` stops with `error` | Prompt text, character count, limit |
| 4 LLM | Prompt, credentials | Single `ConverseStream` call. Each text chunk is passed to the UI as it arrives. The full reply is kept at the end. Failure or rejected credentials stop with `error` | Raw reply, streamed chunk by chunk, then the full text |
| 5 Plan | Raw reply | One JSON object only, code fence allowed. Unknown keys or extra text reject. `cannot_answer` ends the flow | Parsed plan, or `cannot_answer` reason |
| 6 Match | Plan, metadata | Every item matched, or the plan is rejected with candidates (see section 5) | Matched plan and `matches` |
| 7 Build SQL | Matched plan | One `SELECT`. Values bound as parameters. No `LIMIT` on aggregates | SQL text, parameter list |
| 8 Validate | SQL, parameters, metadata | Rules in section 6. Failure rejects | `valid`, `reasons` |
| 9 Execute | Validated SQL | Read-only connection. Row cap applied to row queries | Row count and rows, or the single value |
| 10 Respond | Trace | Final status and result | `Response` |
 
When a stage fails, its `StageResult` has `status: failed` and a reason. Later stages are recorded as `skipped`.
 
## 5. Matching
 
Normalization: lowercase, remove spaces and underscores, remove one trailing `s`. So `Sub_Category`, `subcategory` and `sub category` are equal, and `Phone` and `Phones` are equal.
 
Columns and tables, in this order: exact physical name, normalized physical name, normalized synonym. Each rung must give exactly one candidate.

A row query whose `select` is `["*"]` expands to every physical column of the table, in metadata order. `"*"` mixed with other names is rejected. The SQL that reaches the validator always lists explicit columns.
 
Values on a column with a stored value list: exact stored value, normalized stored value, normalized synonym. The LLM already chose a stored value, so this step only confirms it.
 
Values on a column above the threshold: one bounded lookup, `SELECT DISTINCT` with `LOWER(col) = LOWER(?)` and `LIMIT 2`. One row accepts, zero or two rows reject.
 
Operators: must be one of the `numeric_operators`, or a date operator on a column whose metadata lists it in `date_operators`. Numeric values must parse as numbers. `BETWEEN` takes two numbers with low at most high.
 
Date operators:
- `year`: integer value. Builds `EXTRACT(YEAR FROM col) = ?`.
- `month`: integer from 1 to 12. Builds `EXTRACT(MONTH FROM col) = ?`.
- `year_month`: `[year, month]`. Builds both `EXTRACT` conditions.
- `date_between`: `[start, end]`, ISO dates, start not after end. Builds `col >= CAST(? AS DATE) AND col < CAST(? AS DATE)`. The second parameter is the day after `end`, computed in code, so the end date is inclusive and the SQL text has no literal.
Code does not read month names. The LLM turns "September" or "Sep" into `9`, so there is one mapping, in the LLM, and code only checks the integer.
 
The generator sets `date_operators` on every `DATE` or `TIMESTAMP` column from the physical data type. Nothing is written by hand per column.
 
Aggregation: `COUNT` takes `*` or any column. `SUM`, `AVG`, `MIN` and `MAX` need a numeric column.
 
Any item with no candidate or two candidates rejects the plan. The reason names the item and lists the closest candidates. Nothing is guessed.
 
## 6. Validation rules
 
The validator runs on the built SQL and its parameters:
 
1. The statement is a single `SELECT`. Checked with DuckDB's `json_serialize_sql`, so no new dependency.
2. Every table and column is a physical name from the metadata.
3. No join.
4. Text comparisons use `LOWER(col) = LOWER(?)`. No `ILIKE`.
5. Every parameter value equals a stored value of its column (ignoring case), or is the single lookup result for columns above the threshold.
6. Numeric comparisons use a numeric column and an allowed operator. Date filters use a column that lists the operator in `date_operators`, and their SQL matches the forms in section 5.
7. Row queries have a `LIMIT`. Aggregate queries do not.
8. No user value appears in the SQL text. The only literals allowed are the integer `LIMIT` and the date part keyword `year` or `month` inside `EXTRACT`. Both are fixed words chosen by the builder, not user input.
A failure rejects the request, and the SQL is not executed.
 
## 7. Security
 
- AWS credentials live only in session state, go straight to the Bedrock client, and are never logged, stored, put in the prompt, or passed to the executor. The trace records only `credentials_supplied`.
- The request cannot supply file paths or table names. Only the registry does.
- The executor opens DuckDB with `read_only=True`, so a write cannot run even if validation misses it.
## 8. Logging and trace
 
- Each stage writes one JSON log line to `logs/query.log` (logger `multidb.query`) as soon as it completes, built from the same `StageResult` as the trace, so the log and the UI trace match. The log leaves out the prompt text and result rows. It keeps the prompt size (`chars`) and the row count.
- An unexpected exception inside a stage is caught: that stage becomes `failed` with a reason naming only the exception type, the traceback goes to the log, and later stages are `skipped`.
- The UI updates while the request runs: one placeholder per stage, filled in when that stage finishes. The LLM reply is shown as it streams in (stage 4), and the plan appears as soon as stage 5 parses it.
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
 


Claude finished the response
