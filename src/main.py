"""Command-line evaluation using the CSV files in data/ (the web app uses the same engine)."""
import logging

from config.paths import PATHS
from src.answer_parser import AnswerParser
from src.key_importer import apply_weights_csv, build_questions
from src.pdf_processor import PDFProcessor
from src.result_generator import ResultGenerator
from src.scoring_engine import ScoringEngine

logging.getLogger("pdfminer").setLevel(logging.ERROR)


def evaluate_student(pdf_path, rollno, name, start_page=1):
    questions = apply_weights_csv(build_questions(PATHS["questions"]), PATHS["weights"])
    engine = ScoringEngine(questions)
    parser = AnswerParser()
    by_num = {q["number"]: q for q in questions}

    text = PDFProcessor(start_page).extract_text(pdf_path)
    blocks, _, _ = parser.parse_text(text, set(by_num))
    answers = {n: parser.clean_answer(b, by_num[n]["question"]) for n, b in blocks.items() if n in by_num}

    results = engine.score_answers(answers)
    for r in results:
        if r["answer_text"]:
            print(f"Q{r['number']}: {r['marks']:.2f}/{r['max_marks']:g}")
    scores = {r["number"]: r["marks"] for r in results}
    max_total = sum(r["max_marks"] for r in results)

    reporter = ResultGenerator()
    reporter.add_student(rollno, name, scores, max_total)
    reporter.save_results()
    total = sum(scores.values())
    pct = total / max_total * 100 if max_total else 0.0
    print(f"\nProcessed {name} ({rollno}). Total: {total:.2f}/{max_total:g} ({pct:.2f}%)")
    return total, pct
