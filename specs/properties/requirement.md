# Requirement: Generate Physical and Semantic YAML

## Purpose

Turn the per-schema requirement markdown into the YAML files that the retrieval layer reads. The markdown is the reviewed source. The YAML is generated and must not be edited by hand.

## Inputs

| Input | Location | Written by |
|---|---|---|
| Physical requirement per schema | [physical_requirements/](physical_requirements/) (`<schema>.md`) | Rendered from the DuckDB files and the approved relationship list |
| Semantic requirement per schema | [semantic_requirements/](semantic_requirements/) (`<schema>.md`) | Reviewed by hand |
| Manual synonyms (optional) | `semantic_mapping/synonyms.yaml` | Written by hand. The script never creates it |

## Outputs

| Output | Location |
|---|---|
| Physical YAML per schema | [physical_mapping/](physical_mapping/) (`<schema>.yaml`) |
| Semantic YAML per schema | [semantic_mapping/](semantic_mapping/) (`<schema>.yaml`) |

The schema name is the file stem. `sample_superstore.md` produces `sample_superstore.yaml` in both folders.

## How to run

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m src.mapping.requirements_to_yaml
```

The script is in `src/mapping/requirements_to_yaml.py`. It has no arguments and needs no database connection. It reads only markdown and YAML.

## What the script does

### Physical step

For each `physical_requirements/<schema>.md`:

1. Read the title. The schema name in the title must match the file name.
2. Read the **Source** section: database file, workbook, and database and schema name.
3. Read the tables table: source sheet, row count, and column count.
4. Read each table's columns: type, nullable, has NULLs, and unique.
5. Read the relationships section. Each relationship needs a name, status, meaning, cardinality, enforceable flag, child table and column, parent table and column, issues, and checks.
6. Write `physical_mapping/<schema>.yaml`.

### Semantic step

For each `semantic_requirements/<schema>.md`:

1. Read the title and the schema mapping, plus the numeric operators.
2. Read each column's class, data type, and flags.
3. Read the categorical value lists. A categorical list may hold at most 30 values, and its count must match the stated count.
4. Read the Boolean sections. Each one needs a true side and a false side. The true side must name a listed stored value. The false side must name a listed value or `none`.
5. Read the numeric range rules for numeric columns.
6. Set `date_operators` on every `DATE` or `TIMESTAMP` column, from the physical data type: `year`, `month`, `year_month`, and `date_between`. The markdown does not list date columns, so no date column is written by hand.
7. Apply synonyms from `synonyms.yaml`, if the file exists. Each entry names a schema, table, column, and terms. The script rejects unknown schemas and unknown columns.
8. Write `semantic_mapping/<schema>.yaml`.

### Cross-check

Before writing either file for a schema, the script checks that the physical and semantic files list the same set of `(table, column)` pairs. It also checks that the schema name matches the file name. A mismatch stops the run.

## Failure behavior

- Any error stops the run with exit code 1 and a message naming the file.
- When one schema fails, the script writes nothing for any schema. Outputs are written only after every schema has been parsed and checked.
- If the physical and semantic requirement files do not list the same schema names, the run stops before parsing.

## Verification

- Rerunning the script must produce byte-identical YAML.
- Each generated table and column name, and each column type, must match the DuckDB file.
- Each categorical value set must match the distinct non-null values in the database.
- Current counts: 4 schemas, 11 tables, 81 columns, 6 relationships. All 6 relationships are approved and inferred, and each one is written as `status: proposed`.

## Out of scope

- Editing generated YAML by hand. Change the markdown and rerun the script.
- Creating `synonyms.yaml`. The user writes it.
- Rendering the markdown files from the database. That step is separate and is not run by this script.
