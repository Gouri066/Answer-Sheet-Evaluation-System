"""Central place for every path and tunable default used by the project."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Runtime data (database, uploaded files, secret key). Safe to delete to reset.
INSTANCE_DIR = os.environ.get("INSTANCE_DIR", os.path.join(BASE_DIR, "instance"))
UPLOAD_DIR = os.path.join(INSTANCE_DIR, "uploads")
DB_PATH = os.path.join(INSTANCE_DIR, "evaluation.db")

# Defaults for new exams (taken from the most common values in the original
# Evaluation_Weightage.csv: grammar 20 / keywords 50 / similarity 30, 5 marks).
DEFAULT_MARKS = 5.0
DEFAULT_WEIGHTS = {"grammar": 20.0, "keyword": 50.0, "semantic": 30.0}

# Original CSV-based pipeline (still used by src/main.py and the CLI).
# NOTE: the original file had wrong file names ("QuestionAnswersDataset.csv",
# "Weightage.csv"), which fail on case-sensitive file systems. Fixed here.
PATHS = {
    "questions": os.path.join(DATA_DIR, "QuestionAnswersDataSet.csv"),
    "keywords": os.path.join(DATA_DIR, "Keywords.csv"),
    "weights": os.path.join(DATA_DIR, "Evaluation_Weightage.csv"),
    "output": {
        "results": os.path.join(BASE_DIR, "results.csv"),
        "metadata": os.path.join(BASE_DIR, "result_metadata.csv"),
    },
}
