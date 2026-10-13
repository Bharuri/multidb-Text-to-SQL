Requirements --- Excel to Database POC
1. Objective
Create a small Python application that reads multiple Excel files
(.xls and .xlsx) from a configured folder and loads their sheets
into DuckDB.
2. Functional Requirements
FR1: Read Excel Files
- Read all .xls and .xlsx files in the configured input directory.
- Each Excel file may contain multiple sheets.
FR2: Create a Database
- Create one DuckDB database file for each Excel file.
- Use the Excel filename (without its extension) as the database
  filename.
- If the database file already exists, reuse it.
FR3: Create a Schema
- Create one schema inside each DuckDB database.
- Use the Excel filename (without its extension) as the schema name.
- If the schema already exists, reuse it.
FR4: Create Tables
- Create one table for each sheet in the Excel file.
- Use the sheet name as the table name.
- Use the first row as column headers.
- Load the remaining rows into the corresponding table.
- If a table already exists, replace its contents when the Excel file
  is processed again.
FR5: Names and Empty Values
- Convert schema, table, and column names to valid DuckDB identifiers.
- Handle empty sheets and missing cell values sensibly.
- Preserve cell values as accurately as practical for this POC.
3. Technical Requirements
- Python 3.12.
- DuckDB as the database engine.
- Use a suitable Python library to read .xls and .xlsx files.
- Use a simple configuration for the input directory and output
  database directory.
- Keep the code small and easy to understand.
4. Error Handling
- Print a clear message when an Excel file cannot be read.
- If one file fails, continue processing the remaining files where
  possible.
- Print a short summary of successful and failed files.
5. Out of Scope
- No web interface.
- No scheduling or orchestration.
- No complex logging framework.
- No business-specific transformations.
- No incremental processing or automated schema evolution.
- No unnecessary architecture layers.
6. Expected Result
For each Excel file: - One DuckDB database file. - One schema inside
that database. - One table for each sheet. - Sheet data loaded into its
corresponding table.