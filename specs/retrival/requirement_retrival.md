
# Requirements — Generic Natural-Language-to-SQL Analytics

Version: 2.0  
Status: implementation specification  
Database engine: DuckDB  
Application: local Streamlit application  
LLM architecture: Planner LLM + SQL Generator LLM

## 1. Objective

Build a generic, metadata-driven natural-language analytics application that answers questions against one explicitly selected DuckDB database. It must support straightforward queries and advanced analytical SQL without hard-coded branches for individual questions.

The application is evaluated against `evaluation_questions.md`, which contains 30 representative questions across Sample - Superstore, 113th Congress, Airbnb NYC, and Netflix titles. At least 18 questions target Sample - Superstore.

The benchmark is a capability test, not a list of templates to implement. The production code must not contain question-specific SQL templates, question-ID conditionals, or dataset-name special cases. Dataset-specific differences belong in ingestion mappings and metadata.

## 2. Product scope

### In scope
- Select one registered database per request.
- Ask a question in natural language.
- Use two separate LLM responsibilities:
  1. Planner LLM: convert the question into a validated, structured analytical plan; never write SQL.
  2. SQL Generator LLM: translate the validated plan and relevant metadata into parameterized DuckDB SQL; never execute SQL.
- Deterministic metadata catalog, semantic definitions, relationship registry, plan validation, SQL parsing/validation, and safe execution.
- Simple aggregations and filters plus advanced SQL: CTEs, multi-stage queries, joins, nested/correlated subqueries, `EXISTS`/`NOT EXISTS`, `CASE`, conditional aggregation, window functions, date arithmetic, set operations, ranking, cohort analysis, rolling/cumulative metrics, percentiles, and top-N-per-group.
- Visible, stage-by-stage Streamlit trace, including both LLM calls.
- Reproducible dataset ingestion and benchmark evaluation.
- Tests using mocked LLM responses; live LLM tests are optional and separately marked.
- Explainable rejection when the question is unsupported or materially ambiguous.

### Out of scope for the initial release
- Arbitrary SQL execution entered by the user through natural language or plan fields.
- Cross-database joins in a single question.
- Database writes, DDL, stored procedures, external file/network access from generated SQL.
- Automatic schema discovery from arbitrary production databases beyond the configured DuckDB metadata.
- Query execution against multiple databases in one request.
- Automatic business-definition invention. A metric proposal may be generated for an undefined term, but it is never saved or used in SQL until the user accepts it in the UI (§6.1).
- Guaranteed correct interpretation when source data lacks required fields or history.
- Automatic retry loops that silently change the plan or SQL.

## 3. Core design principles

1. **Generic, not memorized:** support reusable SQL capabilities, not individual benchmark questions.
2. **Metadata-driven:** physical schema, semantic definitions, relationships, synonyms, and capability flags are configuration/data, not hard-coded logic.
3. **Deterministic guardrails:** LLM output is untrusted until parsed and validated.
4. **Plan before SQL:** the Planner returns intent and computation stages, never SQL text.
5. **SQL AST validation:** validate the entire parsed query, recursively including CTEs and subqueries; string checks alone are insufficient.
6. **Fail closed:** ambiguous metadata, unsupported operators, unapproved joins, invalid parameters, or unsafe SQL stop before execution.
7. **Visible processing:** every meaningful stage and its status is available in the UI and trace.
8. **Correctness over execution success:** benchmark correctness compares output rows/values and grain against independently derived expected results.
9. **No silent semantic assumptions:** metrics such as sales, profit margin, repeat customer, host, vote, title, or return must have a clear definition or be explained as assumptions.
10. **Single-database scope:** the selected registry entry is the only database available to a request.

## 4. Inputs and configuration

Each request includes:
- `database`: registry key chosen by the user.
- `question`: non-empty natural-language question.
- Optional request-scoped AWS keys entered in the UI sidebar (access key ID, secret access key, optional session token). They are held in the browser session only and are never logged or displayed. When both key fields are not filled, the configured AWS profile chain is used.

The request must not supply file paths, arbitrary catalog names, table allowlists, or connection strings.

`config.yaml` must configure:
- Registered database entries and paths to DuckDB files.
- Physical-schema metadata path.
- Semantic metadata path.
- Relationship metadata path, if separate.
- Planner model/provider settings.
- SQL Generator model/provider settings.
- Region, temperature, output token limits, timeout, and prompt-size limits.
- Query row/result caps and execution timeout/resource limits.
- Supported SQL capability flags.
- Logging/privacy settings.
- Benchmark dataset/database mappings and optional fixture paths.

No secrets may be stored in source control or metadata files.

## 5. Database registry and metadata catalog

For the selected database, load:
1. Physical metadata: exact table and column names, data types, nullability when available, keys, table grain, and column synonyms.
2. Semantic metadata: business concepts, metric definitions, aliases, dimensions, value synonyms, and interpretation notes.
3. Relationship metadata: source table/column, target table/column, join type policy, cardinality, relationship purpose, and enabled status.
4. SQL capability configuration applicable to the database.

Validation must ensure:
- Registry paths exist.
- The database file and all metadata belong to the selected registry entry.
- Internal database/schema keys agree with the registry key.
- Referenced tables and columns exist in physical metadata.
- Relationship endpoints and declared keys are valid.
- Relationship definitions do not silently create cross-database access.

Metadata is loaded/indexed once per file version and cached. A change to a metadata file invalidates the relevant cache. Runtime query handling must not read every YAML/Markdown file independently for each LLM request. It loads a catalog once, retrieves only the relevant metadata subset per question, and includes only that subset in prompts. When a question has a time signal (a year, or a generic temporal word such as month, date, or trend), the temporal columns of the selected tables are always included, so the planner can filter by time (design §4, step 5a).

## 6. Semantic metadata

Metadata must support:
- Business terms and synonyms.
- Metric definitions, including expression/source columns, aggregation, unit, filters, and grain.
- Dimension definitions and aliases.
- Date roles (order date, ship date, vote date, date added, host-since date, etc.).
- Entity identity and row grain.
- Null and duplicate handling guidance.
- Relationship edges and cardinality.
- Approved derived metrics and their formulas.
- Ambiguity notes and unsupported concepts.

### 6.1 Metric definition lifecycle

- Each metric has a status: `approved` (usable in SQL) or `proposed` (never used in SQL until a user accepts it).
- A metric term resolves to one of three outcomes: `approved` (use it), `proposed` (ask the user to confirm or reject it), or `undefined` (no entry exists).
- For an `undefined` term, the system proposes a definition from the question and the selected database's schema and synonyms. The proposal must pass the same metric validation as authored metrics. If it fails validation, no proposal is shown.
- The user confirms or rejects a proposal in the UI. Only accepted proposals are saved, with status `approved`, to the selected database's business file `specs/properties/business/<db>.yaml` under `metrics`. Rejected proposals are not saved.
- Metric definitions are never hand-written per dataset or per benchmark question. Semantic requirement files keep `metrics: []` until a user accepts a proposal.
- Each metric has a status: `approved` (usable in SQL) or `proposed` (never used in SQL until a user accepts it).
- A metric term resolves to one of three outcomes: `approved` (use it), `proposed` (ask the user to confirm or reject it), or `undefined` (no entry exists).
- For an `undefined` term, the system proposes a definition from the question and the selected database's schema and synonyms. The proposal prompt contains no dataset-specific hints. The proposal must pass the same metric validation as authored metrics. If it fails validation, no proposal is shown.
- The user confirms or rejects a proposal in the UI. Only accepted proposals are saved, with status `approved`, to the selected database's business file `specs/properties/business/<db>.yaml` under `metrics`. Rejected proposals are not saved.
- Accepted metrics take effect after the metadata cache reloads. Generated semantic YAML is never edited by hand.
- Data-quality notes that affect a metric's grain (for example exact duplicate rows or no usable key) come from profiling each table. They are not written as per-dataset caveats. Metrics never deduplicate rows without a confirmed key.

A concept whose meaning materially affects the answer must be defined in metadata or clarified. For a ratio-like concept, the definition must state whether it is a ratio of aggregates or an average of row-level ratios. “Returned order” requires a Returns source or return indicator. “Second purchase” requires a reliable customer identifier, order/purchase date, and complete-enough history.

## 7. Planner LLM requirements

The Planner must:
- Receive the question plus relevant physical/semantic/relationship metadata and enabled capabilities.
- Return exactly one JSON object conforming to the versioned plan schema.
- Express intent, sources, joins, filters, outputs, aggregation, stages, windows, calculations, and sort/limit behavior.
- Never return SQL, raw executable expressions, Python, or instructions to bypass validation.
- Explicitly identify missing fields, unresolved definitions, or insufficient data history.
- Return `cannot_answer` or `needs_clarification` rather than inventing unsupported facts.
- Not assume relationships based only on similar column names.

The plan schema must support:
- One or more source tables from the selected database.
- Explicit join edges drawn from the approved relationship catalog.
- Filter predicates with typed values and AND/OR grouping.
- Dimensions and metrics.
- Aggregate and row-level derived expressions from an allowlisted expression vocabulary.
- Grouping and `HAVING`.
- Ordered computation stages with dependencies.
- CTE/subquery intent.
- Window partitions, order, frames, and functions.
- Existence/anti-existence tests.
- Date truncation, date differences, and period comparisons.
- Set operations where enabled.
- Percentiles/ranking/top-N per group.
- Final ordering and result limit.
- Clarification or cannot-answer outcomes.

The plan schema must not allow arbitrary SQL snippets as a way to express computation.

The planner and SQL generator call the LLM through one provider interface. The provider is chosen by configuration (currently Bedrock, through the AWS profile chain). Model, region, and token limits come from configuration, and provider errors are reported as `llm_error` without exposing credentials or raw provider messages.

## 8. Deterministic plan validation and metadata resolution

Before SQL generation, code must validate:
- JSON structure, schema version, required fields, types, enums, and unknown keys.
- Database scope.
- All tables, columns, metrics, dimensions, date roles, and relationship references.
- Data types and operator compatibility.
- Join edges against the relationship registry.
- Join cardinality and potential row multiplication.
- Grain changes across each computation stage.
- Aggregate/grouping consistency.
- Window-function requirements.
- Calculation dependencies and aliases.
- Null handling and divide-by-zero policies when relevant.
- Feature flags for every requested SQL capability.
- Semantic completeness and ambiguity.

The validator must reject an unconfigured join, ambiguous field, undefined metric, invalid window frame, unsupported function, or plan that cannot be represented safely. It must produce actionable reasons.

## 9. SQL Generator requirements

The SQL Generator receives:
- Original question.
- Validated plan.
- Selected database physical schema subset.
- Relevant semantic definitions and relationship paths.
- Supported DuckDB dialect/features and generation rules.
- Required response schema.

It returns exactly one JSON object with:
- `sql`: one SQL query containing placeholders for data values.
- `params`: ordered parameter values.
- Optional `assumptions`: only assumptions already approved by the plan/metadata; otherwise empty.

It must not:
- Add tables, columns, joins, filters, calculations, or metrics absent from the validated plan.
- Change the business meaning of a metric.
- Inline user-supplied values in SQL. Strings, numbers, dates, and booleans may be written inline or as `?` placeholders. Injection defense rests on the AST allowlist, dataset-scoped names, listed-value checks, and LIMIT and OFFSET bounds.
- Reference a different database or external files.
- Include multiple statements.
- Execute the SQL.
- claim correctness merely because SQL parses.

Simple query shapes may optionally use a deterministic SQL builder. Both paths must obey the same plan contract, validator, trace, and execution controls. The default implementation can use the SQL Generator for generality; any fast path must be explicitly configured and tested.

## 10. Supported SQL capability families

The initial capability matrix must explicitly define enabled/disabled support for each family:

### Basic
- Projection, aliases, `DISTINCT`.
- Typed filters, `IN`, `NOT IN`, `BETWEEN`, null checks, `LIKE`/case-insensitive matching if safely parameterized.
- `AND`/`OR`/`NOT`.
- `COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, `MAX`.
- Multiple metrics and dimensions.
- `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`.

### Joins
- Inner and left joins between tables of one database, only along approved relationship edges with enabled join policies.
- Join keys that need normalization (for example `lower(trim())` or a numeric cast on one side) are supported only when the relationship records that normalization and the join condition applies exactly the same operations. A raw comparison on such a key is rejected, not run.
- Joins across databases are not supported (see §2 out of scope).
- Fan-out rule: a parent-side aggregate over a one-to-many join is rejected.

### Multi-stage and nested
- Multiple CTEs with dependencies.
- Derived tables and nested subqueries.
- Correlated subqueries where supported by DuckDB.
- `EXISTS` and `NOT EXISTS`.
- Conditional aggregation and `CASE WHEN`.
- Semi-joins and anti-joins.
- `UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT` when enabled.

### Window analytics
- `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `NTILE`.
- `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`.
- Window aggregates and cumulative/rolling calculations.
- Explicit `PARTITION BY`, `ORDER BY`, and frame boundaries.
- Tie handling and deterministic ordering where required.

### Time analytics
- Date extraction and truncation.
- Date arithmetic and date differences.
- Year-over-year and month-over-month changes.
- Period-over-period percentage change with zero/null prior-period policy.
- Consecutive-period comparisons.
- Cohorts, retention, first/second events, elapsed time between events.
- Missing-period behavior must be explicitly defined; do not fabricate dates unless the plan calls for a calendar spine and the system has an approved way to create one.

### Statistical and ranking
- Percentiles/quantiles using approved DuckDB functions.
- Top-N and bottom-N per group.
- Pareto/cumulative contribution.
- Outlier comparisons using clearly defined formulas.
- Ratios computed from aggregated numerators and denominators when the metric definition requires it.

The implementation must not claim all SQL is supported. Each family is enabled only when implemented, AST-validated, and covered by tests. Unsupported families are rejected explicitly.

## 11. SQL validation and security

Use DuckDB parsing and/or a SQL parser with an AST. Validation must recursively inspect the full query tree, including every CTE, nested query, join, and set-operation branch.

Required checks:
- Exactly one read-only query.
- Statement type is `SELECT` or a supported read-only query expression.
- All tables/columns belong to the selected database and metadata allowlist.
- All joins match approved relationship edges and enabled join policies.
- All functions and syntax belong to the enabled capability set.
- No DDL, DML, attach/detach, `COPY`, `INSTALL`, `LOAD`, extension installation, external file scan, network access, pragma changes, or other unapproved operations.
- Parameter count and ordering are correct; parameters correspond to the validated plan.
- No user data values are interpolated into SQL text.
- Query complexity limits: maximum CTE count, nesting depth, joins, window operations, and SQL length as configurable limits.
- Row/result caps and execution timeout are enforced.
- SQL cannot change connection settings or access another catalog/database.
- Validator rejects unsupported AST nodes rather than attempting to repair SQL silently.

Read-only connection mode is an additional control, not a replacement for AST validation.

## 12. Execution and result contract

Execute only validated SQL on a read-only DuckDB connection. Apply configured timeout/resource limits and a result cap. Return:
- Column names and data types where available.
- Result rows.
- Row count.
- Query duration.
- Final SQL and bound parameter metadata in the UI (parameter values may be redacted according to privacy settings).
- A safe user-facing error when execution fails.

Do not append a row limit in a way that changes aggregate semantics. Use a final output limit for large grouped/row outputs only when consistent with the plan and configuration.

## 13. Streamlit UI and pipeline trace

Maintain a minimal interface with:
- Database selector.
- Natural-language question input.
- Ask action.
- Ask/result view.
- Step Plan/trace view.
- Run SQL tab kept as a stub unless separately specified.

Display each stage as it completes. Show:
- Metadata selected and retrieval summary.
- Planner prompt summary and planner output.
- Parsed plan and validation decisions.
- SQL prompt summary and SQL Generator output.
- Parsed SQL and parameters.
- AST validation findings.
- Execution result/status.
- Final response.

Both LLM calls must be visible as distinct stages. Stream output chunks when the provider supports it. Never display credentials. Do not expose stack traces or secrets. Trace outputs must be bounded to avoid rendering huge metadata or results.

## 14. Observability and failure behavior

Each stage returns a standard `StageResult`:
- `stage`
- `status`: `ok`, `failed`, or `skipped`
- `output`
- `reason`
- `duration_ms`

Stop at the first failed stage and mark all later stages `skipped`. The final response status is one of `ok`, `needs_clarification`, `cannot_answer`, `rejected`, or `error`.

Logs and UI trace must share stage names/statuses. Logs must not contain credentials or secret configuration. Log raw LLM output only when the configured privacy policy permits it; default to safe summaries and redact sensitive values.

## 15. Dataset ingestion and reproducibility

Create reproducible DuckDB databases/fixtures from:
- `sample_-_superstore.xls`
- `113thCongress.xlsx`
- `airbnb.xlsx`
- `netflix_titles.xlsx`

Ingestion must record workbook sheet names, original column names, normalized physical names if any, row counts, inferred types, null counts, duplicate counts, and row grain. Preserve source columns or maintain explicit reversible mappings. Do not silently assume joins.

Netflix has a main titles sheet plus director, country, cast, and category relationship sheets keyed by `show_id`; these are candidates for configured one-to-many relationships and must be validated for fan-out. Congress contains `Bills` and `Sunlight Info`; joining them requires profiling `Source URL`/other candidate keys and verifying cardinality. Airbnb is a single-sheet listing dataset. Sample Superstore is commonly line-item grain, but actual columns and grain must be confirmed during ingestion.

## 16. Benchmark and quality gates

For every benchmark question:
- Store its database, intent, required fields, required semantics, SQL capability tags, expected result oracle or oracle method, and known caveats.
- Distinguish deterministic expected output from questions requiring explicit assumptions.
- Test against independently derived results, not merely successful execution.
- Include synthetic fixtures for ties, duplicate lines, nulls, missing periods, no second event, zero denominator, and one-to-many fan-out.
- Classify failures as: intent/planning, metadata retrieval, plan validation, SQL generation, SQL validation, execution, or semantic/result correctness.
- Add confirmed failures as generic regression tests.
- Keep a held-out question subset that is not used to shape implementation details.

## 17. Acceptance criteria

The release is accepted only when:
1. The four databases can be built from the supplied workbooks reproducibly.
2. Registry/schema mismatches fail before either LLM is called.
3. Metadata is cached and only relevant context is sent per request.
4. Planner emits valid structured plans and never SQL.
5. Plan validation rejects unsupported or ambiguous intent deterministically.
6. SQL Generator emits parameterized SQL matching the validated plan.
7. AST validation checks all nested query structures and rejects unsafe access.
8. Every enabled SQL capability has positive, negative, and edge-case tests.
9. Pipeline traces show both LLMs and all deterministic stages.
10. All 30 benchmark questions have an evaluation status, and result correctness is independently checked wherever the source data supports it.
11. No production branch/template is keyed to a benchmark question.
12. The application remains runnable with mocked LLMs and without network access.
