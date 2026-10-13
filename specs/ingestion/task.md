Tasks --- Excel to DuckDB POC
Implement these tasks in order. Follow requirements.md and
design.md. All application code must be inside src/ingestion/.
Task 1: Set Up the Project
- Create the project structure described in design.md.
- Create src/ingestion/__init__.py.
- Add src/ingestion/main.py, src/ingestion/excel_loader.py, and
  src/ingestion/duckdb_writer.py as the only application modules
  needed for this POC.
- Add requirements.txt with packages for DuckDB, pandas, YAML
  configuration, and .xlsx/.xls readers.
- Target Python 3.12.
- Add config.yaml with input_dir: data and
  database_dir: databases.
- Add a .gitignore for virtual environments, caches, and generated
  DuckDB files.
Done when: The environment installs dependencies and the
configuration can be read.
Task 2: Read Excel Workbooks
- Implement src/ingestion/excel_loader.py.
- Find .xls and .xlsx files in the configured input directory.
- Read every worksheet into a pandas DataFrame.
- Use the first row as column headers.
- Handle missing values and empty sheets without crashing.
- Report workbook or sheet read errors clearly.
Done when: A workbook with multiple sheets can be read and its sheet
names and data are available to the next step.
Task 3: Create DuckDB Databases and Schemas
- Implement database setup in src/ingestion/duckdb_writer.py.
- Create the output directory if it does not exist.
- Create one .duckdb file per Excel workbook, using the workbook
  filename without its extension.
- Create one schema in each database, using the workbook filename.
- Reuse an existing database file and schema on subsequent runs.
- Safely normalize and quote identifiers.
Done when: Each workbook maps to its own database file with the
expected schema.
Task 4: Load Sheets into Tables
- Create one table per worksheet in the workbook-named schema.
- Use the worksheet name as the table name.
- Load DataFrame rows into the table.
- Replace an existing table's contents when reprocessing the workbook.
- Handle duplicate or invalid column names predictably.
- Ensure database connections are closed after processing.
Done when: All usable sheets are present as tables and their data
can be queried from DuckDB.
Task 5: Add the Command-Line Entry Point
- Implement src/ingestion/main.py.
- Read paths from config.yaml, resolving relative paths from the
  project root.
- Process all supported Excel files in the input directory.
- Continue processing other workbooks when one workbook fails.
- Print a concise summary of successful and failed workbooks and
  sheet-level errors.
- Make the app runnable from the project root with
  python -m src.ingestion.main.
Done when: Running the command processes the configured data/
directory without code changes.
Task 6: Add Basic Tests
- Create tests/test_excel_to_duckdb.py.
- Use temporary directories and small test workbooks.
- Test a workbook containing multiple sheets.
- Test that two workbooks create two separate database files.
- Test schema, table names, and loaded values.
- Test that reprocessing replaces existing table data.
- Test that an invalid workbook does not stop processing a valid
  workbook.
- Test .xls support when the required reader dependency is
  available.
Done when: Tests pass without depending on permanent input files or
production database files.
Task 7: Verify the POC
- Run tests under Python 3.12.
- Run Ruff if configured.
- Add a short README section explaining configuration, dependency
  installation, and how to run python -m src.ingestion.main.
- Confirm application source code remains inside src/ingestion/ and
  generated database files are written only to the configured output
  directory.
Done when: A new user can put Excel files in data/, run the app,
and query the resulting DuckDB tables.
Scope Guardrails
- All application code must be inside src/ingestion/.
- Do not add a web application, scheduler, orchestration framework, or
  complex class hierarchy.
- Do not implement incremental processing, schema evolution, or
  business transformations.
- Keep each task focused and verify it before moving to the next task.