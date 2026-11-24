"""Turn a teacher's question paper + answer key files into question records."""
import json
import os
import re

import pandas as pd

from config.paths import DEFAULT_MARKS, DEFAULT_WEIGHTS
from src.answer_parser import AnswerParser
from src.pdf_processor import PDFProcessor
from src.scoring_engine import generate_keywords

_parser = AnswerParser()

MARKS_RE = re.compile(
    r"(?:\[\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)?\s*\]"
    r"|\(\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)\s*\)"
    r"|[-\u2013,]?\s*(\d+(?:\.\d+)?)\s*marks?)\s*$",
    re.IGNORECASE,
)

_COLS = {
    "number": {"questionnumber", "qno", "no", "number", "q", "sno", "srno", "#", "id", "questionno"},
    "question": {"question", "questiontext", "questions"},
    "answer": {"answer", "answers", "modelanswer", "correctanswer", "solution", "keyanswer"},
    "marks": {"marks", "totalmarks", "maxmarks", "points", "mark"},
}


def _norm_col(c):
    return re.sub(r"[^a-z0-9#]", "", str(c).lower())


def _split_marks(question_text, default=None):
    m = MARKS_RE.search(question_text or "")
    if not m:
        return question_text, default
    marks = float(next(g for g in m.groups() if g))
    return question_text[: m.start()].strip(), marks


def _read_csv(path):
    for enc in ("utf-8-sig", "latin1"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not read the CSV file (unknown encoding).")


def parse_csv(path):
    df = _read_csv(path)
    cols = {}
    for c in df.columns:
        n = _norm_col(c)
        for key, names in _COLS.items():
            if n in names and key not in cols:
                cols[key] = c
    if "answer" not in cols:
        raise ValueError("The CSV needs an 'Answer' column (and ideally 'Question Number', 'Question', 'Marks').")
    items = {}
    for i, row in df.reset_index(drop=True).iterrows():
        try:
            number = int(float(row[cols["number"]])) if "number" in cols else i + 1
        except (ValueError, TypeError):
            continue
        ans = row[cols["answer"]]
        q = row[cols["question"]] if "question" in cols else ""
        marks = row[cols["marks"]] if "marks" in cols else None
        items[number] = {
            "question": "" if pd.isna(q) else str(q).strip(),
            "answer": "" if pd.isna(ans) else str(ans).strip(),
            "marks": None if marks is None or pd.isna(marks) else float(marks),
        }
    return items


def _read_text(path):
    result = PDFProcessor().extract(path)
    if not result.text.strip():
        raise ValueError(f"No readable text found in {os.path.basename(path)}.")
    return result.text


def parse_paper(path):
    """Question paper -> {n: {'question', 'marks'}}"""
    if path.lower().endswith(".csv"):
        return {n: {"question": v["question"], "marks": v["marks"]} for n, v in parse_csv(path).items()}
    blocks, _, _ = _parser.parse_text(_read_text(path), sequential_bare=True)
    items = {}
    for n, text in blocks.items():
        q, marks = _split_marks(text)
        items[n] = {"question": q, "marks": marks}
    return items


def parse_key(path):
    """Answer key -> {n: {'question', 'answer', 'marks'}}"""
    if path.lower().endswith(".csv"):
        return parse_csv(path)
    blocks, _, _ = _parser.parse_text(_read_text(path), sequential_bare=True)
    items = {}
    for n, text in blocks.items():
        q, ans = _parser.split_question_answer(text)
        items[n] = {"question": q, "answer": ans, "marks": None}
    return items


def build_questions(key_path, paper_path=None, default_marks=DEFAULT_MARKS, weights=None):
    """Merge paper + key into a sorted list of question dicts ready for the database."""
    weights = weights or DEFAULT_WEIGHTS
    key = parse_key(key_path)
    paper = parse_paper(paper_path) if paper_path else {}
    if not key and not paper:
        raise ValueError(
            "No numbered questions were detected. Number each item like 'Q1:', '1.' or '1)' "
            "or upload a CSV with Question Number / Question / Answer columns."
        )
    questions = []
    for n in sorted(set(key) | set(paper)):
        k, p = key.get(n, {}), paper.get(n, {})
        answer = k.get("answer", "")
        question = p.get("question") or k.get("question", "")
        marks = k.get("marks") or p.get("marks") or default_marks
        questions.append(
            {
                "number": n,
                "question": question,
                "model_answer": answer,
                "max_marks": float(marks),
                "w_grammar": weights["grammar"],
                "w_keyword": weights["keyword"],
                "w_semantic": weights["semantic"],
                "keywords": generate_keywords(answer, question) if answer else [],
            }
        )
    return questions


def apply_weights_csv(questions, path):
    """Optional: per-question marks/weights from the original Evaluation_Weightage.csv."""
    df = _read_csv(path)
    by_q = {int(r["question"]): r for _, r in df.iterrows()}
    for q in questions:
        r = by_q.get(q["number"])
        if r is not None:
            q["max_marks"] = float(r.get("totalMarks", q["max_marks"]))
            q["w_grammar"] = float(r.get("grammarwt", q["w_grammar"]))
            q["w_keyword"] = float(r.get("Keywordswt", q["w_keyword"]))
            q["w_semantic"] = float(r.get("similarityWt", q["w_semantic"]))
    return questions


def keywords_to_json(keywords):
    return json.dumps(list(keywords))
