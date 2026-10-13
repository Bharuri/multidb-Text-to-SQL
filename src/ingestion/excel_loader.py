"""Reads Excel workbook sheets into pandas DataFrames."""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SUPPORTED_EXTENSIONS = (".xls", ".xlsx")


class WorkbookReadError(Exception):
    """Raised when a workbook file cannot be opened or parsed."""


@dataclass
class Workbook:
    path: Path
    sheets: dict[str, pd.DataFrame] = field(default_factory=dict)
    skipped_sheets: dict[str, str] = field(default_factory=dict)


def find_workbooks(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    # Skip Excel lock files such as "~$report.xlsx", which are not workbooks.
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith("~$")
    )


def read_workbook(path: Path) -> Workbook:
    try:
        frames = pd.read_excel(path, sheet_name=None, header=0)
    except Exception as exc:
        raise WorkbookReadError(f"{path.name}: {exc}") from exc

    workbook = Workbook(path=path)
    for sheet_name, frame in frames.items():
        if frame.columns.empty:
            workbook.skipped_sheets[sheet_name] = "empty sheet (no header row)"
        else:
            workbook.sheets[sheet_name] = frame
    return workbook
