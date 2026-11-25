"""Detect which text belongs to which question number.

Handles the formats seen in the sample sheets and common variants:
  Q1: ...   Q.1 ...   Question 12 - ...   Question110: ...   Ans 3: ...
  1) ...    1. ...    (1) ...             97: ...
Questions may appear in any order, wrap over several lines and pages.
"""
import re

_DELIM = r"[:.)\]\-\u2013\u2014]"

# Q1, Question 2, Problem 3 ... (delimiter optional)
PREFIX_RE = re.compile(
    r"^\s*\(?\s*(?:question|ques|que|q|problem)\s*\.?\s*(?:no\.?|number|#)?\s*(\d{1,3})(?!\d)\s*"
    + _DELIM + r"*\s*(.*)$",
    re.IGNORECASE,
)
# Ans 1: / Answer 2) ... (delimiter required, otherwise "Answer 2 types..." would match)
ANS_PREFIX_RE = re.compile(
    r"^\s*(?:ans(?:wer)?)\s*\.?\s*(?:no\.?|number|#)?\s*(\d{1,3})(?!\d)\s*" + _DELIM + r"\s*(.*)$",
    re.IGNORECASE,
)
# 1)  1.  (1)  97:   - number followed by a delimiter and whitespace ("1.5" / "10:30" don't match)
BARE_RE = re.compile(r"^\s*\(?\s*(\d{1,3})\s*[:.)\]](?=\s|$)\s*(.*)$")
# Fallback: OCR often drops the period/delimiter after a question number, leaving
# just "1 answer text" or "3  IOC 40 60".  We accept a bare number followed by
# whitespace (no delimiter) only when the number is in valid_numbers, so we don't
# accidentally split prose paragraphs on stray numbers.
# The rest must NOT start with a math operator (+, -, *, ×, =) which would indicate
# the number is part of an expression like "5 * 2" rather than a question marker.
BARE_NODELIM_RE = re.compile(r"^\s*(\d{1,3})\s{1,4}(?![+\-*/×=])(\S.*)$")

PAGE_NUM_RE = re.compile(
    r"^\s*(?:page\s+\d{1,3}|\d{1,3}\s*(?:of|/)\s*\d{1,3})\s*$",
    re.IGNORECASE,
)
ANS_SPLIT_RE = re.compile(r"(?:^|\s)(?:ans(?:wer)?|solution)\s*[:\-\u2013]\s*", re.IGNORECASE)

ROLL_RE = re.compile(
    r"(?:roll\s*(?:no\.?|number|#)?|reg(?:istration)?\.?\s*(?:no\.?|number)?|enrol+ment\s*(?:no\.?)?|"
    r"student\s*id|\bid)\s*[:\-\u2013]\s*([A-Za-z0-9][A-Za-z0-9\-_/.]*)",
    re.IGNORECASE,
)
NAME_RE = re.compile(
    r"(?:student\s*)?name\s*[:\-\u2013]\s*(.+?)(?=\s{2,}|\s+(?:roll|reg|id|class|date|subject)\b|$)",
    re.IGNORECASE,
)


def _join_lines(lines):
    out = ""
    for line in lines:
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and line[:1].islower():
            out += line  # word wrapped with a hyphen
        else:
            out += " " + line
    return re.sub(r"\s+", " ", out).strip()


class AnswerParser:
    def parse_text(self, text, valid_numbers=None, sequential_bare=False):
        """Split text into {question_number: text}.

        valid_numbers    - if given, bare markers ("1)", "97:") are only accepted for these numbers.
                           Explicit markers (Q5, Question 5) are always accepted.
        sequential_bare  - accept bare markers only in order 1,2,3... (used for question papers
                           and answer keys, where numbered lists inside a question are common).
        Returns (blocks, header_lines, warnings).
        """
        blocks, header, warnings = {}, [], []
        seen, current, buf = set(), None, []

        def flush():
            if current is None:
                return
            body = _join_lines(buf)
            if current in blocks:
                warnings.append(f"Q{current} appears more than once; the first occurrence was used.")
            else:
                blocks[current] = body

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or PAGE_NUM_RE.match(line):
                continue
            marker = self._match_marker(line, valid_numbers, seen, sequential_bare)
            if marker:
                flush()
                current, rest = marker
                seen.add(current)
                buf = [rest] if rest else []
            elif current is None:
                header.append(line)
            else:
                buf.append(line)
        flush()

        # --- Fallback: if no numbered blocks were detected, try extracting from "Ans:" labels ---
        # This handles sheets where OCR drops question numbers but preserves "Ans:" labels,
        # e.g. EasyOCR reads "(1.) 25+10=?\n Ans: 35" as "25 + [0\n Ans: 35" (number lost).
        if not blocks and valid_numbers:
            fallback = self._extract_by_ans_labels(text, valid_numbers)
            if fallback:
                warnings.append("Question numbers were not detected; answers were matched by 'Ans:' label order.")
                return fallback, header, warnings

        return blocks, header, warnings

    def parse(self, pdf_path):
        """Backwards-compatible helper: PDF path -> {question_number: answer}."""
        from src.pdf_processor import PDFProcessor

        blocks, _, _ = self.parse_text(PDFProcessor().extract_text(pdf_path))
        return blocks

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _match_marker(line, valid_numbers, seen, sequential_bare):
        for rx in (PREFIX_RE, ANS_PREFIX_RE):
            m = rx.match(line)
            if m and int(m.group(1)) >= 1:
                return int(m.group(1)), m.group(2).strip()
        m = BARE_RE.match(line)
        if m:
            n = int(m.group(1))
            if n < 1 or n in seen:
                return None
            if valid_numbers is not None and n not in valid_numbers:
                return None
            if sequential_bare and n != (max(seen) + 1 if seen else 1):
                return None
            return n, m.group(2).strip()
        # Fallback: try bare number *without* delimiter (common in OCR output).
        # Only accept when valid_numbers is given and n is in that set,
        # to avoid false positives in long-form text.
        # m = BARE_NODELIM_RE.match(line)
        # if m:
        #     n = int(m.group(1))
        #     if n < 1 or n in seen:
        #         return None
        #     if valid_numbers is None or n not in valid_numbers:
        #         return None
        #     if sequential_bare and n != (max(seen) + 1 if seen else 1):
        #         return None
        #     return n, m.group(2).strip()
        # return None
                # Fallback for OCR-damaged numbering without punctuation.
        # Only use this when sequential_bare=True, such as when
        # parsing numbered question papers or answer keys.
        # For student answer sheets, "7 continents" could be an answer,
        # not a question marker.
        if sequential_bare:
            m = BARE_NODELIM_RE.match(line)
            if m:
                n = int(m.group(1))

                if n < 1 or n in seen:
                    return None

                if valid_numbers is not None and n not in valid_numbers:
                    return None

                if n != (max(seen) + 1 if seen else 1):
                    return None

                return n, m.group(2).strip()

        return None

    @staticmethod
    def _extract_by_ans_labels(text, valid_numbers):
        """Fallback: extract answers from 'Ans:' / 'Answer:' labels in sequential order.

        When OCR drops question numbers but preserves 'Ans:' labels, we assign
        answers to question numbers in order: first Ans: → Q1, second → Q2, etc.
        """
        ANS_LABEL_RE = re.compile(
            r"^\s*(?:ans(?:wer)?)\s*[:.)\-\u2013]\s*(.*)$",
            re.IGNORECASE,
        )
        expected = sorted(valid_numbers)
        answers = {}
        idx = 0
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = ANS_LABEL_RE.match(line)
            if m and idx < len(expected):
                ans_text = m.group(1).strip()
                if ans_text:  # skip empty "Ans:" lines
                    answers[expected[idx]] = ans_text
                idx += 1
        return answers if answers else None

    @staticmethod
    def clean_answer(block, question_text=""):
        """Remove an echoed question and/or an 'Answer:' label from a detected block.

        For fill-in-the-blank questions (containing blanks like ____ or = ___),
        also try to extract just the filled-in portion.
        """
        if not block:
            return ""
        m = ANS_SPLIT_RE.search(block)
        if m:
            return block[m.end():].strip()
        if question_text:
            q_norm = re.sub(r"\s+", " ", question_text).strip().lower()
            b_norm = re.sub(r"\s+", " ", block).strip().lower()
            if q_norm and b_norm.startswith(q_norm):
                return block[len(q_norm):].lstrip(" :-\u2013").strip()
            # Fill-in-the-blank: the question has underscores/blanks; the student's
            # answer echoes the question but with the blank replaced by a value.
            # Try to match the question text around the blank and extract the answer.
            if "_" in question_text or "____" in question_text:
                answer = _extract_fill_in_blank(block, question_text)
                if answer:
                    return answer
        return block

    @staticmethod
    def split_question_answer(block):
        """For answer keys that hold 'question ... Ans: answer' in one block."""
        m = ANS_SPLIT_RE.search(block)
        if m:
            return block[: m.start()].strip(), block[m.end():].strip()
        return "", block

    @staticmethod
    def extract_header(header_lines):
        """Find roll number / name in the lines above the first question."""
        roll = name = ""
        for line in header_lines[:20]:
            if not roll:
                m = ROLL_RE.search(line)
                if m:
                    roll = m.group(1).strip(" .")
            if not name:
                m = NAME_RE.search(line)
                if m:
                    name = m.group(1).strip(" .")
        return {"roll_no": roll, "name": name}


def _extract_fill_in_blank(block, question_text):
    """Extract the filled-in answer from a block that echoes a fill-in-the-blank question.

    Example: question = "25 + 10 = ____", block = "25 + Ic 35" -> "35"
             question = "1 rupee = ____ paise", block = "rupee [0o paise" -> "[0o paise"
    """
    # Split question on blanks (sequences of underscores)
    parts = re.split(r"_{2,}", question_text)
    if len(parts) < 2:
        return None
    # Normalise: lowercase, collapse whitespace, strip math operators for fuzzy matching
    def norm(s):
        return re.sub(r"\s+", " ", s).strip().lower()
    # The last non-empty part before the blank and the first non-empty part after
    before = norm(parts[0])
    after = norm(parts[-1]) if len(parts) > 1 else ""
    block_norm = norm(block)
    # Try to find the answer between the before/after context in the block
    # Use a fuzzy approach: find the last few words of "before" in the block
    if before:
        before_words = before.split()
        # Try matching the last 1-3 words of "before" at the start/middle of block
        for n_words in range(min(3, len(before_words)), 0, -1):
            tail = " ".join(before_words[-n_words:])
            idx = block_norm.find(tail)
            if idx >= 0:
                start = idx + len(tail)
                rest = block_norm[start:].strip()
                if after and rest.endswith(after.rstrip(".")):
                    rest = rest[:-(len(after.rstrip(".")))].strip()
                if rest:
                    # Return the corresponding portion from the original block
                    orig_rest = block[start:].strip()
                    if after:
                        after_clean = after.rstrip(".")
                        orig_norm = norm(orig_rest)
                        if orig_norm.endswith(after_clean):
                            orig_rest = orig_rest[:-(len(after_clean))].strip()
                    return orig_rest
    return None
