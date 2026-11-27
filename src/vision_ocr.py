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
    """Build a full-page handwriting extraction prompt."""

    question_hint = ""

    if questions:
        lines = []
        for q in sorted(questions, key=lambda item: int(item["number"])):
            lines.append(
                f'Question {q["number"]}: {q["question"]}'
            )

        question_hint = (
            "\nTEACHER'S QUESTION LIST:\n"
            + "\n".join(lines)
        )

    return f"""
You are an expert document reader specializing in handwritten
student answer sheets.

Examine the COMPLETE image carefully. Read the handwriting directly
from the original page. Do not locate answers using bounding boxes.

TASK:
Identify each teacher question and transcribe the student's
corresponding final handwritten answer.

IMPORTANT RULES:

1. Examine the entire page, from top to bottom.
2. Match answers using question numbers, question text and
   their reading order.
3. Answers may be below, above, beside, inside or on the same
   line as a question.
4. Answer positions and spacing can vary.
5. Do not assume fixed layouts, boxes or coordinates.
6. Do not include printed question text in the student's answer.
7. For equations, extract the student's final result.
8. For fill-in-the-blanks, extract what the student wrote
   in the blank.
9. For series questions, include all written missing elements.
10. For crossed-out answers, prefer the clearly visible final
    answer. Do not treat crossed-out attempts as final answers.
11. Preserve the student's actual spelling, numbers and symbols.
12. Do not correct, complete or invent student answers.
13. Do not use the teacher's model answers to guess handwriting.
14. If an answer is unclear, preserve your best transcription
    but assign low confidence and explain the uncertainty.
15. If an answer is genuinely missing, return an empty string.
16. Never combine answers from different questions.
17. Read student name and roll number when visible.

{question_hint}

Return ONLY valid JSON in this structure:

{{
  "name": "",
  "roll_no": "",
  "answers": {{
    "1": {{
      "text": "",
      "confidence": "high",
      "note": ""
    }}
  }},
  "raw_lines": []
}}

Include an entry for every question in the teacher's question list.
Use the actual question numbers supplied.

Confidence must be high, medium or low.
Do not return markdown fences or explanations.
"""


# def _build_prompt(questions=None):
#     """Build the extraction prompt, optionally enriched with known question info."""

#     question_hint = ""
#     if questions:
#         lines = []
#         for q in sorted(questions, key=lambda q: q["number"]):
#             lines.append(f'  Question {q["number"]}: {q["question"]}')
#         question_hint = (
#             "\n\nHere are the expected questions on this exam (use these to "
#             "identify which answer belongs to which question):\n" + "\n".join(lines)
#         )

#     return f"""You are an expert at reading handwritten student answer sheets.

# Carefully examine this answer sheet image and extract ALL information.

# RULES:
# 1. Read EVERY handwritten word, number, and symbol on the page — even inside boxes, blanks, or on lines.
# 2. For each question, extract ONLY the student's ANSWER, NOT the question text.
#    - For fill-in-the-blank (e.g. "25 + 10 = ____"), the answer is what fills the blank (e.g. "35").
#    - For "Ans:" or "Answer:" labels, extract the text AFTER the label.
#    - For equations like "25 + 10 = 35", the answer is "35" (the result).
#    - For questions ending with "= ?", the answer is what the student wrote as the result.
# 3. Handle ANY question numbering: 1., 1), (1), Q1, Q.1, Question 1, etc.
# 4. Read numbers carefully: 0 vs O, 1 vs l, 5 vs S, 6 vs b, 8 vs B.
# 5. If the student left a question blank / unanswered, set its value to "" (empty string).
# 6. Extract the student's name and roll number / registration number if visible.
# {question_hint}

# Return ONLY valid JSON (no markdown fences, no explanation) in this exact structure:
# {{
#   "name": "student name or empty string",
#   "roll_no": "roll number or empty string",
#   "answers": {{
#     "1": "student answer for question 1",
#     "2": "student answer for question 2"
#   }},
#   "raw_lines": ["line 1 of text as read", "line 2", "..."]
# }}

# The "raw_lines" field should contain every line of text you can read on the sheet,
# in reading order (top to bottom, left to right), including headers, name, date, etc.
# This is used for auditing so the teacher can verify what the AI read."""

def _crop_answer_region(image_data, mime_type, bounding_box):
    """Crop a dynamically identified answer region from an image."""
    from io import BytesIO
    from PIL import Image

    image = Image.open(BytesIO(image_data)).convert("RGB")
    width, height = image.size

    x_min, y_min, x_max, y_max = bounding_box

    # Convert normalized coordinates (0–1000) to image pixels.
    left = int(x_min / 1000 * width)
    top = int(y_min / 1000 * height)
    right = int(x_max / 1000 * width)
    bottom = int(y_max / 1000 * height)

    # Add a small margin around the detected handwriting.
    margin_x = max(5, int((right - left) * 0.12))
    margin_y = max(5, int((bottom - top) * 0.20))

    left = max(0, left - margin_x)
    top = max(0, top - margin_y)
    right = min(width, right + margin_x)
    bottom = min(height, bottom + margin_y)

    if right <= left or bottom <= top:
        raise ValueError("Invalid answer-region coordinates.")

    cropped = image.crop((left, top, right, bottom))

    output = BytesIO()
    cropped.save(output, format="PNG")

    return output.getvalue()



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


# def extract_from_image(image_data, mime_type="image/png", questions=None):
#     """Extract answers from a single image using Gemini Vision.

#     Parameters
#     ----------
#     image_data : bytes
#         Raw image bytes.
#     mime_type : str
#         MIME type of the image (image/png, image/jpeg, etc.).
#     questions : list[dict] | None
#         Known questions for context (each with "number" and "question" keys).

#     Returns
#     -------
#     dict | None
#         {"name": str, "roll_no": str, "answers": {int: str}, "raw_text": str}
#         or None if Gemini is unavailable or extraction fails.
#     """
#     model = _get_model()
#     if model is None:
#         return None

#     prompt = _build_prompt(questions)

#     try:
#         response = model.generate_content(
#             [prompt, {"mime_type": mime_type, "data": image_data}],
#             generation_config={"temperature": 0.1},  # low temperature for factual extraction
#         )

#         result = _parse_response(response.text)

#         # Normalise answers: keys to int, values to stripped strings
#         answers = {}
#         for k, v in result.get("answers", {}).items():
#             try:
#                 answers[int(k)] = str(v).strip()
#             except (ValueError, TypeError):
#                 continue

#         # Build a readable raw_text from raw_lines (for storage/display)
#         raw_lines = result.get("raw_lines", [])
#         raw_text = "\n".join(str(line) for line in raw_lines) if raw_lines else ""

#         # Also append an answer summary for transparency
#         answer_summary = "\n--- Gemini Vision extracted answers ---"
#         for n in sorted(answers):
#             answer_summary += f"\nQ{n}: {answers[n]!r}"
#         raw_text += answer_summary

#         return {
#             "name": str(result.get("name", "")).strip(),
#             "roll_no": str(result.get("roll_no", "")).strip(),
#             "answers": answers,
#             "raw_text": raw_text,
#         }

#     except Exception as exc:
#         log.warning("Gemini Vision extraction failed: %s", exc)
#         return None


def extract_from_image(image_data, mime_type="image/png", questions=None):
    """Extract answers directly from the complete page using Gemini."""

    model = _get_model()

    if model is None:
        return None

    try:
        prompt = _build_prompt(questions)

        response = model.generate_content(
            [
                prompt,
                {
                    "mime_type": mime_type,
                    "data": image_data,
                },
            ],
            generation_config={"temperature": 0.1},
        )

        result = _parse_response(response.text)

        answers = {}
        answer_details = {}

        raw_answers = result.get("answers", {})

        if not isinstance(raw_answers, dict):
            raise ValueError("Gemini returned an invalid answers structure.")

        for key, value in raw_answers.items():
            try:
                number = int(key)
            except (ValueError, TypeError):
                continue

            if isinstance(value, dict):
                answer_text = str(value.get("text", "") or "").strip()
                confidence = str(
                    value.get("confidence", "medium") or "medium"
                ).strip().lower()
                note = str(value.get("note", "") or "").strip()
            else:
                answer_text = str(value or "").strip()
                confidence = "medium"
                note = "Confidence information was not provided."

            if confidence not in ("high", "medium", "low"):
                confidence = "medium"

            answers[number] = answer_text

            answer_details[number] = {
                "confidence": confidence,
                "note": note,
            }

        raw_lines = result.get("raw_lines", [])

        if isinstance(raw_lines, list):
            raw_text = "\n".join(str(line) for line in raw_lines)
        else:
            raw_text = str(raw_lines or "")

        raw_text += "\n\n--- Gemini Vision extracted answers ---"

        for number in sorted(answers):
            details = answer_details[number]

            raw_text += (
                f"\nQ{number}: {answers[number]}"
                f"\nConfidence: {details['confidence']}"
            )

            if details["note"]:
                raw_text += f"\nNote: {details['note']}"

        return {
            "name": str(result.get("name", "") or "").strip(),
            "roll_no": str(result.get("roll_no", "") or "").strip(),
            "answers": answers,
            "answer_details": answer_details,
            "raw_text": raw_text,
        }

    except Exception as exc:
        log.warning("Gemini Vision extraction failed: %s", exc)
        return None

def extract_from_file(file_path, questions=None):
    """Extract answers from supported image files or PDFs."""

    ext = os.path.splitext(file_path)[1].lower()

    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }

    if ext in mime_map:
        with open(file_path, "rb") as fh:
            image_data = fh.read()

        result = extract_from_image(
            image_data, mime_map[ext], questions
        )

        if result:
            result["pages"] = 1
            result["warnings"] = [
                "Read with Gemini Vision AI."
            ]

        return result

    if ext == ".pdf":
        return _extract_from_pdf(file_path, questions)

    log.warning("Unsupported answer sheet format: %s", ext)
    return None


def _extract_from_pdf(pdf_path, questions=None):
    """Render PDF pages and extract answers from each page."""

    try:
        import fitz
    except ImportError:
        log.warning("PyMuPDF is required for PDF processing.")
        return None

    merged = {
        "name": "",
        "roll_no": "",
        "answers": {},
        "answer_details": {},
        "raw_text": "",
        "pages": 0,
        "warnings": [],
    }

    try:
        with fitz.open(pdf_path) as doc:
            if doc.needs_pass:
                return None

            merged["pages"] = len(doc)

            for page_number, page in enumerate(doc, start=1):
                pix = page.get_pixmap(dpi=200)
                image_data = pix.tobytes("png")

                result = extract_from_image(
                    image_data,
                    "image/png",
                    questions,
                )

                if result is None:
                    merged["warnings"].append(
                        f"Page {page_number}: extraction failed."
                    )
                    continue

                if result.get("name") and not merged["name"]:
                    merged["name"] = result["name"]

                if result.get("roll_no") and not merged["roll_no"]:
                    merged["roll_no"] = result["roll_no"]

                for number, answer in result.get("answers", {}).items():
                    if number not in merged["answers"] or not merged["answers"][number]:
                        merged["answers"][number] = answer

                        merged["answer_details"][number] = (
                            result.get("answer_details", {}).get(
                                number, {}
                            )
                        )

                merged["raw_text"] += (
                    f"\n\n--- Page {page_number} ---\n"
                    + result.get("raw_text", "")
                )

    except Exception as exc:
        log.warning("PDF extraction failed: %s", exc)
        return None

    if not merged["answers"]:
        return None

    merged["warnings"].append("Read with Gemini Vision AI.")
    return merged
