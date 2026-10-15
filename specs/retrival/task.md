
# Tasks — Generic Natural-Language-to-SQL Analytics

This task list is a fresh implementation plan based on `requirements.md`, `design.md`, and `evaluation_questions.md`. Complete tasks in order unless a dependency is explicitly documented. Each task should leave the application in a runnable state. Do not implement question-specific SQL templates or branches.

## Execution rules for the coding agent

1. Read all three specification files and the benchmark before editing code.
2. Inspect the current repository and report the actual starting state; do not assume files/modules exist.
3. Treat `requirements.md` as product behavior and `design.md` as the technical contract. If they conflict, stop and report the conflict instead of guessing.
4. Before implementation, create a capability matrix for all 30 questions and identify missing metadata/ambiguous business definitions.
5. Implement one task at a time. For each task: make focused changes, add tests, run tests and Ruff, summarize changed files and outcomes.
6. Never add a production branch keyed to a question, benchmark ID, exact question text, or dataset name to make an individual benchmark pass.
7. Use fake/mock LLM responses for routine tests. Live provider tests are optional and must not be required for the local test suite.
8. Do not silently simplify unsupported queries. Return a structured rejection or clarification.
9. Do not expose credentials, secrets, or stack traces in the UI/logs.
10. Keep Streamlit UI minimal and preserve the existing interface where possible; add trace panels rather than redesigning the application.

## Phase A — Repository and data foundation

| Task | Work | Acceptance criteria |
|---|---|---|
| A1. Repository audit | Inspect source tree, dependency pins, current UI, config, tests, current pipeline, and existing metadata. Produce a short gap report against all specifications before editing. | No assumptions about nonexistent modules; report lists reuse/replace decisions and conflicts. |
| A2. Workbook inventory | Inspect all sheets/columns in the four supplied workbooks. Profile row counts, types, nulls, duplicates, candidate keys, and likely row grain. | Inventory includes exact sheet and column names and flags uncertain keys/relationships. |
| A3. Deterministic ingestion | Build repeatable ingestion scripts to create one DuckDB file per workbook/domain, using explicit column mappings and type handling. | Re-running ingestion yields the same schema and row counts; source-to-physical mapping is documented. |
| A4. Data-quality/relationship profiling | Verify unique keys, one-to-many paths, title duplication risk, vote grain, and candidate join keys. Define only evidence-backed relationships. | Relationship file includes cardinality and enabled status; unsupported joins are absent or disabled. |
| A5. Metadata contract | Define strict physical, semantic, and relationship metadata schemas and sample metadata for all four datasets. | Metadata validation checks names/types/references; invalid files fail with actionable errors. |

## Phase B — Deterministic catalog and semantic resolution

| Task | Work | Acceptance criteria |
|---|---|---|
| B1. Registry and config | Implement typed configuration for databases, metadata paths, models, limits, capability flags, and privacy settings. | Unknown database, missing file, and mismatched metadata identity fail before LLM calls. |
| B2. Cached metadata catalog | Load/normalize metadata once per version fingerprint; invalidate when files change. | Repeated requests reuse cached catalog; modifying a metadata file reloads only the affected catalog. |
| B3. Relevant context retrieval | Resolve question terms to relevant tables, columns, metrics, date roles, semantics, and relationship paths. Never send every metadata file to each LLM. | Tests verify prompts contain relevant context only and never cross database boundaries. |
| B4. Semantic resolver | Resolve metric/dimension synonyms and detect undefined/ambiguous concepts. | Defined concepts map deterministically; undefined concepts request clarification or reject. |
| B5. Query observability models | Define `StageResult`, `Response`, structured failure categories, trace bounds, and logging policy. | Trace and log stage identity/status agree; secrets are excluded. |

## Phase C — Planner and plan contract

| Task | Work | Acceptance criteria |
|---|---|---|
| C1. Versioned plan schema | Implement strict schema/models for sources, joins, filters, stages/dependencies, expressions, windows, existence tests, set operations, final output, and clarification outcomes. | Valid example plans parse; unknown keys, raw SQL expressions, invalid enums, and broken references reject. |
| C2. Planner prompt | Build prompt from question, relevant metadata, semantic definitions, enabled capabilities, and schema instructions. Enforce configured prompt size. | Prompt is deterministic for identical inputs and contains no unrelated metadata or secrets. |
| C3. Planner client | Implement Planner LLM client with provider abstraction and streaming callback where supported. | Mocked and live adapter tests pass; exceptions become safe stage failures. |
| C4. Planner output parser | Parse one JSON object; validate schema version, exact structure, and outcomes. | Malformed JSON, extra prose, unknown keys, and invalid statuses are rejected. |
| C5. Deterministic plan validator | Resolve all identifiers, operators, metrics, relationships, stage dependencies, grain changes, aggregate rules, window requirements, and feature flags. | Plan is rejected before SQL generation if any reference/relationship/capability is ambiguous or unsupported. |
| C6. Join/grain safety | Validate join paths and cardinality; detect likely fan-out and require pre-aggregation or reject. | Tests cover one-to-many joins, many-to-many relationships, duplicate dimension rows, and safe pre-aggregation. |

## Phase D — SQL generation and validation

| Task | Work | Acceptance criteria |
|---|---|---|
| D1. SQL Generator prompt | Supply original question, validated plan, relevant physical/semantic metadata, relationship paths, DuckDB dialect, enabled features, and strict output schema. | Prompt cannot request unapproved sources; output contract is explicit. |
| D2. SQL Generator client | Implement separate model call and streaming trace. | Tests prove Planner and SQL Generator are separate calls with separate outputs/stages. |
| D3. Generated SQL parser | Parse strict JSON `{sql, params, assumptions}`; validate types, keys, placeholder count and ordering. | Malformed response and parameter mismatches reject before AST validation. |
| D4. Optional simple-query builder | Implement only a declared subset as deterministic fast path if useful. Otherwise keep shared identifier/parameter helpers only. | Fast path uses the same validator/executor and never claims to support advanced plan shapes it cannot represent. |
| D5. SQL AST validator foundation | Parse one query and recursively inspect all nodes, CTEs, subqueries, joins, set branches, table scans, columns, and functions. | Forbidden statements/external access/multiple statements are rejected even when nested in a CTE/subquery. |
| D6. Plan-to-SQL consistency | Check generated SQL against validated plan: source tables, join edges, filters, output aliases, metrics, grain, and calculations. | Query cannot add unplanned sources/metrics/filters or omit required outputs without rejection. |
| D7. Capability allowlists | Add explicit feature gates and AST checks for basic aggregation, CTEs, nested/correlated subqueries, `CASE`, conditional aggregates, windows, date math, ranking, percentiles, and set operations. | Each enabled feature has positive and negative tests; disabled syntax is rejected. |
| D8. Complexity and parameter safety | Enforce SQL length, CTE/nesting/join limits, safe parameter binding, row/result limits, and query timeout. | Limits are tested; no user data value is interpolated into SQL. |

## Phase E — Execution, pipeline, and UI

| Task | Work | Acceptance criteria |
|---|---|---|
| E1. Read-only executor | Execute only validated SQL against the selected DuckDB file with bound params and result/resource caps. | Executor cannot run writes; runtime errors are safe; parameters are passed separately. |
| E2. Pipeline orchestration | Implement all 14 logical stages from `design.md`, stop on first failure, mark later stages skipped, and return a trace. | Unit tests cover failure at each stage and confirm later stages are skipped. |
| E3. Structured logging | Emit one structured record per stage with duration/status/failure category and privacy-safe fields. | No credentials or stack traces in user-facing output; trace/log stage order matches. |
| E4. Streamlit integration | Wire database selector, question form, Ask/result view, Step Plan view, and Run SQL stub to pipeline. | Each stage updates visibly; Planner and SQL Generator responses are distinct; UI remains usable on failure. |
| E5. Error and clarification UX | Show a focused clarification when needed; distinguish cannot-answer, rejected plan/SQL, runtime error, and success. | No silent fallback to another database, definition, or simpler query. |

## Phase F — Testing and benchmark

| Task | Work | Acceptance criteria |
|---|---|---|
| F1. Unit tests | Test metadata, semantic resolution, plan schema, plan validator, generated SQL parsing, AST validation, parameter binding, executor, and trace. | Unit suite runs offline without live LLM credentials. |
| F2. Security tests | Test multiple statements, write statements, external file scans, unknown tables/columns, unapproved joins/functions, nested malicious SQL, CTE bypass attempts, parameter mismatch, and excessive complexity. | All unsafe cases reject before execution. |
| F3. Synthetic edge fixtures | Create small deterministic datasets for ties, nulls, duplicate order lines, no second purchase, zero denominator, missing periods, and one-to-many fan-out. | Expected results are manually verified and stored with tests. |
| F4. Benchmark manifest | Map each of the 30 questions to database, output grain, required metadata, capability tags, oracle method, and caveats. | All questions are mapped; questions needing unavailable fields or unsupported joins are flagged, not falsely marked pass/fail. |
| F5. Independent result oracles | Derive expected outputs using independent reference SQL/manual calculations. Record assumptions for source-dependent questions. | Tests compare exact values/rows where deterministic, with documented numeric tolerance/order rules. |
| F6. Benchmark runner | Run questions through mocked pipeline and, separately, live LLM pipeline; capture SQL, trace, results, timings, and failure category. | One report per run, no question-specific production code. |
| F7. Capability coverage | For each feature family, add focused tests independent of benchmark wording. | Capabilities are tested generically and held-out questions are preserved. |
| F8. Final regression and documentation | Run full tests, Ruff, ingestion reproducibility checks, and benchmark; update capability matrix and known limitations. | App launches; tests/lint pass or failures are documented; no claim of universal SQL support. |

## Required implementation order

A1 → A2 → A3 → A4 → A5 → B1–B5 → C1–C6 → D1–D8 → E1–E5 → F1–F8.

Do not start by prompting the coding model to “solve all 30 questions.” First establish the data model and plan schema, then enable SQL capabilities one family at a time. If a task reveals that a requirement cannot be met, stop and propose a specification update rather than silently changing behavior.

## Definition of done for every task

- Focused implementation is complete.
- Unit tests cover success, failure, and boundary cases.
- Relevant tests pass.
- Ruff passes on changed files (or pre-existing failures are identified separately).
- Application remains runnable.
- Changed files and decisions are summarized.
- No benchmark-specific production branch/template has been added.
