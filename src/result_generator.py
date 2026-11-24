import os

import pandas as pd

from config.paths import PATHS


class ResultGenerator:
    """Collects per-student totals and appends them to a CSV (used by the command-line tool)."""

    COLUMNS = ["RollNo", "Name", "Total", "MaxTotal", "Percentage"]

    def __init__(self):
        self.rows = []

    def add_student(self, rollno, name, scores, max_total):
        total = round(sum(scores.values()), 2)
        pct = round(total / max_total * 100, 2) if max_total else 0.0
        self.rows.append([rollno, name, total, max_total, pct])
        return self.rows[-1]

    def save_results(self, path=None):
        path = path or PATHS["output"]["results"]
        df = pd.DataFrame(self.rows, columns=self.COLUMNS)
        df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)
        return path
