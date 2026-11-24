"""Background evaluation: extract text -> detect answers -> score -> save."""
import json
import logging
import os
import queue
import threading

from src.answer_parser import AnswerParser
from src.pdf_processor import OCRUnavailable, PDFProcessor
from src.scoring_engine import ScoringEngine
from webapp.db import connect, now

log = logging.getLogger(__name__)

_queue = queue.Queue()
_queued = set()
_lock = threading.Lock()
_thread = None


def enqueue(exam_id):
    global _thread
    with _lock:
        if exam_id not in _queued:
            _queued.add(exam_id)
            _queue.put(exam_id)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="evaluator", daemon=True)
            _thread.start()


def resume_pending():
    """After a restart, requeue anything that was waiting or half-processed."""
    conn = connect()
    conn.execute("UPDATE submissions SET status = 'pending' WHERE status = 'processing'")
    conn.commit()
    for row in conn.execute("SELECT DISTINCT exam_id FROM submissions WHERE status = 'pending'").fetchall():
        enqueue(row["exam_id"])
    conn.close()


def _loop():
    while True:
        exam_id = _queue.get()
        with _lock:
            _queued.discard(exam_id)
        try:
            process_exam(exam_id)
        except Exception:  # keep the worker alive whatever happens
            log.exception("Evaluation of exam %s failed", exam_id)


def _load_questions(conn, exam_id):
    rows = conn.execute("SELECT * FROM questions WHERE exam_id = ? ORDER BY number", (exam_id,)).fetchall()
    out = []
    for r in rows:
        q = dict(r)
        q["keywords"] = json.loads(q["keywords"] or "[]")
        out.append(q)
    return out


def process_exam(exam_id):
    conn = connect()
    try:
        questions = _load_questions(conn, exam_id)
        if not questions:
            conn.execute(
                "UPDATE submissions SET status='error', note='The exam has no questions.' "
                "WHERE exam_id = ? AND status = 'pending'", (exam_id,))
            conn.commit()
            return
        engine = ScoringEngine(questions)
        parser = AnswerParser()
        processor = PDFProcessor()
        by_num = {q["number"]: q for q in questions}
        while True:
            sub = conn.execute(
                "SELECT * FROM submissions WHERE exam_id = ? AND status = 'pending' ORDER BY id LIMIT 1",
                (exam_id,)).fetchone()
            if sub is None:
                break
            conn.execute("UPDATE submissions SET status = 'processing' WHERE id = ?", (sub["id"],))
            conn.commit()
            try:
                _evaluate(conn, sub, engine, parser, processor, by_num)
            except OCRUnavailable as exc:
                _fail(conn, sub["id"], str(exc))
            except ValueError as exc:  # expected problems: unreadable file, no answers detected
                log.warning("Submission %s: %s", sub["id"], exc)
                _fail(conn, sub["id"], str(exc))
            except Exception as exc:
                log.exception("Submission %s failed", sub["id"])
                _fail(conn, sub["id"], f"Could not process this file: {exc}")
    finally:
        conn.close()


def _fail(conn, sub_id, message):
    conn.execute("UPDATE submissions SET status = 'error', note = ?, evaluated_at = ? WHERE id = ?",
                 (message[:500], now(), sub_id))
    conn.commit()


def _evaluate(conn, sub, engine, parser, processor, by_num):
    notes = []
    raw, roll, name = sub["raw_text"], sub["roll_no"], sub["name"]
    valid = set(by_num)

    if raw is None:  # first time: read the file
        result = processor.extract(sub["stored_path"])
        raw = result.text
        notes += result.warnings
        if result.ocr_pages:
            notes.append(f"{result.ocr_pages} page(s) were read with OCR - please spot-check the answers.")
        if not raw.strip():
            raise ValueError("No readable text was found. If this is a scan, install OCR support (pip install easyocr).")
        blocks, header, warns = parser.parse_text(raw, valid)
        found = parser.extract_header(header)
        roll = found["roll_no"] or roll
        name = found["name"] or name
    else:
        blocks, _, warns = parser.parse_text(raw, valid)
    notes += warns

    answers, unknown = {}, []
    for n, block in blocks.items():
        if n in by_num:
            answers[n] = parser.clean_answer(block, by_num[n]["question"])
        else:
            unknown.append(n)
    if not answers:
        raise ValueError("No numbered answers were detected (expected Q1:, 1., 1) ...).")
    if unknown:
        notes.append("Ignored answers for question numbers not in the key: " + ", ".join(f"Q{n}" for n in sorted(unknown)[:15]))
    unanswered = [n for n in by_num if not answers.get(n)]
    if unanswered:
        notes.append(f"No answer found for {len(unanswered)} question(s).")

    results = engine.score_answers(answers)
    total = round(sum(r["marks"] for r in results), 2)
    max_total = round(sum(r["max_marks"] for r in results), 2)
    pct = round(total / max_total * 100, 2) if max_total else 0.0

    conn.execute("DELETE FROM answers WHERE submission_id = ?", (sub["id"],))
    conn.executemany(
        "INSERT INTO answers (submission_id, number, answer_text, grammar, keyword, semantic, marks, max_marks,"
        " matched, missing, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(sub["id"], r["number"], r["answer_text"], r["grammar"], r["keyword"], r["semantic"], r["marks"],
          r["max_marks"], json.dumps(r["matched"]), json.dumps(r["missing"]), r["note"]) for r in results])
    conn.execute(
        "UPDATE submissions SET raw_text=?, roll_no=?, name=?, status='done', note=?, total=?, max_total=?,"
        " percentage=?, backend=?, evaluated_at=? WHERE id=?",
        (raw, roll, name, " ".join(notes)[:1000], total, max_total, pct, engine.backend, now(), sub["id"]))
    conn.commit()
