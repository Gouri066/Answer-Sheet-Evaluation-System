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

PAGE_NUM_RE = re.compile(r"^\s*(?:page\s*)?\d{1,3}(?:\s*(?:of|/)\s*\d{1,3})?\s*$", re.IGNORECASE)
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
        return None

    @staticmethod
    def clean_answer(block, question_text=""):
        """Remove an echoed question and/or an 'Answer:' label from a detected block."""
        if not block:
            return ""
        m = ANS_SPLIT_RE.search(block)
        if m:
            return block[m.end():].strip()
        if question_text:
            q = re.sub(r"\s+", " ", question_text).strip().lower()
            if q and block.lower().startswith(q):
                return block[len(q):].lstrip(" :-\u2013").strip()
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
