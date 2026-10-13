# Tasks: Natural-language analytics over one database

Build order: the UI and shared contracts first, then deterministic metadata and plan validation, then the two LLM calls and SQL validation. Keep the existing Streamlit layout and add visible stage panels rather than redesigning the UI.

## Stage outputs shown in the UI

| Stage | Output shown |
|---|---|
| 1 Submit | Database, question, `credentials_supplied` |
| 2 Resolve | Registry entry, file paths, table/column counts, business metadata status |
| 3 Planner Prompt | Prompt text or safe preview, character count, configured limit |
| 4 Planner LLM | Raw planner reply, streamed as it arrives |
| 5 Parse Plan | Parsed plan JSON, `cannot_answer` reason, or rejection reason |
| 6 Match and Validate Plan | Matched plan and match details, or unmatched/ambiguous item with candidates |
| 7 SQL Prompt | SQL-generator prompt preview, character count, configured limit |
| 8 SQL Generation | Raw SQL-generator reply, streamed as it arrives; parsed SQL and parameters |
| 9 Validate SQL | `valid` and validation reasons |
| 10 Execute | Row count and result rows/table, or safe runtime error |
| 11 Respond | Final status, final SQL/parameters when available, result summary |

A failed stage shows its reason, and later stages are skipped. The two LLM calls have separate trace entries.

## UI layout

- **Ask tab:** keep the question box and Ask button. Directly below them, show the final status, the final SQL with parameters when available, and the results. If the query does not run, show the first failed stage's reason.
- **Step plan tab:** keep the existing step-panel approach. Show one panel for every stage above. Show planner and SQL-generator output separately, and stream each model response into its own panel.
- **Run SQL tab:** a manual SQL box. It runs one typed read-only SELECT or WITH statement on a read-only connection to the selected database, capped at `default_row_limit`. It does not go through the planner or validator, and the read-only connection blocks writes.
- Do not change navigation, styling, or the overall app layout.

## Tasks

| # | Task | Output | Done when |
|---|---|---|---|
| 0 | Build or retain the UI first with stub stages. Create `app.py` and `pipeline.py` with the shared `StageResult`, stage callbacks, placeholders, and one failure stub. | Streamlit app and stub pipeline | Ask fills panels in order; a failure marks later panels skipped; replacing a stub does not require UI layout changes. |
| 1 | Update `config.yaml` with the database registry, row and prompt limits, the high-cardinality threshold, planner and SQL-generator LLM settings, and feature flags for supported query capabilities. | Config file | Each registered database points to its own physical, semantic/business metadata and DuckDB file; unknown names error without fallback. |
| 2 | Extend the YAML generator so every physical `DATE`/`TIMESTAMP` column receives supported date operators from its physical type. Include Boolean `true`/`false` values and regenerate metadata. | Updated YAML | All date columns receive generated operators; no date column is hand-edited. |
| 3 | Define the shared request, `StageResult`, `Response`, `Plan`, `MatchedPlan`, and `GeneratedQuery` shapes. Document the versioned plan and SQL-generation JSON schemas. | Shared types/schema | Both LLM replies are parsed against strict schemas; the trace can represent every stage and streaming update. |
| 4 | Build or extend `catalog.py`: registry lookup, path checks, YAML load, database/schema-key checks, cache by modification time, and optional business semantics/relationship metadata loading. | Module | Unknown database, missing files, mismatched paths/schema keys, and malformed metadata fail at Resolve. |
| 5 | Build `matcher.py` for table, column, dimension, metric, filter, operator, aggregation, date-grain, and business-concept matching. Use exact, then normalized, then synonym matching, and reject ambiguity. | Module | Fixed plans match deterministically; unmatched and ambiguous items include candidates; invalid numeric/date values reject. |
| 6 | Build or extend `lookup.py` for high-cardinality columns. | Module | One stored-value match accepts; zero or multiple matches reject; all lookup values are parameterized and the lookup is bounded. |
| 7 | Define the supported query-plan schema and build `plan.py` to remove an optional code fence, parse JSON, reject extra keys or text, and detect `cannot_answer`. Include dimensions, multiple metrics, filters, `group_by`, `having`, `order_by`, `calculations`, and `limit`. | Module and schema tests | Valid plans parse; malformed JSON, extra keys or text, invalid shapes, and unsupported calculations reject. |
| 8 | Build `prompt.py` for the planner. Include the selected database's metadata, stored low-cardinality values, date/numeric operators, business definitions, supported query features, and the strict JSON contract. Enforce `max_prompt_chars`. | Planner prompt module | The prompt includes only selected-database context; an oversize prompt stops before the planner call; the prompt stage is visible. |
| 9 | Build `llm.py` planner client using Bedrock `ConverseStream` (or the configured supported API). Pass chunks to a callback and return the full reply. | Planner LLM module | Chunks reach the callback before completion; failures return safe errors; credentials are never logged. |
| 10 | Build `business_semantics.py` or an equivalent catalog layer for metric definitions, synonyms, dimensions, approved calculations, and optional relationship metadata. Do not try to encode every question as a rule. | Business semantics layer and examples | Configured definitions are authoritative; ambiguous or undefined concepts are not silently guessed; metadata is scoped to the selected database. |
| 11 | Build the deterministic plan validator/matcher integration. Validate supported aggregation types, dimension references, grouping consistency, filter shapes, `HAVING`, ordering, date grain, calculation inputs, and configured feature flags. | Validated matched plan | Unsupported or ambiguous operations reject before SQL generation; the same plan and metadata produce the same result. |
| 12 | Build `sql_prompt.py` (or extend `prompt.py`) for the SQL-generator LLM. Provide the original question, matched plan, physical schema, relevant business definitions, supported dialect/features, and the strict JSON output schema. | SQL prompt module | The prompt tells the model to use only approved schema and plan elements, and to return SQL with placeholders plus a separate parameter list; the size limit is enforced. |
| 13 | Build the SQL-generator LLM call in `llm.py`, separate from the planner call. Stream chunks to the stage-8 callback. Parse a strict response such as `{"sql":"... ? ...", "params":[...]}`. | SQL generation module | The call is distinct and visible; malformed output fails; there is no automatic retry; the raw reply and parsed output are traced safely. |
| 14 | Implement `sql_builder.py` as an optional deterministic path for supported simple plans, or as shared helpers for quoting identifiers and binding values. Do not require it to implement every advanced business calculation. | SQL builder/helpers | Simple queries can use a deterministic path; values remain parameters; the trace says which generation path was used. |
| 15 | Build `validator.py` using SQL parsing (AST or DuckDB parsing) plus an explicit allowlist. Validate a single read-only query, selected-database tables and columns, approved joins, functions, grouping, CTE and window-function use, placeholders, parameters, limits, and consistency with the matched plan. | SQL validator | Unsafe, multiple, or write statements, unknown identifiers, unapproved joins or functions, interpolated values, invalid grouping, and parameter mismatches reject before execution. |
| 16 | Build `executor.py` using a read-only DuckDB connection and enforce row and result limits. | Executor | Write statements cannot execute; only validated SQL runs; runtime errors are safe and do not expose stack traces to users. |
| 17 | Build the pipeline with the stages in the table above. Stop at the first failure, mark later stages skipped, log one structured record per stage, and invoke callbacks in order. | Pipeline module | The trace and logs agree; callbacks fire in order; planner and SQL-generator streams are separate; credentials never enter the trace or logs. |
| 18 | Connect `app.py` to the real stages without redesigning the UI. | Streamlit app | One click makes one request; an empty question shows “Enter a question first.”; each stage appears before the next begins; both LLM responses stream into their panels. |
| 19 | Write unit tests for plan parsing, matching, business semantics, SQL-generation parsing, the SQL builder, and the validator, using fixed plans and replies. No live LLM is required. | Unit test suite | Tests cover rows, aggregates, grouped queries, multiple metrics, `HAVING`, sorting, dates, YoY, ambiguity, unsafe SQL, and parameters. |
| 20 | Write trace tests for success and failure at every stage. | Trace tests | All stages appear on success; stages after a failure are skipped; the trace and log contain no credentials; each LLM call is separately visible. |
| 21 | Write end-to-end tests with mocked planner and SQL-generator replies. | E2E test suite | Acceptance criteria pass without Bedrock; wrong or ambiguous plans and unsafe SQL never execute. |
| 22 | Run live Bedrock tests with reviewed examples, including grouped sales, multiple dimensions, yearly sales, YoY growth, and unsupported or ambiguous business concepts. | Test report | The trace shows each stage; answers are checked against known expected DuckDB results; no unapproved SQL executes. |

## Suggested order

`0 → 1–3 → 4–7 → 8–11 → 12–13 → 14–17 → 18 → 19–22`

Build deterministic plan validation and SQL validation before relying on live LLM output. Test both LLMs with fixed replies first. Allow SQL-generator freedom only for query constructs the validator explicitly supports. Keep the current UI structure, and add stage panels rather than redesigning the app.
