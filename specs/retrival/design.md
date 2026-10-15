
# Design — Generic Natural-Language-to-SQL Analytics

Version: 2.0  
This document is the implementation contract corresponding to `requirements.md`.

## 1. Architecture overview

Use a deterministic orchestration layer around two LLMs. LLMs interpret and compose; code owns metadata, validation, safety, and execution.

```text
User question + selected database
            |
            v
1. Request validation
            |
            v
2. Registry + cached metadata catalog
            |
            v
3. Relevant metadata retrieval
            |
            v
4. Planner prompt -> 5. Planner LLM
            |
            v
6. Parse + validate plan
            |
            v
7. SQL prompt -> 8. SQL Generator LLM
            |
            v
9. Parse generated SQL/parameters
            |
            v
10. Recursive AST validation
            |
            v
11. Read-only DuckDB execution
            |
            v
12. Result shaping + response
```

Each stage returns a `StageResult`; failures stop execution and later stages become `skipped`. The UI consumes the same ordered trace used for logging.

## 2. Suggested project structure

Keep the codebase modular and use the existing repository structure where practical. The following is a logical target, not a requirement to create redundant layers:

```text
config.yaml
specs/properties/
  physical_mapping/
  semantic_mapping/
src/
  ui/
    app.py
  query/
    models.py
    config.py
    catalog.py
    metadata_retriever.py
    semantic_resolver.py
    planner_prompt.py
    planner_client.py
    plan_parser.py
    plan_validator.py
    sql_prompt.py
    sql_generator_client.py
    generated_sql_parser.py
    sql_builder.py              # optional simple-query path/helpers
    sql_ast_validator.py
    executor.py
    pipeline.py
    result_formatter.py
  ingestion/
    workbook_inspector.py
    duckdb_builder.py
  evaluation/
    benchmark_runner.py
    oracle.py
tests/
  unit/
  integration/
  security/
  benchmark/
```

Do not create a module unless it owns a distinct responsibility. If the existing project already has a suitable module, extend it rather than creating duplicate abstractions. UI code must not contain query-planning or SQL-validation logic.

## 3. Configuration and metadata

### 3.1 Registry example

```yaml
input_dir: data
database_dir: databases
databases:
  sample_superstore:
    physical_mapping: specs/properties/physical_mapping/sample_superstore.yaml
    semantic_mapping: specs/properties/semantic_mapping/sample_superstore.yaml
    database_file: databases/sample_superstore.duckdb
  airbnb:
    physical_mapping: specs/properties/physical_mapping/airbnb.yaml
    semantic_mapping: specs/properties/semantic_mapping/airbnb.yaml
    database_file: databases/airbnb.duckdb
  netflix_titles:
    physical_mapping: specs/properties/physical_mapping/netflix_titles.yaml
    semantic_mapping: specs/properties/semantic_mapping/netflix_titles.yaml
    database_file: databases/netflix_titles.duckdb
  _113thCongress:
    physical_mapping: specs/properties/physical_mapping/_113thCongress.yaml
    semantic_mapping: specs/properties/semantic_mapping/_113thCongress.yaml
    database_file: databases/_113thCongress.duckdb
retrieval:
  default_row_limit: 1000
  max_prompt_chars: 60000
  value_list_threshold: 30
```

The repository's `config.yaml` is the source of truth for these keys and paths. The `limits` block below is the D8 contract and is enforced as written in §7.4. `max_prompt_chars` and `max_result_rows` mirror `retrieval.*` and are read from there. `max_sql_chars` is 20000, matching the parser input cap (§6.5).

```yaml
limits:
  max_sql_chars: 20000
  max_ctes: 20
  max_nesting_depth: 12
  max_joins: 12
  max_window_functions: 20
  query_timeout_seconds: 30
```

`max_result_rows` is not repeated here. The row cap is `retrieval.default_row_limit` (1000), and §8 applies it at execution.

### 3.2 Physical metadata model

At minimum:
- database key
- tables: physical name, description, grain
- columns: physical name, type, nullable, key role, synonyms, date role, allowed operators, optional stored values
- source-to-physical name mapping when ingestion renames columns
- ingestion/profile provenance

Do not infer primary keys from uniqueness in a small sample alone. Profile the full dataset and record confidence.

### 3.3 Semantic metadata model

At minimum:
- `concepts`: name, synonyms, definition, status
- `metrics`: name, definition, source columns, aggregation/formula, unit, grain, null/zero policy
- `dimensions`: name, synonyms, source column or approved expression
- `date_roles`: named date concepts and physical columns
- `relationships`: relationship IDs and intended analytical meaning
- `ambiguities`: concept, unresolved alternatives, clarification policy
- `capabilities`: enabled feature families and limits

Metric entry format (authored in the semantic requirements markdown under `## Metric definitions` as one fenced `yaml` block, generated into the semantic YAML `metrics:` list):

- `name` (snake_case, unique per database), `synonyms` (list), `status` (`approved` | `proposed`), `kind`, `unit`, `grain`, `null_zero_policy`, `description`.
- `kind: ratio` requires `numerator` and `denominator`. Each is `{table, column, aggregate, filters?}`.
- `kind: aggregate` requires `expression` = `{table, column, aggregate, filters?}`.
- `kind: weighted_average` requires `value` and `weight` as `{table, column}` and computes `SUM(value*weight)/SUM(weight)`.
- `aggregate` is one of `sum`, `count`, `avg`. `sum` and `avg` require a numeric column.
- `filters` are a list of `{column, operator, value}`. `operator` is one of `=`, `!=`, `<`, `<=`, `>`, `>=`.
- Validation rejects unknown tables, columns, kinds, operators, or statuses, and rejects `proposed` metrics in SQL.

Accepted proposals are written to `specs/properties/business/<db>.yaml` under `metrics` with the same format. The loader merges that file into the catalog. Its fingerprint is part of the catalog cache key.

Semantic files may be YAML or Markdown if the loader normalizes them into a validated internal model. Runtime retrieval must not parse every individual file on every request.

### 3.4 Relationship model

Each relationship has:
- unique ID
- source table/column(s)
- target table/column(s)
- cardinality (`one_to_one`, `many_to_one`, `one_to_many`, `many_to_many`, or `unknown`)
- approved join direction/type
- semantic purpose
- enabled flag
- join policy (`inner` or `left`, and which side may be dropped)
- normalization applied to each side before comparing keys, or `none`. Stored as structured data per endpoint (`child` and `parent`), not free text. Each endpoint's normalization is an ordered list of operations from the allowlist in §5.6 (`lower`, `trim`, `cast` to `BIGINT`, `VARCHAR`, `DOUBLE`, or `DATE`). An empty list means no normalization on that side.
- enforceable-now flag (whether the key can be declared as a foreign key today)
- fan-out warnings or pre-aggregation requirements

Relationships are declared in the physical requirements markdown and generated into `specs/properties/physical_mapping/*.yaml` by `src.mapping.requirements_to_yaml`. A relationship is enabled for joins only when its key evidence is recorded in the same file. A join that needs normalization is enabled only when the normalization is recorded and applied in the join condition. The generator reads the structured per-endpoint normalization into YAML and rejects any free-text normalization it cannot parse.

A relationship with unknown cardinality cannot be used for metrics across a one-to-many path unless the plan defines and validates the necessary pre-aggregation strategy. Similar column names are not sufficient evidence of a relationship.

## 4. Metadata loading and retrieval

1. Load the registry at startup.
2. On database selection, validate paths and load that database's metadata into a normalized catalog.
3. Cache catalog by registry entry and metadata file modification/version fingerprint.
4. Resolve question concepts to candidate tables, columns, metrics, dimensions, date roles, and relationship paths.
5. Retrieve only relevant metadata plus minimal relationship context.
5a. Time signal (added after the first live benchmark). A question has a time signal when it contains a 4-digit year (`2024`, `2024-01`), or one of these generic temporal words in any form: year, month, quarter, week, day, date, time, period, trend, ago, since, recent, earliest, latest. When it does, retrieval also adds every temporal column of the selected tables, with reason `time_signal`. A column is temporal when its physical type is DATE, TIMESTAMP, or TIME, its semantic `column_class` is `date`, or a word of its name is a temporal word above. Physical types alone are not enough, because some date columns are stored as VARCHAR. If no table is selected by lexical matching, temporal columns of every table in the database are added instead. The rule uses only metadata and generic words; it names no dataset or question.
6. If candidate resolution is ambiguous, expose candidates to the planner only when it can make a grounded choice; otherwise ask for clarification.
7. Enforce prompt-size limits after retrieval. Do not solve prompt overflow by silently dropping required columns or definitions.
8. Never allow retrieval to cross the selected database boundary.

5b. Token normalization (added after SS01 failed with "customers" not found). Lexical matching compares normalized tokens, and the same normalization applies to question text, physical column names, synonyms, and stored values. Tokenizing lowercases the text, splits on non-alphanumeric characters, and drops stopwords. Each remaining token is reduced to its singular form with a fixed suffix rule: `ies` becomes `y` (tokens longer than 4 characters); `sses`, `shes`, `ches`, `xes`, and `zes` lose their final `es`; tokens ending in `ss`, `us`, or `is` are kept; any other final `s` is removed from tokens longer than 3 characters. Stopwords are checked after normalization, so `shows` drops like `show`. Temporal words (step 5a) are normalized the same way. Example: "distinct customers" matches the column `Customer_ID`, because both contain the token `customer`. The rule is generic and names no dataset. It may over-match. The planner and the validators remain the guard against wrong columns.

5c. Full planner schema (added after SS01 showed the planner could not see `Customer_ID`). Lexical retrieval narrows metadata, so a column it misses is invisible to the planner. The planner therefore also receives the complete physical schema of the selected database: every table, and every column with its type, with no synonyms, values, or relationships. This list is read from the database's physical requirements file, `specs/properties/physical_requirements/<key>.md`, under the `## Columns` section (one `### \`Table\`` heading per table, followed by rows whose first cell is a backticked column name and whose second cell is the type). Loading rules:   - The parser reads only the `## Columns` section. It ignores the header row and any row whose first cell is not backticked. Line endings are normalized.
   - `load_catalog` cross-checks the parsed tables, column names, and types against the physical YAML. Any table, column, or type that is missing or different on either side raises `MetadataMismatchError` and the database does not load. The YAML stays the runtime truth for everything else.
   - The markdown file joins the catalog fingerprint, so editing it invalidates the cache.
   - A missing markdown file raises a metadata error. There is no silent fallback.
   The planner prompt lists this full schema in a section after the retrieved context. The size rule (step 7) still applies. The full schema is not sampled or trimmed, because a partial schema reintroduces the failure. The SQL generator keeps its plan-scoped metadata (section 6.1). The rule names no dataset or question.

A deterministic lexical/synonym resolver is sufficient for the initial implementation. An embedding search may be added only if it materially improves metadata retrieval and has tests. The architecture must not depend on vector search.

## 5. Planner contract

### 5.0 Current design (supersedes 5.1–5.7, 6.2, 7.2, and the stage table in 9.2)

The pipeline is a flat flow. The planner writes a short analysis, the SQL generator writes free SQL from it, and deterministic checks run afterward.

1. Planner (`src/query/prompts.py`, `build_planner_prompt`). It returns one JSON envelope: `{"status": "ready" | "needs_clarification" | "cannot_answer", "analysis": string, "clarification": null | {"message": string, "missing": [string]}}`. A ready reply needs non-empty `analysis`, capped at 8000 characters. A non-ready reply needs a `clarification.message`. `missing` is capped at 10 terms. `src/query/analysis.py` (`parse_analysis`) checks only this envelope. It does not inspect the analysis text.
2. Planner repair. One repair call runs when the envelope is malformed and the repair prompt fits `max_prompt_chars`. `LlmError` is never retried.
3. SQL generator (`build_sql_prompt`). It receives the question, the analysis, the physical and semantic catalog, retrieved context, and the enabled capabilities. It returns `{"sql": string, "params": [...], "assumptions": [...]}`. The generator is not constrained to a plan grammar.
4. Deterministic checks, in order (`src/query/pipeline.py`, `_check_generated_sql`):
   - `validate_sql_ast`: a single read-only SELECT over dataset tables, with the capability gates in `config.yaml`.
   - `check_sql_limits`: structural limits only (CTE count, nesting depth, joins, window functions, LIMIT and OFFSET bounds). Inline constants are allowed.
   - `check_sql_names_and_values` (`src/query/sql_checks.py`):
     - Every column reference is a physical column, an output alias, or a CTE output column. A qualified reference to a CTE is checked against that CTE's output columns. The check is skipped when a CTE selects `*`.
     - A string value (parameter or inline) compared with a column that has listed `values` in the semantic YAML must match one of them, ignoring case. Numeric values are not checked.
5. Repair. One SQL repair call runs for the categories `unsupported`, `invalid_sql`, `from`, `joins`, and `unsafe_sql` when the reason contains "not allowed". Multiple statements, writes, and out-of-dataset tables are not repaired.

Literal policy. Strings, numbers, dates, and booleans may be inline or `?` placeholders. The generator is not forced to use placeholders (user decision). The injection defense is the AST allowlist, dataset-scoped names, listed-value checks, and LIMIT and OFFSET bounds. This is weaker than placeholder binding for string values, and it is an accepted trade-off.

Limits. The planner's output is non-deterministic, so the same question can pass on one run and fail on another. Plan-to-SQL fidelity is not verified. Only names, structure, and string values are checked.

Stages (`STAGES` in `pipeline.py`): `submit`, `resolve_database`, `load_catalog`, `retrieve_context`, `build_planner_prompt`, `planner_llm`, `parse_analysis`, `build_sql_prompt`, `sql_generator_llm`, `parse_generated_sql`, `validate_sql`, `execute`, `format_response`.

Status mapping: `clarification_needed` → `needs_clarification`. `unsupported` → `cannot_answer`. `invalid_sql` and `unsafe_sql` → `rejected`. Other failures → `error`.

### 5.1 Example plan JSON (historical; superseded by 5.0)

This is illustrative; implement and validate a formal JSON Schema or equivalent Pydantic model.

```json
{
  "schema_version": "1.0",
  "status": "ready",
  "intent": "compare monthly sales and profit by sub-category",
  "sources": [
    {"table": "Orders", "role": "order_line"}
  ],
  "joins": [],
  "filters": [
    {
      "target": {"kind": "column", "table": "Orders", "name": "Order_Date"},
      "operator": "date_range",
      "value": {"start": "2023-01-01", "end": "2024-12-31"},
      "boolean_group": "base_filters"
    }
  ],
  "stages": [
    {
      "id": "monthly",
      "kind": "aggregate",
      "depends_on": [],
      "group_by": [
        {"kind": "date_trunc", "date_role": "order_date", "grain": "month"},
        {"kind": "column", "table": "Orders", "name": "Sub-Category"}
      ],
      "metrics": [
        {"name": "sales", "aggregation": "sum", "column": "Sales", "alias": "sales"},
        {"name": "profit", "aggregation": "sum", "column": "Profit", "alias": "profit"}
      ]
    },
    {
      "id": "with_prior_period",
      "kind": "window",
      "depends_on": ["monthly"],
      "windows": [
        {
          "function": "lag",
          "input": "sales",
          "partition_by": ["Sub-Category"],
          "order_by": [{"expression": "month", "direction": "asc"}],
          "alias": "prior_sales"
        }
      ]
    },
    {
      "id": "final",
      "kind": "project",
      "depends_on": ["with_prior_period"],
      "calculations": [
        {
          "kind": "safe_divide",
          "numerator": {"kind": "subtract", "left": "sales", "right": "prior_sales"},
          "denominator": "prior_sales",
          "alias": "sales_growth_pct"
        }
      ]
    }
  ],
  "final_output": {
    "stage": "final",
    "columns": ["month", "Sub-Category", "sales", "profit", "prior_sales", "sales_growth_pct"],
    "order_by": [{"expression": "month", "direction": "asc"}],
    "limit": 1000
  },
  "clarification": null
}
```

The exact field names may be refined before implementation, but a plan must express intermediate computation stages and dependencies without containing SQL strings. Do not implement only the fields in this example; the schema must cover the enabled capability families.

### 5.2 Plan schema requirements

Use a strict versioned schema. Suggested top-level fields:
- `schema_version`
- `status`: `ready`, `needs_clarification`, or `cannot_answer`
- `intent`
- `sources`
- `joins`
- `filters`
- `stages`
- `final_output`
- `clarification`

Implemented stage kinds (decision, recorded before code):
- `aggregate`: GROUP BY keys plus named aggregate expressions. Optional `having`.
- `project`: named expressions with no aggregates.
- `window`: named window functions over the output of a prior stage or the source.

Each stage has `id`, `kind`, and `from`. `from` is `"source"` or the id of an earlier stage. A `column` reference is valid only when `from` is `"source"`. A `ref` reference (by output name) is valid only when `from` is a stage.

Filters are not stages. Top-level `filters` are applied as WHERE on the source. A `filter` stage (kind `filter`) is a separate stage that reads an earlier stage and holds a `where` predicate over that stage's refs. Final output is `final_output.stage`, not a `finalize` stage kind.

Not implemented and rejected by validation: `join` (top-level `joins`, gated by the `joins` capability, which is enabled once the join validation passes), `rank`, `deduplicate`, `exists_filter`, `set_operation`, and `date_spine`. Rank-like questions are expressed with `window` functions `rank` or `dense_rank`.

Stage operations reference names/IDs, not raw SQL. Expressions use a recursive, allowlisted expression grammar with these node types: `column` (table alias and name), `ref` (output name), `metric` (approved metric name, C5), `literal`, `arith` (`+`, `-`, `*`), `safe_divide`, `date_trunc` (day, week, month, quarter, year), `date_diff` (day, month, year), `aggregate` (sum, count, count_distinct, avg, min, max, median; `input` null means count(*)), `case`, `compare`, `in` (literal list, optional negate), `boolean` (and, or, not), `is_null`, and `normalize` (`{"type": "normalize", "input": column, "ops": [...]}`). `normalize` is valid only as an operand of a join `on` comparison (see §5.6). Its `ops` are an ordered list from `lower`, `trim`, and `cast` to `BIGINT`, `VARCHAR`, `DOUBLE`, or `DATE`. A `window` node is allowed only inside a window stage and never inside an aggregate or window argument. Window specs use `function` (row_number, rank, dense_rank, lag, lead, sum, avg, count, min, max), `input`, `partition_by`, `order_by`, `frame` (partition or running), and `offset` for lag and lead.

Permission propagates through operators. `arith`, `safe_divide`, `date_trunc`, `date_diff`, `case`, `compare`, `in`, `boolean`, and `is_null` pass the aggregate and window permission of their position to their children, so `safe_divide(sum(a), sum(b))` is valid in an aggregate stage. Aggregate and window arguments reset both permissions to off.

The grammar must reject raw SQL strings, arbitrary function names, unresolved references, aggregates outside `aggregate` stages, and expressions deeper than a fixed limit.

Capability flags map to these features: `windows` gates `window` stages, `case_expressions` gates `case`, and `joins` gates top-level `joins`. The planner only uses stage kinds and features that the config enables.

Deterministic fast path (D4): not implemented. The planner always produces a plan, and SQL is always generated by the LLM. The identifier-quoting and parameter helpers are kept for the validator.

### 5.3 Planner outcomes

- `ready`: validate and continue.
- `needs_clarification`: stop before SQL generation and ask a focused question.
- `cannot_answer`: stop and explain which required field/definition/capability is missing.
- Invalid JSON/schema: reject as planner output error.

Required keys depend on status (fixed after the first live benchmark). A `ready` plan must contain all nine top-level fields. A `needs_clarification` or `cannot_answer` plan must contain `schema_version`, `status`, and `clarification`. `intent`, `sources`, `joins`, `filters`, `stages`, and `final_output` may be omitted on those plans; a missing `intent` is stored as an empty string. If `final_output` is present on a non-ready plan it must still be null or valid. Other present ready-only fields are not executed, because stage 8 stops non-ready plans (§9.2).

### 5.4 Planner response parsing

- `src/query/planner_parser.py` converts the raw LLM text into a `Plan` through `plan.parse_plan`. It is the only code that reads raw planner text.
- Accepted forms: a bare JSON object, or one fenced block (```` ```json ```` or plain ```` ``` ````) whose only content is that object. Leading and trailing whitespace is allowed.
- Rejected: any prose outside the object or fence, more than one object, arrays or scalars at the top level, empty output, and input longer than `MAX_PLANNER_OUTPUT_CHARS` (20000).
- Duplicate keys at any depth are rejected, because the last value would otherwise silently win.
- Any decode or schema failure raises `PlanError` (subclass of `ValueError`) with the dotted path. The pipeline reports it as category `invalid_plan`. Raw text is not echoed beyond the trace's bounded reason.

### 5.5 LLM client contract

- `src/query/llm.py` defines `LlmClient.complete(prompt, on_chunk=None) -> str`. The pipeline depends only on this protocol, so tests use fake clients.
- `BedrockClient` builds a boto3 `bedrock-runtime` client from the configured profile and region. It uses `converse` when no chunk callback is given, and `converse_stream` when one is. Only text deltas are forwarded to the callback.
- Any provider or client-construction failure raises `LlmError` with category `llm_error`. The message names the exception type only, never the provider's message, which may contain request details.
- Unsupported `provider` values fail at construction time.
- Model ID, region, temperature, and max output tokens come from the `LlmSettings` in `config.yaml`. Nothing is hard-coded.

### 5.6 Plan validation (C5)

`src/query/plan_validator.py` checks a parsed `Plan` against the selected database's `Catalog` and the enabled capabilities. It runs after `parse_plan` and before any SQL generation. It never changes the plan. It returns the same plan or raises `PlanValidationError` with a dotted path and a reason. The pipeline maps this error to `invalid_plan` when the plan references something that does not exist, and to `unsupported` when the plan uses a feature that is disabled.

Identifier rules:
- Every `sources[].table` must be a physical table in the selected database's catalog. Names are matched exactly, not by fuzzy match.
- A `column` reference's `table` is a source alias (from `sources[].alias`), not a physical table name. The alias must exist and its table must contain the column. Aliases keep self-joins unambiguous.
- A `ref` (output name) must match a name defined by the referenced stage's `group_by` or `columns`. Missing names are rejected.
- `final_output.sort[].name` must be a name in `final_output.columns`.
- `final_output.stage` must be `"source"` or a stage id. When it is `"source"`, `final_output.columns` must name physical columns of the sources. When it is a stage id, each name must be an output of that stage. Stage outputs are not reachable from `source`.

Stage rules:
- A `column` reference is valid only when the stage's `from` is `"source"`. A `ref` reference is valid only when `from` is a stage id. The validator checks both directions.
- Stage order is the list order. A stage may reference only earlier stages.
- `filters` and `joins[].on` read the source only. A `ref` or `metric` node there is rejected. A `normalize` node is rejected everywhere except as a direct operand of a join `on` comparison.

Metric rules:
- A `metric` node `{"type": "metric", "name": "<snake_name>"}` is the only way a plan uses a business metric. The validator expands it from the catalog definition. A plan never contains a metric's SQL or formula. Metric nodes are allowed only where an aggregate is allowed (aggregate stages and `having`), and only when the stage's `from` is `"source"`. Every table the metric references must be one of the plan's `sources` tables.
- Only metrics with status `approved` in the catalog are valid. A `proposed` metric is rejected with `unsupported` and the user is asked to accept it through the UI (§6.1 of the requirements). A name that is not in the catalog at all is rejected with `invalid_plan`. A metric whose referenced table is not among the plan's `sources` is rejected with `invalid_plan`.
- Metric resolution is done by the catalog, not by the planner. The planner's `intent` text is never used to decide a metric.

Relationship and join rules:
- If `joins` is disabled and `sources` has more than one entry, the plan is rejected with `unsupported`.
- Each `joins[i].right` must equal `sources[i+1].alias`. The parser enforces this, so joins follow the source order.
- Each join's `on` must be a single `compare` node with `op: "="`. Each operand is either a `column` node or a `normalize` node. A `normalize` node wraps one `column` and an ordered list of allowlisted operations (`lower`, `trim`, `cast` to one of `BIGINT`, `VARCHAR`, `DOUBLE`, `DATE`). Nothing else is allowed inside a join `on`. One operand must use the join's `right` alias and the other an earlier alias.
- The pair (table, column, normalization ops) for each operand must match an enabled relationship (`Catalog.enabled_relationships`, a property) in either direction. The match is exact: the relationship endpoint's normalization list must equal the plan's list for that side, in order. A plain `column` operand on an endpoint that has normalization is rejected with `unsupported`, because a raw comparison silently matches no rows. A `normalize` operand on an endpoint with no normalization is also rejected. No match is rejected with `unsupported`, because the join is not configured.
- A `normalize` node is not a general expression. It is only valid as a direct operand of a join `on`. Filters, stage columns, and metrics must not use it. The planner prompt says so.
- Join cardinality determines grain. `Relationship.cardinality` is stated child-to-parent: `many_to_one` means many child rows per parent row. When a join reaches a child from its parent (the parent alias is already in the plan and the child alias is the join's `right`), each parent row is repeated once per matching child row. An aggregate whose input reads a column of that parent alias is then rejected with `invalid_plan`, because the parent value would be counted once per child row. Aggregates over the child alias and plain projections of the parent are allowed.
- The rule is direction-aware. Joining from child to parent never multiplies rows and is allowed. A relationship with cardinality `unknown` or `many_to_many` is treated like `many_to_one` in the fan-out direction, so parent-side aggregates over it are rejected.

Capability rules. Flags come from `config.yaml` `capabilities`. A missing key counts as disabled.
- `windows` gates `window` stages and `window` nodes.
- `calculations` gates the `arith` and `safe_divide` nodes. A `project` stage whose columns are plain `column` nodes needs no flag.
- `case_expressions` gates `case` nodes.
- `date_grains` gates `date_trunc` and `date_diff` nodes.
- `group_by` gates a non-empty aggregate `group_by`.
- `having` gates an aggregate `having`.
- `order_by` gates a non-empty `final_output.sort`.
- `joins` gates multi-source plans.

Failure policy:
- Unknown identifiers, unknown stage references, and invalid metric names are `invalid_plan`.
- Disabled features and unapproved metrics are `unsupported`.
- A `final_output.columns` name that exists in more than one source (with `stage: "source"`) is ambiguous and fails closed with `invalid_plan`. The message names the candidate aliases.
- The validator never rewrites a plan to make it valid. A rejected plan stops the pipeline (§10).

### 5.7 Planner repair retry (decided)

Planner output varies between calls, so a single well-formed plan can fail on one small shape error. The pipeline allows one repair attempt for that case only.

- Trigger: `parse_planner_output` or `validate_plan` raises `PlanError` (category `invalid_plan`) on the first planner reply. Other outcomes never retry: `needs_clarification`, `cannot_answer`, and `LlmError` stop as before.
- Request: one second call to the same planner LLM. The prompt is the original planner prompt, followed by the model's previous reply and the bounded error reason (dotted path and message). It asks for one complete corrected plan under the same rules, and nothing else.
- Limit: at most one repair call per request. If the repaired reply also fails parsing or validation, the request stops with `invalid_plan` and the reason from the second failure.
- Equal checks: the repaired reply goes through the same parser and validator, with no shortcuts. The repair never changes the plan contract, never adds fields, and never rewrites a plan in code.
- Trace: the repair is recorded inside the existing `planner_llm` stage (stage 6), which reports `attempts: 1` or `2`. Stage names and the stage list are unchanged. Stages 7 and 8 record the final attempt only.
- Bounds: the error reason sent back is the same bounded string the trace stores. The previous reply is sent as-is, within `MAX_PLANNER_OUTPUT_CHARS`.

## 6. SQL generation contract

### 6.1 Prompt inputs (D1)

`build_sql_prompt(question, plan, catalog, capabilities)` in `src/query/prompts.py` builds the prompt. It accepts only a validated `ready` plan and draws every input from the catalog, never from free text.

- Original user question (bounded by the planner intent limit).
- The validated plan, serialized as JSON with sorted keys.
- Physical metadata limited to the source tables named in the plan, and only the columns that the plan's `column` nodes reference. Each column lists its type.
- Semantic metadata for those columns only: column class, data type, and synonyms.
- Definitions of approved metrics that the plan references (`metric` nodes in stages, filters, or `having`). The raw definition is included unchanged, so the generator must reproduce it exactly.
- Enabled relationships whose child and parent tables are both plan sources, with cardinality, join policy, and fan-out note. No other relationships are listed.
- Enabled capability names from configuration and the DuckDB dialect instructions.
- Strict output contract (section 6.2).
- Rules: preserve the plan's output grain and metric definitions. Do not add tables, columns, joins, filters, metrics, or output names that the plan does not contain. Every string or boolean value from the plan must be a positional `?` placeholder, with its value in `params`. Numeric values may be written inline (user decision). The final `limit` and a window `offset` are plain integers, since the plan parser validates them as structural integers. Every stage except the final output is a CTE; the final output is the outer SELECT (no CTE). A single-stage plan is one plain SELECT with no CTE. A `GROUP BY` item that contains a `?` placeholder is written as its select position (`GROUP BY 1`). `lower` or `trim` is written only inside a join condition, and only where the plan's join key is a `normalize` node, in the plan's order.
- Planner rules: a join key on an endpoint with listed normalization must be a `normalize` node with exactly the listed ops; a plain column is used only where none is listed. The relationship context lists each endpoint's normalization, so the planner can copy it.

The prompt contains no dataset- or question-specific guidance. Output is deterministic for identical inputs.

### 6.2 Output schema

```json
{
  "sql": "WITH ... SELECT ...",
  "params": ["Furniture", "2024-01-01", "2025-01-01"],
  "assumptions": []
}
```

Use positional `?` placeholders consistently with DuckDB's parameter binding. The parser validates exact output keys and parameter type/count. No extra prose. If the chosen DuckDB interface uses a different placeholder style, document it once and test it end-to-end.

Contract details (D1):
- The response is exactly one JSON object with exactly the keys `sql`, `params`, and `assumptions`.
- `sql` is one `SELECT` or `WITH` statement in DuckDB SQL.
- `params` is a list of scalars (string, number, boolean, or null). Their order matches the `?` placeholders in `sql`, left to right.
- `assumptions` is a list of strings describing interpretations the generator made. It is empty when there are none.
- Any other shape, including prose, markdown outside one fence, or extra keys, is rejected by the D3 parser.

### 6.3 Deterministic fast path (decided: omitted)

The optional deterministic builder is omitted (D4 decision recorded in C1). Every plan goes through the SQL Generator, and every generated SQL goes through the same AST validation (section 7) and plan-consistency checks (task D6). Simple questions therefore use the same path as complex ones, and no question is silently simplified. Shared identifier and parameter helpers are not added ahead of use. They are introduced in D5 and D6, the first tasks that compare identifiers and parameters, and are tested there.

### 6.4 SQL Generator client (D2)

`src/query/sql_generator.py` makes the SQL Generator call. It is separate from the planner call.

- `build_sql_generator(registry, profile_name)` builds a client from `registry.sql_generator_llm` only. The planner settings are never reused, so the two calls can differ in model and limits.
- `generate_sql(client, prompt, on_chunk=None)` makes one call with the D1 prompt and returns `(raw_text, StageResult)`. The stage name is `sql_generate`. Chunks go to `on_chunk` for the UI stream, and are not kept in the trace.
- The trace output is `{"characters": n}` only. The generated SQL and any prompt text are not stored in the trace or logs.
- A provider failure becomes a failed stage with category `llm_error` and a reason naming only the exception type (from `LlmError`). Nothing is parsed here. Parsing and validation belong to D3 and D5.
- An empty response is still returned as text. D3 rejects it.

### 6.5 Generated SQL parser (D3)

`src/query/sql_parser.py` turns the raw SQL Generator text into `(sql, params, assumptions)`. It runs before AST validation (section 7). Malformed output fails here and never reaches DuckDB.

Input handling, in order:
1. Input must be text. It must not exceed `MAX_SQL_OUTPUT_CHARS` (20000) characters.
2. Surrounding whitespace is removed. Empty input is rejected.
3. If the text contains a triple backtick, it must be exactly one fenced block (optional `json` tag) with nothing outside it. Otherwise the whole text must be the object.
4. The body must decode to exactly one JSON object with no trailing content. Duplicate keys, arrays at the top level, scalars, and the constants `NaN`, `Infinity`, and `-Infinity` are rejected.

Shape checks:
- The key set must equal exactly `{sql, params, assumptions}`. Missing or extra keys are rejected.
- `sql` is a non-empty string after trimming. Its length limit is enforced later by D8, not here.
- `params` is a list. Each item is a string, boolean, `null`, or a finite number. Lists, objects, and other types are rejected.
- `assumptions` is a list of strings. Non-string items are rejected.

Placeholder check:
- The parser counts positional `?` placeholders in `sql`. It skips `?` inside single-quoted string literals (with `''` escapes), double-quoted identifiers, `--` line comments, `/* */` block comments, and dollar-quoted strings (`$$...$$` or `$tag$...$tag$`).
- The number of placeholders must equal `len(params)`. Any mismatch is rejected.
- Ordering is positional. The `?` tokens bind to `params` left to right, so there is nothing else to match at this stage. D5 checks the parameter nodes in the AST as a second count.

Failures raise `SqlParseError` (a `ValueError` subclass) with a path and reason. They map to category `invalid_sql` in the trace. The reason names the rule that failed and never echoes the SQL text.

## 7. SQL AST validation

Parse SQL with DuckDB's parser/serialization facilities and/or a maintained parser that can represent the DuckDB dialect. If using a parser that does not understand a supported DuckDB feature, fail closed and document the limitation.

Validation is recursive and must inspect:
- root statement and all statement nodes
- CTE definitions and references
- subqueries and correlated references
- every join and its predicate
- each table scan and column reference
- function calls
- window clauses and frames
- set-operation branches
- `ORDER BY`, `GROUP BY`, `HAVING`, `QUALIFY` if enabled
- parameter placeholders and literals

Validation steps:
1. Parse exactly one statement.
2. Ensure read-only query root.
3. Reject forbidden statements/functions/extensions/external scans.
4. Resolve every physical table and column against the selected catalog, accounting for aliases and CTE output columns.
5. Validate joins against plan edges and relationship metadata.
6. Validate functions/operators against enabled capabilities.
7. Check query semantics against the plan: expected sources, filters, output metrics/dimensions, grain, and calculations.
8. Verify parameters, count, order, and data types; user values remain parameters.
9. Enforce complexity limits and output limits.
10. Return structured findings, not just a boolean.

The validator must not trust comments, aliases, CTE names, quoted identifiers, or LLM explanations. A CTE must not hide a forbidden table access.

### 7.1 Implemented rules (D5)

Parsing:
- SQL is serialized with DuckDB's `json_serialize_sql` on an in-memory connection after `LOAD json`. The serializer only parses. It never opens a data file, so nothing from the query touches disk during validation.
- A DuckDB parser error maps to `invalid_sql`. Any other serialization error (for example a non-SELECT statement such as `CREATE`, `ATTACH`, `EXPLAIN`, or `SET`) maps to `unsafe_sql`.
- The result must contain exactly one statement. Its root node must be `SELECT_NODE` or `SET_OPERATION_NODE`. WITH is represented as `cte_map` on the root, so it needs no separate root type.

Node walk (recursive, through every dict and list, including CTE bodies, subqueries, join sides, and set branches):
- Any dict with a string `type` must be in the node allowlist. An unknown type fails closed as `unsafe_sql` and the reason names the type.
- The allowlist covers only structural nodes DuckDB produces for the plan grammar: select, set operation, base table, subquery, join, star, column reference, constants, parameters, functions, `CASE`, comparisons, `BETWEEN`, `IN`, boolean conjunctions, casts, `COALESCE`, `IS [NOT] NULL`, window nodes, order and limit modifiers, and type descriptors.
- `EMPTY` is allowed only as the FROM clause of a FROM-less SELECT (for example `SELECT 1`).
- `TABLESAMPLE` can sit on a `SELECT_NODE` as well as on a `BASE_TABLE`, so both are checked.
- Table functions (`read_csv`, `unnest`, and so on), `PIVOT`, and `VALUES` lists are not on the allowlist, so they are rejected by the same rule.

Tables:
- Every `BASE_TABLE` must be either a CTE defined in the same statement (matched by name, so a CTE can shadow a physical table) or a table in the selected catalog.
- `catalog_name` must be empty. `schema_name` must be empty or equal to the catalog's schema.
- `TABLESAMPLE` (`sample`) and time travel (`at_clause`) must be absent.
- File paths used as table names (for example `'x.csv'`) are not catalog tables and are rejected.
- Rejection reasons never echo table names from the SQL text.

Functions:
- Every `FUNCTION` must have a name in the function allowlist. The allowlist is the set the plan grammar can produce: `sum`, `avg`, `count`, `count_star`, `min`, `max`, `median`, `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `date_trunc`, `date_diff`, `nullif`, `+`, `-`, `*`, `/`, and `quantile_cont` for percentiles. D7 gates each family by capability; D5 only decides whether a name is known at all.

Parameters:
- Each `VALUE_PARAMETER` node is counted. The count must equal `len(params)` from D3, and the identifiers must be positional numbers. A mismatch maps to `invalid_sql`. This is the second count from D3 (see §6.5).

Scope narrowing (recorded deliberately):
- Join-only functions: `lower` and `trim` are not in the general function allowlist. They are allowed only inside a join condition (`from_table.condition` of a `JOIN` node), so the same call in a WHERE, SELECT, or CTE is rejected as an unknown function. Casts (`OPERATOR_CAST`) keep the general node allowlist from D5. The plan grammar only produces casts inside a join `normalize` node, and the D6 check (§7.2, step 4a) enforces that match.

Full column resolution against aliases and CTE output columns is not done in D5. DuckDB resolves column names at bind time, before any row is returned. D6 then checks the final output names with `SELECT * FROM (sql) LIMIT 0`. Column existence is therefore enforced by the database and by D6, not by a separate scope resolver in D5. This does not weaken table safety, because table access is fully resolved above.

Result:
- A successful check returns a summary (tables read, CTE names, function names, parameter count) for D6. A failure raises `SqlAstError` with category `unsafe_sql` or `invalid_sql`, a path, and a reason.

### 7.2 Plan-to-SQL consistency (D6) (superseded)

Superseded by the flat flow (§5.0). The plan-to-SQL module `src/query/sql_consistency.py` was removed. Its name and value checks are now in `src/query/sql_checks.py`. The text below is historical.

Historical design: it was implemented in `src/query/sql_consistency.py`. It takes the validated plan, the catalog, the generated SQL with its params, and the D5 summary. It re-reads the root tree and never changes the query. Any mismatch raises `SqlAstError` with category `invalid_sql` and a path and reason. Reasons never echo SQL text or parameter values.

Checks, run in this order:
1. Stage CTEs: the CTE names, in order, equal `[stage.id for stage in plan.stages]`. No other CTE is allowed.
2. Stage bodies, for each CTE body (`cte_map` entry `value.query.node`):
   - FROM: when `stage.source` is `"source"`, the body reads one planned source table. Otherwise it reads the CTE named by `stage.source`.
   - Grouping: an aggregate stage with `group_by` has a non-empty GROUP BY, and one without has none. Project and window stages have no GROUP BY.
   - HAVING is present if and only if `stage.having` is set.
   - A window stage contains a `WINDOW_*` node. Aggregate and project stages contain none.
3. Source tables: the set of physical tables read (D5 `summary.tables`) equals the set of planned `sources[].table`.
4. Joins: `summary.join_count == len(plan.joins)`. For each join, the ON predicate's normalization must match the plan. The AST's join condition must contain `lower`/`trim`/`CAST` operations that correspond one-to-one, in order, with the plan's `normalize` operations (step 4a). A `lower`, `trim`, or `CAST` in a join condition that the plan did not ask for is `invalid_sql`.
   4a. Join normalization: the operations on each side of each join's ON comparison equal the plan's normalization ops for that side. Plain column operands on both sides must have no operations.
5. Final select: the root FROM is the final stage's CTE, or the planned source table when `final_output.stage` is `"source"`. A single-stage plan is one SELECT with no WITH. Otherwise each non-final stage is a CTE named by its id, and the final stage is the outer SELECT. The root modifiers match the plan:
   - With `sort`: one ORDER_MODIFIER with exactly `len(sort)` orders, each direction matching (`ASCENDING` or `DESCENDING`). Without `sort`: no ORDER_MODIFIER.
   - With `limit`: one LIMIT_MODIFIER whose value is an inline integer constant equal to `limit`. The limit is a structural integer that the plan parser already validates, not a user value, so it stays inline (§6.1). Without `limit`: no LIMIT_MODIFIER.
6. Literal params: the multiset of normalized plan literals equals the multiset of normalized params. Plan literals are every `literal` node in filters, stage columns, `group_by`, `having`, and join `on`, plus each `in` value. Null is included. Normalization gives `(kind, value)` with kinds `null`, `bool`, `number` (compared as float), and `text`. Booleans never equal numbers. The limit and window `offset` are structural integers and are not in this multiset (see step 5 and §6.1). This check runs before step 7 because it is pure and cheap.
7. Output columns: run `SELECT * FROM (<sql>) LIMIT 0` on a read-only connection to `catalog.database_file`, with the params. The result names must equal `final_output.columns` in order. A bind or execution error is `invalid_sql`. This is where column existence is enforced (see §7.1).
8. Approved metrics (partial): for each `metric` node in the plan, the columns that its catalog definition reads must appear as `COLUMN_REF` names in the SQL. Those columns come from `expression`, `numerator`, `denominator`, `value`, and `weight`, depending on the kind.

Limits recorded deliberately:
- Positional order of the `?` values is not verified. The AST does not show which value binds to which placeholder, so only the multiset is checked. A complete but misordered list passes D6. Execution tests in E and F catch it.
- Filter placement is covered only through literal values and the table set. A filter placed in the wrong stage is not detected.
- Grain is enforced by C6 and the output-column check, not re-derived here.
- Metric checking confirms that the definition's columns are referenced. It does not prove the formula is identical. Metric filter literals come from the definition, not the plan, so they are not in the param multiset.
- `LIMIT 0` may still plan or scan on DuckDB. It is read-only, and the D8 timeout applies.

### 7.3 Capability gating of SQL features (D7)

Implemented in `src/query/sql_ast.py`, inside the same tree walk as D5. D5 says whether a construct is known. D7 says whether the construct is enabled for this dataset. Detection is done on the SQL tree, not the plan. A feature the SQL uses is rejected even when the plan did not ask for it.

Each detected feature maps to one flag in the `capabilities` block of `config.yaml`:

| SQL construct (DuckDB tree) | Flag | Default |
|---|---|---|
| `cte_map` entries on a statement | `ctes` | true |
| `SUBQUERY` anywhere (derived table, scalar, EXISTS, IN) | `subqueries` | false |
| `SET_OPERATION_NODE` (UNION, INTERSECT, EXCEPT) | `set_operations` | false |
| `JOIN` | `joins` | true |
| non-empty GROUP BY | `group_by` | true |
| `HAVING` | `having` | true |
| `ORDER_MODIFIER` | `order_by` | true |
| `CASE_EXPR` | `case_expressions` | true |
| `CASE_EXPR` inside an aggregate argument | `conditional_aggregation` | true |
| `WINDOW_*` nodes | `windows` | true |
| `WINDOW_ROW_NUMBER`, `WINDOW_RANK`, `WINDOW_RANK_DENSE` | `ranking` | true |
| `date_trunc`, `date_diff` | `date_grains` | true |
| `+`, `-`, `*`, `/`, `nullif` | `calculations` | true |
| `quantile_cont` | `percentiles` | false |

Always allowed, with no flag: `sum`, `avg`, `count`, `count_star`, `min`, `max`, `median`, and `LIMIT`. These are the basic family in requirement §10.

Rules:
- A flag that is missing from `capabilities` counts as disabled, matching `plan_validator._enabled`.
- A disabled feature raises `SqlAstError` with category `unsupported`. The reason names the flag (for example, "SQL feature 'subqueries' is not enabled"). It never echoes SQL text.
- `validate_sql_ast(sql, catalog, param_count, capabilities)` takes `capabilities` as a required argument. `AstSummary` gains `features`, the sorted tuple of flags the SQL used, for logging in E3.
- The function allowlist is aligned to §7.1. D7 removes the names the code had and the design did not list: `%`, `abs`, `round`, `lower`, `trim`, `||`, and `~~`. The plan grammar cannot produce them (`ARITH_OPS` is `+`, `-`, `*`), and no capability covers text functions.
- `CASE_EXPR` in an aggregate sets both `case_expressions` and `conditional_aggregation`. Both must be enabled for that SQL to pass.

Config changes:
- `config.yaml` gains `ctes: true`, `subqueries: false`, `set_operations: false`, `conditional_aggregation: true`, `ranking: true`, and `percentiles: false`. Existing flags are unchanged. `config.py` reads the block as a dict and needs no code change.

Consequences:
- Multi-stage plans (CTE per stage) pass. Derived tables, scalar subqueries, EXISTS, UNION, and quantiles are rejected until their flags are enabled. Any benchmark question that needs them is reported as `unsupported`, and the query logic is not changed for it.

### 7.4 Complexity and parameter limits (D8)

Limits are read from the `limits:` block of `config.yaml` (§3.1), and the code does not hard-code them. Each limit is checked in the order below, and the first failure stops validation. Rejections use the category named here and never echo SQL text or values.

Settings (all required, all positive integers):
- `max_sql_chars` = 20000. This is the one SQL length value. §6.5 already caps raw parser input at `MAX_SQL_OUTPUT_CHARS`, so the parser reads `max_sql_chars` from config instead of its own constant. Both paths enforce the same value. Over the limit: `invalid_sql`.
- `max_ctes` = 20. Counted from the `cte_map` of every SELECT in the statement. Over the limit: `unsupported`.
- `max_nesting_depth` = 12. Depth of nested query nodes (`SELECT_NODE` inside a CTE, derived table, or subquery). The root statement is depth 1. Over the limit: `unsupported`.
- `max_joins` = 12. Equal to the join count from §7.1. Over the limit: `unsupported`.
- `max_window_functions` = 20. Count of `WINDOW_*` nodes. Over the limit: `unsupported`.
- `max_result_rows` is not a `limits` key. It is `retrieval.default_row_limit` = 1000. `query_timeout_seconds` = 30 is in `limits`. Both apply at execution (§8). D8 only supplies the values and tests the settings. E1 enforces them.

Parameter and constant rule (flat flow, current):
- Every `VALUE_PARAMETER` must be positional. The number of `?` must equal the number of params.
- Inline constants are allowed everywhere: strings, numbers, dates, booleans, and `NULL`. The generator may write them inline or as `?` placeholders. Inline strings are not forced into `?` (user decision).
- `check_sql_limits` bounds only structure and resource use. The final `LIMIT` must be an integer from 0 to `max_result_rows`. A window `lag`/`lead` offset must be from 0 to 1000. Grains are not allowlisted here; the AST function allowlist (`date_grains` capability) gates the function names.
- `check_literal_values` (`src/query/sql_checks.py`) checks a string value compared with a column that lists its values. It applies to both `?` parameters and inline strings, matched without regard to case.
- Injection defense: the single-statement AST check, the function and table allowlists, the dataset-scoped column checks, and the listed-value checks. String values are no longer forced into placeholders, so the defense does not rely on placeholders.
- A rejected SQL reply (disallowed function, unknown column, unlisted value, or a limit over the cap) gets one repair call that receives the rejection reason. Multi-statement, write, and dataset-violating SQL is never repaired.
- A rejected SQL reply (plan mismatch, disallowed function, extra join or column, or a constant rule) gets one repair call that receives the rejection reason (`pipeline._repair_generated_sql`). Multi-statement, write, and dataset-violating SQL is never repaired.

Algorithm (D8 adds this as the step between D5 and D6):
1. Serialize SQL, check `len(sql) <= max_sql_chars`.
2. Walk the tree once and collect CTE count, nesting depth, join count, window count, and the constant nodes with their parent node type.
3. Check the limits in the order above.
4. Check each constant against the allowed positions. A constant is allowed only when its parent is one of: the `LIMIT_MODIFIER`, a `WINDOW_LAG`/`WINDOW_LEAD` offset or default, a `FUNCTION` named `nullif` whose value is `0`, a `date_trunc`/`date_diff` first argument that is a grain, or an in-range ordinal under `GROUP BY` or `ORDER BY`.

Order in the D5 -> D6 pipeline: D8 limits run after D5 AST validation and before D6 consistency, so that size and depth are checked before the expensive `LIMIT 0` execution in D6.

## 8. Query execution

- Open the configured DuckDB database in read-only mode.
- Bind parameters through the DuckDB driver.
- Enforce query timeout/resource limits where available.
- Set a maximum number of returned rows.
- Do not mutate the database or connection settings from generated SQL.
- Return a safe runtime error and log diagnostic details without secrets.
- For large outputs, truncate only the displayed/result rows under an explicit cap and indicate truncation.

## 9. Pipeline and trace model

Use the following logical stages (the exact UI may group adjacent panels, but every stage must remain traceable):

| # | Stage | Responsibility |
|---:|---|---|
| 1 | Submit | Validate non-empty question and registered database selection |
| 2 | Resolve database | Validate registry, file paths, and selected database identity |
| 3 | Load catalog | Load/cache normalized physical, semantic, and relationship metadata |
| 4 | Retrieve context | Select relevant tables, columns, definitions, and relationship paths |
| 5 | Build planner prompt | Create prompt and enforce size limits |
| 6 | Planner LLM | Call Planner; stream chunks where supported |
| 7 | Parse planner output | Parse strict JSON and schema version |
| 8 | Validate plan | Resolve references, grain, relationships, and capabilities |
| 9 | Build SQL prompt | Supply validated plan and relevant metadata |
| 10 | SQL Generator LLM | Generate SQL JSON; stream chunks where supported |
| 11 | Parse generated SQL | Validate response JSON, SQL string, parameters |
| 12 | Validate SQL AST | Recursively validate read-only query and semantics |
| 13 | Execute | Execute parameterized SQL read-only |
| 14 | Format response | Shape result, status, trace, and safe errors |

Every stage produces:

```python
{
    "stage": "validate_sql_ast",
    "status": "ok",  # ok | failed | skipped
    "output": {},
    "reason": None,
    "duration_ms": 0
}
```

Final response:

```python
{
    "status": "ok",  # ok | needs_clarification | cannot_answer | rejected | error
    "trace": [],
    "result": {
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False
    },
    "sql": None,
    "params": [],
    "reasons": []
}
```

Never include credentials in `StageResult`, prompts, model output, or executor arguments.

### 9.1 Failure categories and trace bounds (B5)

Every `failed` stage and every non-`ok` final response carries one failure category:

| Category | Used for |
|---|---|
| `invalid_request` | Empty question, unknown option |
| `unknown_database` | Database key not in the registry |
| `metadata_error` | Missing file, identity mismatch, validator failure |
| `clarification_needed` | Planner or semantic resolver asks for clarification |
| `unsupported` | Planner `cannot_answer`, unsupported capability |
| `llm_error` | Provider call failed or returned unusable output |
| `invalid_plan` | Plan schema or validation failure |
| `invalid_sql` | SQL JSON, parameter, or syntax failure |
| `unsafe_sql` | AST validator rejected the statement |
| `execution_error` | DuckDB runtime error |
| `internal_error` | Unexpected exception (message redacted, traceback never shown) |

Trace bounds: each `StageResult.output` is serialized and truncated to `MAX_OUTPUT_CHARS` (2000 characters) with a `"...[truncated]"` marker. `reason` is truncated to `MAX_REASON_CHARS` (500). Results rows are capped by `default_row_limit` before they reach the trace.

Logging policy: one structured record per stage with `stage`, `status`, `duration_ms`, and `category`. Raw prompts and LLM output are not logged by default. Credentials, configuration secrets, and stack traces are never logged in the trace or the user-facing response.

### 9.2 Stage names, status mapping, and non-ready plans (E2)

The current stage names and status mapping are in §5.0. The table below is historical: the plan stages it names (`parse_planner_output`, `validate_plan`, `validate_sql_ast`) were replaced by `parse_analysis`, `validate_sql`, and the deterministic checks.

Historical stage names in the trace were the snake_case names of the table above: `submit`, `resolve_database`, `load_catalog`, `retrieve_context`, `build_planner_prompt`, `planner_llm`, `parse_planner_output`, `validate_plan`, `build_sql_prompt`, `sql_generator_llm`, `parse_generated_sql`, `validate_sql_ast`, `execute`, `format_response`. Stage 12 runs D5 AST validation, then D8 limits, then D6 consistency, as one stage in that order. Stage 6 and stage 10 record the LLM call under those names, even though the generator returns the stage name `sql_generate`.

Stage-to-category mapping. The pipeline catches these errors and records a failed stage with the category shown:

| Stage | Error | Category |
|---|---|---|
| `submit` | empty question or unknown option | `invalid_request` |
| `resolve_database` | unknown database key | `unknown_database` |
| `load_catalog` | `MetadataMismatchError`, `ConfigError`, `MissingFileError` | `metadata_error` |
| `retrieve_context` | `ContextTooLargeError` | `unsupported` |
| `build_planner_prompt` | prompt size limit | `unsupported` |
| `planner_llm`, `sql_generator_llm` | `LlmError` | `llm_error` |
| `parse_planner_output` | `PlanError` | `invalid_plan` |
| `validate_plan` | `PlanValidationError` | its own category (`invalid_plan` or `unsupported`) |
| `validate_plan` | plan status `needs_clarification` | `clarification_needed` |
| `validate_plan` | plan status `cannot_answer` | `unsupported` |
| `build_sql_prompt` | `ValueError` from prompt builder | `invalid_plan` |
| `parse_generated_sql` | `SqlParseError` | `invalid_sql` |
| `validate_sql_ast` | `SqlAstError` | its own category (`invalid_sql`, `unsupported`, or `unsafe_sql`) |
| `execute` | `ExecutionError` | `execution_error` |
| any stage | any other exception | `internal_error`, reason redacted |

Non-ready plans. The planner may return `needs_clarification` or `cannot_answer`. Those are valid plans and parse without error. Stages 5 through 7 pass them through unchanged. Stage 8 (`validate_plan`) is the only stop point for them. It checks the plan status first and does not run the reference validator on a non-ready plan, so a clarification request is not rejected for references the planner could not resolve. It fails with the category in the table above, and the plan's clarification message (or a fixed message when none exists) becomes the stage reason. The `missing` list goes into the stage output. Stages 9 through 14 are `skipped`.

Response status from category (the final response status is set by the first failed stage, or by `ok` when no stage failed):

| Category | Response `status` |
|---|---|
| none (all stages ok) | `ok` |
| `clarification_needed` | `needs_clarification` |
| `unsupported` | `cannot_answer` |
| `invalid_plan`, `invalid_sql`, `unsafe_sql` | `rejected` |
| `invalid_request`, `unknown_database`, `metadata_error`, `llm_error`, `execution_error`, `internal_error` | `error` |

`reasons` carries the stage reason, bounded by `MAX_REASON_CHARS`. `sql` and `params` are set once stage 11 passes. `result` is filled only when stage 13 succeeds. Rows are never written to the trace or the logs.

## 10. Error and stop policy

- Invalid request/unknown database/missing file: fail at request or database stage.
- Invalid metadata/schema mismatch: fail before Planner LLM call.
- Metadata ambiguity: clarification or rejection before SQL generation.
- Planner `needs_clarification`/`cannot_answer`: stop and surface reason.
- Invalid plan: after one planner repair attempt (§5.7) fails, reject before SQL Generator.
- Invalid SQL JSON, unsafe SQL, unapproved table/function/join, parameter mismatch: reject before execution.
- DuckDB runtime error: return safe `error`; do not auto-repair and rerun without an explicit separately reviewed retry design.
- On any failure, all later stages are marked `skipped`.

## 11. Workbook ingestion design

Create deterministic ingestion commands, separate from request handling. Each workbook is inspected before loading:
- list sheet names and columns
- infer/normalize types with explicit mapping
- preserve original column labels in metadata
- profile row counts, nulls, duplicates, candidate keys, and grain
- load each relevant sheet into a configured DuckDB database
- record source workbook, sheet, ingestion timestamp/version, and mappings

Dataset notes to validate rather than blindly assume:
- **Sample - Superstore:** likely order-line grain with sales/profit/quantity/discount and order/customer/product/date fields. Confirm exact headers and duplicates.
- **Airbnb:** `AirBnB_NYC` sheet; columns include host/listing characteristics, room type, neighbourhood, price, beds, reviews, and ratings. It appears to be listing-level but must be profiled.
- **Netflix:** main `netflix_titles` table and director/country/cast/category relationship sheets keyed by `show_id`; many-to-many or one-to-many joins can duplicate titles.
- **113th Congress:** `Bills` and `Sunlight Info`; profile `Source URL` and other candidate keys before defining a join. The Bills sheet has vote-related fields and may be at vote/record grain rather than one row per bill.

Do not join sheets simply because names look related. Create relationship metadata only after validating key uniqueness and intended grain.

### 11.1 Source-to-physical mapping (A3)

| Source element | Physical target | Rule |
| --- | --- | --- |
| Workbook file stem (`data/<name>.xlsx` or `.xls`) | `databases/<name>.duckdb` file and schema `<name>` | `normalize_identifier` (non-word characters become `_`, leading digit gets `_` prefix). Two workbooks that normalize to the same name are reported as a conflict, not merged. |
| Sheet name | Table `<sheet>` in schema `<name>` | `unique_names` (same normalization; duplicates get `_2`, `_3`). Empty sheets (no header) are skipped and reported. |
| Column header | Column in the physical table | Same normalization as sheet names. Duplicate headers get `_1`, `_2`. The original label is kept in `physical_mapping` YAML (`source_sheet`, `column`). |
| Column values | DuckDB type inferred by pandas and DuckDB | Columns with mixed Python types are stored as `VARCHAR`. Integer-valued floats in mixed columns are written without `.0`. Type choices appear in the physical YAML. |

Rebuild semantics: each run replaces the workbook's schema from scratch (`DROP SCHEMA ... CASCADE`, then recreate). A sheet removed from a workbook is therefore removed from the database on the next run. Re-running on unchanged input yields the same table names, column names, types, ordinal positions, and row counts.

## 12. Evaluation design

`evaluation_questions.md` is the benchmark specification. A benchmark manifest should include:
- question ID and exact natural-language prompt
- selected database
- expected output grain and columns
- SQL capability tags
- required metadata/relationships
- expected result or oracle derivation
- assumptions/caveats
- status and failure category

Use a three-way split:
- development questions for design and debugging
- validation questions for capability regression
- held-out questions for generalization

Do not tune production code to question IDs. For source-dependent questions, independently calculate expected outputs with transparent reference SQL or manually verified computations. Use synthetic fixtures when workbook data cannot exercise an edge case.

### 12.1 Benchmark runner

`src/evaluation/benchmark_runner.py` runs the 30 questions through `run_pipeline`.
- Question text is read at runtime from `specs/retrival/evaluation_question.md`. The manifest in `tests/benchmark/manifest.yaml` supplies only the database and status. Prompts are never copied into the manifest. The runner refuses to start if the two ID sets differ.
- Modes: `mocked` uses an offline client that declines every plan without calling a model. It checks runner and report wiring only and says nothing about answer quality. `live` uses the configured Bedrock planner and SQL generator and needs `AWS_PROFILE`. Live runs incur Bedrock cost.
- Outcome per question is compared with the manifest status:
  - `as_expected`: the response status is in the allowed set for that status (answerable → `ok`; clarification → `needs_clarification`; needs_config and profile_first → `needs_clarification` or `cannot_answer`; gap → `cannot_answer`).
  - `not_answered`: an answerable question did not return `ok`.
  - `unexpected_answer`: a question that should not be answered returned `ok`. This is a false answer.
  - `unexpected_status`: any other mismatch.
  - `harness_error`: the runner caught an exception for that question.
- Oracle verdict applies only to `ok` responses with an oracle. The result is compared with each oracle part; the first matching part passes. On failure, the closest part is reported. `column_mismatch` means names or order differ. `value_mismatch` means values differ.
- Each run writes one JSON report, `eval_results/benchmark-<mode>-<UTC timestamp>.json`. It holds the run metadata, models, summary counts, and one record per question with SQL, params, stage trace, timings, verdict, and a preview of up to 20 rows.
- Exit code is 1 only when a harness error occurred. Wrong answers do not change the exit code, because the benchmark reports them.

## 13. Non-functional constraints

- Python 3.10+ (unless the existing project explicitly pins a compatible version).
- DuckDB local files.
- Streamlit UI.
- Tests runnable offline with mocked LLM clients.
- Ruff or the project's existing linter.
- Deterministic ingestion and unit tests.
- No new service/database/architecture layer unless a demonstrated requirement justifies it.
