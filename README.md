# MultiDB: Excel to DuckDB ingestion

Reads every `.xls` and `.xlsx` workbook in `data/` and writes one DuckDB
file per workbook into `databases/`. Each database has one schema named after
the workbook, and each worksheet becomes a table in that schema.

## Configuration

Settings live in `config.yaml` at the project root:

```yaml
input_dir: data
database_dir: databases
```

Relative paths are resolved from the project root, so the app works without
code changes when you edit these values.

## Install dependencies

Requires Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

To run the tests, also install the development requirements, which add
`xlwt` so the tests can create `.xls` files:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Run the ingestion

1. Put `.xls` or `.xlsx` files in `data/`.
2. From the project root, run:

   ```powershell
   .\.venv\Scripts\python.exe -m src.ingestion.main
   ```

The command prints a summary of successful and failed workbooks, plus any
sheet-level errors. It continues past a failed workbook and exits with code 1
if any workbook failed. Rerunning replaces existing table data.

## Query the results

```python
import duckdb

con = duckdb.connect("databases/sample_superstore.duckdb")
con.sql("SELECT * FROM sample_superstore.sample_superstore.Orders LIMIT 5").show()
```

Table references are three-part (`database.schema.table`). DuckDB requires
this because the database name and the schema name are the same.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_excel_to_duckdb -v
```

The tests use temporary directories and small generated workbooks. They do not
touch `data/` or `databases/`.
