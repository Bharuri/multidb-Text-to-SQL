# Tasks: Natural-language question over one database

Build order goes from the parts that need no LLM to the parts that do. Each task produces a stage output that the UI can show, so each step can be checked on its own.

## Step outputs shown in the UI

| Stage | Output shown |
|---|---|
| 1 Submit | Database, question, `credentials_supplied` |
| 2 Resolve | Registry entry, file paths, table and column counts |
| 3 Prompt | Prompt text, size in characters, limit |
| 4 LLM | Raw reply, as returned |
| 5 Plan | Parsed plan JSON, or the `cannot_answer` reason, or the rejection reason |
| 6 Match | Matched plan JSON with `matches`, or the unmatched item with candidates |
| 7 Build SQL | SQL text and parameter list |
| 8 Validate | `valid` and `reasons` |
| 9 Execute | Row count and rows as a table, or the single value |
| 10 Respond | Final status |

A failed stage shows its reason, and later stages show as `skipped`.

## UI layout

- **Ask tab:** question box and Ask button. Directly below them: the final status, the final SQL with its parameters, and the result (a table, or a single value for an aggregate). If the query did not run, the Ask tab shows the reason of the first failed stage.
- **Step plan tab:** one panel per stage (stages 1 to 10, including the execute output), with the LLM reply streaming into stage 4.
- **Run SQL tab:** stays as a stub.

## Tasks

| # | Task | Output | Done when |
|---|---|---|---|
| 0 | Build the UI first with stub stages. Write `app.py` (sidebar credentials, question box, Ask button, one placeholder per stage). Write `pipeline.py` with `StageResult` and ten stub functions that return fixed outputs in the real shape, and an `on_stage_done` callback. Add one stub that returns `failed` to test skipping | Streamlit app and stub pipeline | Clicking Ask fills all ten panels in order, the failed stub marks later panels `skipped`, and the UI code does not change when a stub is replaced |
| 1 | Create `config.yaml` with the registry, `default_row_limit`, `max_prompt_chars` and value-list threshold. Each registry entry points to its own YAML files and DB file | Config file | All four databases listed with their own file paths, unknown name returns an error, no entry points to another database's files |
| 2 | Extend the YAML generator. For every `DATE` or `TIMESTAMP` column, write `date_operators` (`year`, `month`, `year_month`, `date_between`) from the physical data type. Also write the Boolean true and false values, then regenerate | Updated YAML | `Orders.Order_Date` lists all four date operators, and no date column is hand-edited |
| 3 | Define `StageResult` and the `Response` shape in `pipeline.py` | Shared types | Each stage can return a result, and the response carries the trace |
| 4 | Build `catalog.py`: registry lookup, YAML load, path check, schema check, cache by modified time. Stage 2 output | Module | Unknown database errors, mismatched paths error, a YAML whose schema key differs from the database name errors, file change reloads |
| 5 | Build the matcher (`matcher.py`): normalization, table, column, value, operator and aggregation rungs. Check date operators against `date_operators`, and check the value shape for each (integer, `[year, month]`, `[start, end]`). Stage 6 output | Module | Fixed plans give the same matched plan on every run, unmatched and ambiguous items are reported, a month of 13 or a reversed range is rejected |
| 6 | Build `lookup.py` for columns above the threshold | Module | One row accepts, zero or two rows reject |
| 7 | Build `sql_builder.py`, including the four date forms in the design. Stage 7 output. Test with hand-written matched plans in `tests/unit/test_sql_builder.py` (`MATCHED_COUNT`, `MATCHED_ROWS`), before `plan.py` exists. The builder takes the schema name as a second argument and the catalog name as a third. DuckDB names the catalog after the database file, so a two-part name is ambiguous when the file and schema share a name (for example `sample_superstore`). | Module and test file | Tests in `test_sql_builder.py` pass: values are all parameters, aggregates have no `LIMIT`, `year_month` gives both `EXTRACT` conditions, `date_between` includes the end date |
| 8 | Build `validator.py` with the rules in the design. Stage 8 output | Module | Joins, unknown names, unstored values and value text in the SQL are rejected |
| 9 | Build `executor.py` on a read-only connection with a row cap. Stage 9 output | Module | A write statement fails at the executor |
| 10 | Build `plan.py`: fence removal, shape check, `cannot_answer`. Stage 5 output | Module | Extra keys and extra text are rejected |
| 11 | Build `prompt.py` with the size check. Stage 3 output | Module | Oversized prompt returns `error` and the LLM is not called |
| 12 | Build `llm.py` for the `ConverseStream` call with credentials from the request or the AWS chain. Pass each chunk to a callback as it arrives, and return the full reply at the end. Stage 4 output | Module | Chunks reach the callback before the stream ends, bad credentials return `error`, the keys appear nowhere |
| 13 | Build `pipeline.py`: run stages 1 to 10 in order, stop at first failure, mark later stages `skipped`, write one log line per stage as it completes, and send each finished stage to a callback for the UI | Module | Trace has one entry per stage, log matches trace, the callback fires once per stage in order |
| 14 | Connect `app.py` to the real stages (replacing the stubs from task 0). Show the LLM text as chunks arrive, and keep the Run SQL tab | Streamlit app | One click makes one request, empty question shows "Enter a question first.", the JSON appears while it is generated, each stage's output appears before the next stage starts |
| 15 | Unit tests for matcher, SQL builder, validator and plan check using fixed plans (no LLM) | Test suite | All pass, repeat runs give identical output |
| 16 | Trace tests: success path, and a failure at each stage | Test suite | Success shows all stages, failure shows `skipped` after the failing stage, no credential in any trace |
| 17 | End-to-end tests with a mocked LLM reply covering the acceptance criteria | Test suite | All acceptance criteria pass |
| 18 | Live Bedrock test on the example questions, reviewed through the trace | Test report | Typo question gives the same result as the correct word, every stage output is as expected |

## Suggested order

0 (UI with stubs), then 1 to 3, then 4 to 9 (no LLM), then 10 and 11, then 12 and 13, then replace the UI stubs with real results, then 15 to 18.

Each stage is first a stub that returns a fixed `StageResult`. Replace each stub with real code only after its task's "Done when" holds, so the UI keeps working at every step.

Testing the deterministic stages with fixed plans before the live LLM test makes it clear whether a wrong answer came from the code or from the LLM's plan.
