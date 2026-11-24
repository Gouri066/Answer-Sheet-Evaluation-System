"""Score one student from the command line against the CSV key in data/.

    python cli.py --rollno S001 --name "John Doe" --pdf Test/test_perfect_refined.pdf
"""
import argparse
import os
import sys

from src.main import evaluate_student


def main():
    p = argparse.ArgumentParser(description="Evaluate a student answer sheet")
    p.add_argument("--rollno", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--pdf", required=True, help="path to the answer sheet PDF")
    p.add_argument("--start-page", type=int, default=1, help="first page that contains answers")
    a = p.parse_args()
    if not os.path.exists(a.pdf):
        sys.exit(f"File not found: {a.pdf}")
    evaluate_student(a.pdf, a.rollno, a.name, a.start_page)


if __name__ == "__main__":
    main()
