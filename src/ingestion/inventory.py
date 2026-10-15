"""Profiles workbook sheets to support metadata and key decisions."""

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    null_count: int
    distinct_count: int


@dataclass
class SheetProfile:
    workbook: str
    sheet: str
    row_count: int
    duplicate_row_count: int
    columns: list[ColumnProfile] = field(default_factory=list)
    candidate_keys: list[str] = field(default_factory=list)
    near_unique_columns: list[str] = field(default_factory=list)


def profile_frame(workbook: str, sheet: str, frame: pd.DataFrame) -> SheetProfile:
    row_count = len(frame)
    columns = [
        ColumnProfile(
            name=str(column),
            dtype=str(frame[column].dtype),
            null_count=int(frame[column].isna().sum()),
            distinct_count=int(frame[column].nunique(dropna=True)),
        )
        for column in frame.columns
    ]
    candidate_keys = [
        col.name
        for col in columns
        if row_count > 0 and col.null_count == 0 and col.distinct_count == row_count
    ]
    # Columns that are almost unique but not unique, so a human should check them.
    near_unique = [
        col.name
        for col in columns
        if row_count > 0
        and col.name not in candidate_keys
        and col.distinct_count / row_count >= 0.99
    ]
    return SheetProfile(
        workbook=workbook,
        sheet=sheet,
        row_count=row_count,
        duplicate_row_count=int(frame.duplicated().sum()),
        columns=columns,
        candidate_keys=candidate_keys,
        near_unique_columns=near_unique,
    )
