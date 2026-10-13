Design --- Excel to DuckDB POC
1. Overview
A small Python 3.12 command-line application reads Excel files from the
data/ directory and loads each workbook into its own DuckDB database
file.
For each workbook: - Create or reuse one DuckDB database file. - Create
or reuse one schema named after the workbook. - Create one table per
worksheet. - Replace each table's data when the workbook is processed
again.
2. Simple Project Structure
excel_to_duckdb/
├── data/                         # Source Excel files
├── databases/                    # Generated DuckDB database files
├── src/
│   └── ingestion/
│       ├── __init__.py
│       ├── main.py               # Command-line entry point
│       ├── excel_loader.py       # Reads Excel sheets into DataFrames
│       └── duckdb_writer.py      # Creates databases, schemas, and tables
├── tests/
│   └── test_excel_to_duckdb.py
├── config.yaml
└── requirements.txt
All application code for this POC must live inside src/ingestion/.
Keep configuration, input data, generated databases, and tests outside
that directory.
- src/ingestion/main.py: reads configuration, finds files, runs
  processing, and prints a summary.
- src/ingestion/excel_loader.py: reads workbook sheets into pandas
  DataFrames.
- src/ingestion/duckdb_writer.py: creates the database file, schema,
  and tables and loads data.
- config.yaml: input and output directory settings.
- data/: source Excel files.
- databases/: generated DuckDB database files.
- tests/: tests using temporary files and directories.
Keep the project to these files; do not add extra layers unless
required.
3. Configuration
Use a small YAML configuration:
input_dir: data
database_dir: databases
Resolve paths relative to the project root, not the current working
directory, so the app can be run from different locations.
4. Processing Flow
1. Run the application using the src.ingestion.main entry point.
2. Read config.yaml.
3. Find .xls and .xlsx files in the configured input directory
   (data/ by default).
4. For each Excel file:
   1. Derive a safe database filename from the workbook filename,
      without its extension.
   2. Read each worksheet into a pandas DataFrame, using the first row
      as column headers.
   3. Open or create the corresponding DuckDB database file.
   4. Create the workbook-named schema if it does not exist.
   5. For each successfully read worksheet, create or replace its
      table with the DataFrame contents.
   6. Close the database connection.
5. Continue with the next workbook if one workbook fails.
6. Print a summary of successful and failed workbooks and any
   sheet-level failures.
5. Naming Rules
- Use the workbook filename without its extension for the database
  filename and schema name.
- Use the worksheet name for the table name.
- Normalize names to valid, predictable identifiers where necessary.
- Handle duplicate column names after normalization so each column
  name is unique.
- Quote identifiers safely when constructing SQL. Never insert raw
  file, sheet, or column names into SQL without identifier quoting.
- If two workbook names normalize to the same database filename,
  report the conflict rather than silently merging their data.
6. Excel Reading
- Support .xlsx using a compatible Excel reader such as openpyxl.
- Support .xls using a compatible reader such as xlrd.
- Use pandas to represent each sheet as a DataFrame.
- Treat the first row as column headers.
- Handle blank cells as missing values.
- Handle empty worksheets without crashing; report them clearly if
  they cannot form a usable table.
7. DuckDB Loading
- Use the Python duckdb package.
- Store each database as a separate .duckdb file under
  database_dir.
- Create the workbook-named schema with CREATE SCHEMA IF NOT EXISTS.
- Write each DataFrame to a table in that schema.
- Replace the table's contents on reprocessing. For this POC, dropping
  and recreating the table is acceptable.
- Use DuckDB's Python API to register DataFrames or otherwise pass
  data safely; avoid building SQL statements from cell values.
8. Error Handling
- If a workbook cannot be read, print the workbook name and error,
  then continue to the next workbook.
- If a sheet fails to load, report the sheet and error. Continue with
  other sheets where practical.
- Close connections in a finally block or a context manager.
- Print a final count of successful and failed workbooks.
9. Testing
Use temporary directories and small generated Excel files. Tests should
cover: - One workbook with multiple sheets. - Multiple workbooks
producing separate database files. - Expected schema and table names. -
Data values loaded correctly. - Reprocessing replaces old table data. -
A bad workbook does not prevent a valid workbook from being processed. -
Both .xlsx and .xls support where the test environment supports
their required reader packages.
10. Out of Scope
- No web interface.
- No scheduling or orchestration.
- No incremental loads or schema evolution.
- No business-specific transformations.
- No remote database server.
- No complex logging or plugin architecture.