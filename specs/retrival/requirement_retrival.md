Requirements: Natural-language analytics over one database

1. Purpose
Answer natural-language questions using one selected DuckDB database. Results may be rows, grouped summaries, aggregates, or approved calculations such as year-over-year (YoY) comparisons. Show every pipeline stage in the existing Streamlit UI.

Complex queries use two LLM calls:
- The planner interprets the question and returns a structured plan, never SQL.
- The SQL generator converts the validated plan, physical metadata, and relevant business definitions into parameterized SQL.

Deterministic code resolves metadata, validates both responses, enforces safety, and executes SQL. If explicitly enabled, simple queries may use a deterministic SQL builder instead of the second LLM call.

2. Inputs
- `database`: a name registered in `config.yaml`.
- `question`: plain text.
- AWS credentials for the request, or the standard AWS credential chain if none are supplied.

Requests cannot provide file paths, arbitrary table names, or connection settings.

3. Database resolution and metadata
- Resolve databases only through the `config.yaml` registry. An unknown database or missing required file returns `error`; never fall back to another database.
- Load the selected database's DuckDB file, physical metadata, and any configured semantic/business metadata.
- Verify metadata files agree and their schema/database key matches the requested registry name.
- Cache metadata by file modification time and reload it when changed.
- Use only metadata belonging to the selected database.

4. Planner LLM
- Provide the question, physical tables and columns with types, available categorical/Boolean values, supported operations, configured business definitions and relationships, and planner rules.
- Require one JSON object describing intent and operations; it must not contain SQL.
- Support repeat-purchase analysis through the optional `repeat_purchase` plan key (average days from a customer's first to second purchase, and the count of customers who never returned), rendered by a fixed, validated template.
- Support row selection; one or more dimensions and metrics (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, and `SHARE`, the fraction of rows that match a condition); categorical, Boolean, numeric, and date filters; `GROUP BY`, `HAVING`, `ORDER BY`, and `LIMIT`; supported date grains (year, quarter, month, year-month); approved calculations (including YoY change and percentage growth); and approved joins when relationship metadata and join support are enabled.
- If the request is unsupported by the selected database or capabilities, return `{"cannot_answer": true, "reason": "..."}`.
- Do not invent missing business definitions. For materially ambiguous concepts not resolved by metadata or the question, request clarification or return `cannot_answer`.

5. Query-plan contract
Document the exact JSON schema in `design.md` and validate it in code. It must support:
- `table`: primary physical table;
- `select`: columns for row queries;
- `dimensions`: grouping/output dimensions;
- `metrics`: aggregations with function, column or `*`, and alias; a `SHARE` metric also has a non-empty `where` list of filters;
- `filters`: column, operator, and value;
- `group_by`: dimension references;
- `having`: conditions on aggregate results;
- `order_by`: dimension/metric reference and direction (`ASC`/`DESC`);
- `calculations`: explicitly typed derived calculations, initially including YoY sales/metric comparison;
- `limit`: bounded result limit.

Accept only schema-defined fields and shapes. Reject unknown keys, malformed JSON, wrong value shapes, or extra prose. A permitted Markdown code fence may be removed before parsing.

6. Deterministic plan matching
- Match tables, columns, dimensions, metrics, aliases/references, operators, aggregations, date grains, and categorical values against metadata.
- Try exact, normalized, then configured-synonym matching. Each matching step must resolve to exactly one candidate. Apply the same order to stored categorical/Boolean values.
- For columns above the configured distinct-value threshold, make one bounded, parameterized lookup. Accept exactly one match; otherwise reject with candidates.
- Validate numeric/date types and operator/value shapes in code.
- Never guess ambiguous items. On failure, return `rejected` with the item and closest candidates.
- Matching the same plan against the same metadata must always produce the same result.

7. Business semantics
- Support reusable, database-specific business metadata for terms, metric definitions, dimensions, synonyms, and approved relationships.
- Treat supplied definitions as authoritative; for example, a sales metric may define its source column and aggregation, and a YoY metric its calculation.
- Do not require a hand-written rule for every question: the planner interprets intent and the SQL generator composes supported SQL from the plan and metadata.
- Do not invent materially significant definitions (such as profit margin, active customer, or net sales). Reject or clarify unsupported or ambiguous concepts.

8. SQL generation
- Give the SQL generator the original question, validated/matched plan, selected database's physical schema, relevant business definitions, supported SQL capabilities, and generation rules.
- Require one JSON object containing SQL with placeholders and a separate parameter list. The generator must not execute SQL.
- Restrict SQL to the selected database and allowlisted physical tables and columns. Bind all values; never interpolate user values into SQL text.
- Support approved constructs such as `GROUP BY`, `HAVING`, `ORDER BY`, CTEs, and window functions. Allow joins only through configured, validated relationships when enabled.
- Do not go beyond the validated plan or add unrequested filters, metrics, joins, or calculations.
- Reject failed or malformed generation and unsupported constructs. Never retry either LLM automatically.
- A deterministic SQL builder may handle simple query shapes; record the selected path in the trace.

9. SQL validation
Before execution, deterministic validation must verify:
- The output is one well-formed JSON object with exactly the documented keys.
- SQL parses as one supported, read-only query.
- All tables and columns are allowlisted physical names from the selected database; joins follow approved relationships.
- Grouping, aggregate references, ordering, `HAVING`, CTEs, and window functions are valid and supported.
- There are no write, DDL, administrative, multiple-statement, external-file-access, or unapproved-function operations.
- Placeholders and parameter count/types match; user values are not embedded in SQL; parameters agree with validated filters and approved calculations.
- Row queries have a bounded limit, and aggregate/grouped queries have a configured maximum where needed.
- The query does not access another database or catalog.

On validation failure, return `rejected` and do not execute SQL. Use a SQL parser/AST validator or DuckDB parsing facilities, not string checks alone.

10. Execution
- Execute only validated SQL on a read-only DuckDB connection.
- Enforce a row cap and reasonable execution/resource limits.
- Return rows as a table, including named columns for aggregate/grouped results.
- On runtime failure, return `error` with a safe user-facing message and no stack trace.

11. Pipeline trace and UI
Keep the current Streamlit layout: Ask tab, Step plan tab, and Run SQL tab. Do not redesign it.

Show each stage's output as soon as it completes. Display planner JSON and the SQL generator's response (SQL and parameters) separately. Stream each LLM response in chunks when supported. Show a failed stage's reason and mark later stages as skipped.

The Ask tab shows final status, available SQL and parameters, and results. The Step plan tab has one panel per pipeline stage. The Run SQL tab runs one typed read-only SELECT or WITH statement and shows the result table or an error.

12. Suggested stage sequence
1. Submit
2. Resolve
3. Planner Prompt
4. Planner LLM
5. Parse Plan
6. Match and Validate Plan
7. SQL Prompt
8. SQL Generation
9. Validate SQL
10. Execute
11. Respond

The deterministic SQL-builder path must also show its output and every processing step. The implementation uses eleven numbered stages, but must expose every meaningful stage and both LLM calls separately.

13. Logging and security
- Record one structured log entry per completed stage, aligned with the UI trace.
- Subject to the configured privacy policy, log the question, stage statuses, raw model replies, matched plan, SQL and parameter metadata, validation result, execution duration, and row count.
- Never log AWS credentials or include them in prompts, traces, SQL-generation inputs, or executor arguments.
- Do not expose credentials, secrets, or stack traces in the UI.
- Keep credentials in request/session scope and pass them only to the Bedrock client.
- Use read-only executor access even if validation has a defect.

14. Response statuses
- `ok`: query succeeded; return the plan, matched plan, SQL/parameters, trace, and results.
- `rejected`: plan matching or SQL validation failed; return reasons and closest candidates when applicable.
- `cannot_answer`: the planner determines the request is unsupported or cannot be answered reliably.
- `error`: database resolution, prompt construction, Bedrock, or execution failed; return a safe message without a stack trace.

15. Acceptance criteria
- Simple row and aggregate questions continue to work.
- “Sales by subcategory in 2025 in India” returns one row per subcategory with aggregated sales and optional explicit sorting.
- Multi-dimension, multi-metric grouped queries, and supported `HAVING` and `ORDER BY` work.
- Yearly sales and YoY absolute/percentage changes use explicit calculation definitions. YoY by dimension compares each member with itself across periods.
- Unsupported or ambiguous business definitions are never invented.
- Unknown columns, tables, joins, functions, and calculations are rejected before execution.
- Every LLM call and pipeline stage appears in the trace; model text streams when supported.
- Credentials never appear in logs, traces, prompts, SQL, or output. SQL values are parameterized and execution is read-only.
- No automatic LLM retry occurs after failed plan or SQL generation/validation.
- The same model output and metadata always produce the same matching and validation decisions.