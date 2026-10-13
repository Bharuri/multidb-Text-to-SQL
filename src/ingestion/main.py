"""Command-line entry point: python -m src.ingestion.main"""

import sys
from pathlib import Path

import yaml

from src.ingestion.duckdb_writer import load_workbook, workbook_name
from src.ingestion.excel_loader import WorkbookReadError, find_workbooks, read_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    missing = [key for key in ("input_dir", "database_dir") if not config.get(key)]
    if missing:
        raise ValueError(f"{path.name} is missing: {', '.join(missing)}")
    return config


def main() -> int:
    try:
        config = load_config(CONFIG_PATH)
        input_dir = resolve_path(str(config["input_dir"]))
        database_dir = resolve_path(str(config["database_dir"]))
        workbook_paths = find_workbooks(input_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Error: {exc}")
        return 1

    if not workbook_paths:
        print(f"No .xls or .xlsx files found in {input_dir}")
        return 0

    succeeded: list[str] = []
    failed: list[str] = []
    sheet_issues: list[str] = []
    claimed_names: dict[str, str] = {}

    for path in workbook_paths:
        print(f"Processing {path.name}")
        db_key = workbook_name(path).casefold()
        if db_key in claimed_names:
            failed.append(path.name)
            print(
                f"  FAILED: database name '{db_key}' is already used by "
                f"{claimed_names[db_key]}; skipping to avoid merging data"
            )
            continue
        claimed_names[db_key] = path.name

        try:
            workbook = read_workbook(path)
            result = load_workbook(database_dir, path, workbook.sheets)
        except WorkbookReadError as exc:
            failed.append(path.name)
            print(f"  FAILED: {exc}")
            continue
        except Exception as exc:
            failed.append(path.name)
            print(f"  FAILED: {path.name}: {exc}")
            continue

        for sheet_name, reason in workbook.skipped_sheets.items():
            sheet_issues.append(f"{path.name} / {sheet_name}: {reason}")
        for sheet_name, error in result.sheet_errors.items():
            sheet_issues.append(f"{path.name} / {sheet_name}: {error}")

        succeeded.append(f"{path.name} -> {result.database.name} ({len(result.tables)} table(s))")
        print(f"  OK: {len(result.tables)} table(s) written to {result.database.name}")

    print()
    print("Summary")
    print(f"  Successful workbooks: {len(succeeded)}")
    for line in succeeded:
        print(f"    - {line}")
    print(f"  Failed workbooks: {len(failed)}")
    for name in failed:
        print(f"    - {name}")
    if sheet_issues:
        print(f"  Sheet issues: {len(sheet_issues)}")
        for line in sheet_issues:
            print(f"    - {line}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
