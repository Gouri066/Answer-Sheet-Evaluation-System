"""Scoring: grammar (spelling) + keyword coverage + semantic similarity.

Same three-part method as the original prototype, now driven by question records
(from the database) instead of three separate CSV files:

    marks = max_marks * (wg*grammar + wk*keyword + ws*semantic) / (wg + wk + ws)

Improvements over the prototype:
  * keywords are generated automatically from the model answer (teacher can edit them)
    and matched on word stems, so "inherits" matches "inheritance";
  * technical words from the question / model answer are never counted as spelling errors;
  * grammar credit is scaled by relevance, so a well-spelled but unrelated answer earns nothing;
  * semantic similarity is calibrated and batched, and falls back to TF-IDF cosine similarity
    when sentence-transformers (or its model download) is unavailable.
"""
import logging
import os
import re
import threading
from functools import lru_cache

import numpy as np

log = logging.getLogger(__name__)

try:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    STOPWORDS = set(ENGLISH_STOP_WORDS)
except Exception:  # pragma: no cover
    STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "on", "with", "as", "by"}

try:
    from nltk.stem.porter import PorterStemmer

    _porter = PorterStemmer()

    @lru_cache(maxsize=50000)
    def stem(word):
        return _porter.stem(word)

except Exception:  # pragma: no cover

    @lru_cache(maxsize=50000)
    def stem(word):
        for suf in ("ing", "ed", "es", "s"):
            if word.endswith(suf) and len(word) - len(suf) >= 3:
                return word[: -len(suf)]
        return word


TOKEN_RE = re.compile(r"[a-z0-9]+")
WORD_RE = re.compile(r"[A-Za-z']+")


def tokens(text):
    return TOKEN_RE.findall((text or "").lower())


def generate_keywords(model_answer, question="", limit=12):
    """Pick the most informative content words from the model answer."""
    seen, words = set(), []
    for i, w in enumerate(tokens(model_answer)):
        if len(w) < 3 or w in STOPWORDS or w.isdigit():
            continue
        s = stem(w)
        if s in seen:
            continue
        seen.add(s)
        words.append((w, i))
    top = sorted(words, key=lambda x: (-len(x[0]), x[1]))[:limit]
    return [w for w, _ in sorted(top, key=lambda x: x[1])]


# --------------------------------------------------------------------- spelling
class SpellChecker:
    _inst = None
    _lock = threading.Lock()

    def __init__(self):
        import textblob

        path = os.path.join(os.path.dirname(textblob.__file__), "en", "en-spelling.txt")
        self.words = set()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(";;;") or not line.strip():
                    continue
                self.words.add(line.split()[0].lower())

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._inst is None:
                try:
                    cls._inst = cls()
                except Exception as exc:  # textblob missing -> grammar check disabled
                    log.warning("Spell checker unavailable (%s); grammar score will be 1.0", exc)
                    cls._inst = False
            return cls._inst or None

    def known(self, w):
        w = w.lower()
        if w in self.words:
            return True
        for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""),
                         ("ing", "e"), ("ly", ""), ("er", ""), ("ers", ""), ("ness", ""), ("ment", ""),
                         ("ments", ""), ("tion", "te"), ("ions", "ion")):
            if w.endswith(suf) and len(w) > len(suf) + 2 and (w[: -len(suf)] + rep) in self.words:
                return True
        # doubled final consonant: running -> runn -> run
        for suf in ("ing", "ed"):
            if w.endswith(suf) and len(w) > len(suf) + 3 and w[-len(suf) - 1] == w[-len(suf) - 2]:
                if w[: -len(suf) - 1] in self.words:
                    return True
        return False


def grammar_score(text, whitelist, speller):
    """1 - (misspelled words / checked words). Acronyms, numbers and teacher terms are ignored."""
    if speller is None:
        return 1.0, 0
    checked = errors = 0
    for w in WORD_RE.findall(text or ""):
        if len(w) < 3 or "'" in w or w.isupper():
            continue
        checked += 1
        if w.lower() not in whitelist and not speller.known(w):
            errors += 1
    if checked == 0:
        return 1.0, 0
    return max(0.0, 1.0 - errors / checked), errors


# --------------------------------------------------------------------- semantic backends
_st_model = None
_st_lock = threading.Lock()
SEM_FLOOR, SEM_CEIL = 0.20, 0.85  # transformer cosine range mapped to 0..1
TFIDF_CEIL = 0.70


def _load_backend(preference):
    global _st_model
    if preference in ("auto", "transformer"):
        with _st_lock:
            try:
                if _st_model is None:
                    from sentence_transformers import SentenceTransformer

                    _st_model = SentenceTransformer(os.getenv("SEMANTIC_MODEL", "all-MiniLM-L6-v2"))
                return "transformer", _st_model
            except Exception as exc:
                if preference == "transformer":
                    raise
                log.warning("sentence-transformers unavailable (%s). Using TF-IDF similarity instead.", exc)
    return "tfidf", None


class ScoringEngine:
    """Build once per exam (encodes model answers), then score many students."""

    def __init__(self, questions, backend=None):
        self.questions = {int(q["number"]): q for q in questions}
        self.backend, self._model = _load_backend((backend or os.getenv("SEMANTIC_BACKEND", "auto")).lower())
        self.speller = SpellChecker.get()
        self._kw, self._white = {}, {}
        for n, q in self.questions.items():
            kws = list(q.get("keywords") or []) or generate_keywords(q.get("model_answer", ""), q.get("question", ""))
            self._kw[n] = [(k, tuple(stem(t) for t in tokens(k))) for k in kws if tokens(k)]
            self._white[n] = set(tokens(q.get("question", ""))) | set(tokens(q.get("model_answer", ""))) | {
                t for k in kws for t in tokens(k)
            }
        self._prepare_semantic()

    # -- semantic -----------------------------------------------------------
    def _prepare_semantic(self):
        nums = [n for n, q in self.questions.items() if (q.get("model_answer") or "").strip()]
        self._model_vec = {}
        if not nums:
            return
        answers = [self.questions[n]["model_answer"] for n in nums]
        if self.backend == "transformer":
            emb = self._model.encode(answers, normalize_embeddings=True, show_progress_bar=False)
            self._model_vec = {n: emb[i] for i, n in enumerate(nums)}
        else:
            from sklearn.feature_extraction.text import TfidfVectorizer

            corpus = answers + [self.questions[n].get("question", "") for n in nums]
            self._tfidf = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
            self._tfidf.fit(corpus)
            mat = self._tfidf.transform(answers)
            self._model_vec = {n: mat[i] for i, n in enumerate(nums)}

    def _semantic_batch(self, items):
        """items: [(question_number, student_text)] -> {question_number: 0..1}"""
        items = [(n, t) for n, t in items if n in self._model_vec and t.strip()]
        if not items:
            return {}
        texts = [t for _, t in items]
        out = {}
        if self.backend == "transformer":
            emb = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            for (n, _), e in zip(items, emb):
                cos = float(np.dot(e, self._model_vec[n]))
                out[n] = min(1.0, max(0.0, (cos - SEM_FLOOR) / (SEM_CEIL - SEM_FLOOR)))
        else:
            mat = self._tfidf.transform(texts)
            for i, (n, _) in enumerate(items):
                cos = float(mat[i].multiply(self._model_vec[n]).sum())
                out[n] = min(1.0, max(0.0, cos / TFIDF_CEIL))
        return out

    # -- keywords -----------------------------------------------------------
    def keyword_score(self, text, n):
        kws = self._kw.get(n) or []
        if not kws:
            return None, [], []
        stems = [stem(t) for t in tokens(text)]
        stem_set = set(stems)
        matched, missing = [], []
        for kw, kstems in kws:
            if len(kstems) == 1:
                ok = kstems[0] in stem_set
            else:
                ok = any(tuple(stems[i : i + len(kstems)]) == kstems for i in range(len(stems) - len(kstems) + 1))
            (matched if ok else missing).append(kw)
        return len(matched) / len(kws), matched, missing

    # -- main entry ----------------------------------------------------------
    def score_answers(self, answers):
        """answers: {question_number: text}. Returns one result dict per question in the key."""
        sem = self._semantic_batch([(n, answers.get(n, "")) for n in self.questions])
        results = []
        for n in sorted(self.questions):
            q = self.questions[n]
            text = (answers.get(n) or "").strip()
            res = {
                "number": n, "answer_text": text, "grammar": 0.0, "keyword": 0.0, "semantic": 0.0,
                "marks": 0.0, "max_marks": float(q["max_marks"]), "matched": [], "missing": [], "note": "",
            }
            if not (q.get("model_answer") or "").strip():
                res["note"] = "No model answer in the key"
            elif not text:
                res["note"] = "No answer found"
                res["missing"] = [k for k, _ in self._kw.get(n, [])]
            else:
                k, matched, missing = self.keyword_score(text, n)
                s = sem.get(n, 0.0)
                g, _errs = grammar_score(text, self._white[n], self.speller)
                relevance = max(k or 0.0, s)
                g_eff = g * min(1.0, relevance * 2)  # grammar only counts if the answer is on topic
                wg, wk, ws = float(q["w_grammar"]), float(q["w_keyword"]), float(q["w_semantic"])
                if k is None:  # no keywords defined: hand their weight to the other two parts
                    wk, k = 0.0, 0.0
                total_w = wg + wk + ws
                frac = (wg * g_eff + wk * k + ws * s) / total_w if total_w > 0 else 0.0
                res.update(grammar=g, keyword=k, semantic=s, matched=matched, missing=missing,
                           marks=round(res["max_marks"] * min(1.0, max(0.0, frac)), 2))
            results.append(res)
        return results
