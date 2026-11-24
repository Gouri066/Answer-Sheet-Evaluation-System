"""Use Gemini Vision AI to extract structured answers from answer sheet images.

This replaces the brittle EasyOCR + regex pipeline with an intelligent vision model
that understands any answer sheet layout, any handwriting style, and any format.

Usage:
    Set the GEMINI_API_KEY environment variable, then the worker will automatically
    use Gemini Vision instead of EasyOCR for all image/scanned submissions.

    Fallback: if GEMINI_API_KEY is not set or the API call fails, the system
    falls back to the existing EasyOCR + regex parsing pipeline.
"""
import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def is_available():
    """Check whether Gemini Vision is configured (API key present)."""
    return bool(os.environ.get("GEMINI_API_KEY"))


def _get_model():
    """Lazily initialise the Gemini model (singleton)."""
    global _model
    with _model_lock:
        if _model is not None:
            return _model

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None

        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            model_name = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.0-flash")
            _model = genai.GenerativeModel(model_name)
            log.info("Gemini Vision initialised with model %s", model_name)
            return _model
        except Exception as exc:
            log.warning("Could not initialise Gemini Vision: %s", exc)
            return None


def _build_prompt(questions=None):
    """Build the extraction prompt, optionally enriched with known question info."""

    question_hint = ""
    if questions:
        lines = []
        for q in sorted(questions, key=lambda q: q["number"]):
            lines.append(f'  Question {q["number"]}: {q["question"]}')
        question_hint = (
            "\n\nHere are the expected questions on this exam (use these to "
            "identify which answer belongs to which question):\n" + "\n".join(lines)
        )

    return f"""You are an expert at reading handwritten student answer sheets.

Carefully examine this answer sheet image and extract ALL information.

RULES:
1. Read EVERY handwritten word, number, and symbol on the page — even inside boxes, blanks, or on lines.
2. For each question, extract ONLY the student's ANSWER, NOT the question text.
   - For fill-in-the-blank (e.g. "25 + 10 = ____"), the answer is what fills the blank (e.g. "35").
   - For "Ans:" or "Answer:" labels, extract the text AFTER the label.
   - For equations like "25 + 10 = 35", the answer is "35" (the result).
   - For questions ending with "= ?", the answer is what the student wrote as the result.
3. Handle ANY question numbering: 1., 1), (1), Q1, Q.1, Question 1, etc.
4. Read numbers carefully: 0 vs O, 1 vs l, 5 vs S, 6 vs b, 8 vs B.
5. If the student left a question blank / unanswered, set its value to "" (empty string).
6. Extract the student's name and roll number / registration number if visible.
{question_hint}

Return ONLY valid JSON (no markdown fences, no explanation) in this exact structure:
{{
  "name": "student name or empty string",
  "roll_no": "roll number or empty string",
  "answers": {{
    "1": "student answer for question 1",
    "2": "student answer for question 2"
  }},
  "raw_lines": ["line 1 of text as read", "line 2", "..."]
}}

The "raw_lines" field should contain every line of text you can read on the sheet,
in reading order (top to bottom, left to right), including headers, name, date, etc.
This is used for auditing so the teacher can verify what the AI read."""


def _parse_response(text):
    """Parse the JSON from Gemini's response, tolerating markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
        else:
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    return json.loads(text)


def extract_from_image(image_data, mime_type="image/png", questions=None):
    """Extract answers from a single image using Gemini Vision.

    Parameters
    ----------
    image_data : bytes
        Raw image bytes.
    mime_type : str
        MIME type of the image (image/png, image/jpeg, etc.).
    questions : list[dict] | None
        Known questions for context (each with "number" and "question" keys).

    Returns
    -------
    dict | None
        {"name": str, "roll_no": str, "answers": {int: str}, "raw_text": str}
        or None if Gemini is unavailable or extraction fails.
    """
    model = _get_model()
    if model is None:
        return None

    prompt = _build_prompt(questions)

    try:
        response = model.generate_content(
            [prompt, {"mime_type": mime_type, "data": image_data}],
            generation_config={"temperature": 0.1},  # low temperature for factual extraction
        )

        result = _parse_response(response.text)

        # Normalise answers: keys to int, values to stripped strings
        answers = {}
        for k, v in result.get("answers", {}).items():
            try:
                answers[int(k)] = str(v).strip()
            except (ValueError, TypeError):
                continue

        # Build a readable raw_text from raw_lines (for storage/display)
        raw_lines = result.get("raw_lines", [])
        raw_text = "\n".join(str(line) for line in raw_lines) if raw_lines else ""

        # Also append an answer summary for transparency
        answer_summary = "\n--- Gemini Vision extracted answers ---"
        for n in sorted(answers):
            answer_summary += f"\nQ{n}: {answers[n]!r}"
        raw_text += answer_summary

        return {
            "name": str(result.get("name", "")).strip(),
            "roll_no": str(result.get("roll_no", "")).strip(),
            "answers": answers,
            "raw_text": raw_text,
        }

    except Exception as exc:
        log.warning("Gemini Vision extraction failed: %s", exc)
        return None


def extract_from_file(file_path, questions=None):
    """Extract answers from an image file or PDF using Gemini Vision.

    For PDFs, each page is sent as a separate image and results are merged.
    For images, the file is sent directly.

    Returns dict with {name, roll_no, answers, raw_text, pages, warnings} or None.
    """
    ext = os.path.splitext(file_path)[1].lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".bmp": "image/bmp",
        ".tif": "image/tiff", ".tiff": "image/tiff",
    }

    if ext in mime_map:
        with open(file_path, "rb") as fh:
            data = fh.read()
        result = extract_from_image(data, mime_map[ext], questions)
        if result:
            result["pages"] = 1
            result["warnings"] = ["Read with Gemini Vision AI."]
        return result

    if ext == ".pdf":
        return _extract_from_pdf(file_path, questions)

    return None  # unsupported format — let the fallback handle it


def _extract_from_pdf(pdf_path, questions=None):
    """Extract answers from a PDF by rendering each page and sending to Gemini."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("PyMuPDF not available for PDF rendering")
        return None

    merged = {"name": "", "roll_no": "", "answers": {}, "raw_text": "", "pages": 0, "warnings": []}

    with fitz.open(pdf_path) as doc:
        if doc.needs_pass:
            return None
        merged["pages"] = len(doc)

        for i in range(len(doc)):
            page = doc[i]
            pix = page.get_pixmap(dpi=200)
            image_data = pix.tobytes("png")

            result = extract_from_image(image_data, "image/png", questions)
            if result is None:
                merged["warnings"].append(f"Page {i + 1}: Gemini Vision extraction failed.")
                continue

            # Merge results
            if result["name"] and not merged["name"]:
                merged["name"] = result["name"]
            if result["roll_no"] and not merged["roll_no"]:
                merged["roll_no"] = result["roll_no"]

            # Merge answers (later pages don't overwrite earlier ones unless empty)
            for n, ans in result["answers"].items():
                if n not in merged["answers"] or not merged["answers"][n]:
                    merged["answers"][n] = ans

            # Append raw text with page separator
            if merged["raw_text"]:
                merged["raw_text"] += f"\n\n--- Page {i + 1} ---\n"
            merged["raw_text"] += result["raw_text"]

    if not merged["answers"]:
        return None

    merged["warnings"].append("Read with Gemini Vision AI.")
    return merged
