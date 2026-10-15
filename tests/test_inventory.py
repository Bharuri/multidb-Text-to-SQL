import unittest

import pandas as pd

from src.ingestion.inventory import profile_frame


class ProfileFrameTests(unittest.TestCase):
    def test_unique_non_null_column_is_candidate_key(self):
        frame = pd.DataFrame({"id": [1, 2, 3], "grp": ["a", "a", "b"]})

        profile = profile_frame("book.xlsx", "Sheet1", frame)

        self.assertEqual(profile.row_count, 3)
        self.assertEqual(profile.candidate_keys, ["id"])

    def test_null_values_disqualify_candidate_key(self):
        frame = pd.DataFrame({"id": [1, 2, None]})

        profile = profile_frame("book.xlsx", "Sheet1", frame)

        self.assertEqual(profile.candidate_keys, [])
        self.assertEqual(profile.columns[0].null_count, 1)

    def test_duplicate_rows_are_counted(self):
        frame = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"]})

        profile = profile_frame("book.xlsx", "Sheet1", frame)

        self.assertEqual(profile.duplicate_row_count, 1)

    def test_near_unique_column_is_flagged_for_review(self):
        values = list(range(100))
        values[0] = 1  # 99 distinct of 100 rows: not a key, but near-unique
        frame = pd.DataFrame({"code": values})

        profile = profile_frame("book.xlsx", "Sheet1", frame)

        self.assertEqual(profile.candidate_keys, [])
        self.assertEqual(profile.near_unique_columns, ["code"])


if __name__ == "__main__":
    unittest.main()
