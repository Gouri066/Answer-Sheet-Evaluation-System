import csv
import io
import json
import os
import re
import shutil
import uuid
import zipfile
import secrets
import string
import sqlite3

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config.paths import DEFAULT_MARKS, DEFAULT_WEIGHTS, UPLOAD_DIR
from src.key_importer import build_questions
from src.scoring_engine import generate_keywords
from webapp import auth, worker
from webapp.auth import login_required
from webapp.db import get_db, now

bp = Blueprint("main", __name__)

KEY_EXTS = {".pdf", ".csv", ".txt"}
SHEET_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
MAX_ZIP_MEMBER = 100 * 1024 * 1024
MAX_ZIP_TOTAL = 1024 * 1024 * 1024


# ----------------------------------------------------------------- helpers
def ext_of(name):
    return os.path.splitext(name or "")[1].lower()


def owned_exam(exam_id):
    exam = get_db().execute("SELECT * FROM exams WHERE id = ? AND teacher_id = ?", (exam_id, g.teacher["id"])).fetchone()
    if exam is None:
        abort(404)
    return exam


def owned_submission(sub_id):
    row = get_db().execute(
        "SELECT s.* FROM submissions s JOIN exams e ON e.id = s.exam_id WHERE s.id = ? AND e.teacher_id = ?",
        (sub_id, g.teacher["id"])).fetchone()
    if row is None:
        abort(404)
    return row


def exam_dir(exam_id, *parts):
    path = os.path.join(UPLOAD_DIR, f"exam_{exam_id}", *parts)
    os.makedirs(path, exist_ok=True)
    return path


def save_upload(file_storage, folder):
    name = secure_filename(os.path.basename(file_storage.filename)) or "file"
    path = os.path.join(folder, f"{uuid.uuid4().hex[:8]}_{name}")
    file_storage.save(path)
    return path


def num(value, default, low=0.0, high=1000.0):
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(high, max(low, v))


def guess_identity(filename):
    """S001_John_Doe.pdf -> ('S001', 'John Doe')"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    m = re.match(r"^([A-Za-z]*\d[A-Za-z0-9]*)[\s_\-]+(.+)$", stem)
    if m:
        return m.group(1), re.sub(r"[_\-]+", " ", m.group(2)).strip()
    if re.fullmatch(r"[A-Za-z]*\d[A-Za-z0-9]*", stem):
        return stem, ""
    return "", re.sub(r"[_\-]+", " ", stem).strip()


def csv_safe(value):
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def replace_questions(db, exam_id, questions):
    db.execute("DELETE FROM questions WHERE exam_id = ?", (exam_id,))
    db.executemany(
        "INSERT INTO questions (exam_id, number, question, model_answer, max_marks, w_grammar, w_keyword,"
        " w_semantic, keywords) VALUES (?,?,?,?,?,?,?,?,?)",
        [(exam_id, q["number"], q["question"], q["model_answer"], q["max_marks"], q["w_grammar"], q["w_keyword"],
          q["w_semantic"], json.dumps(q["keywords"])) for q in questions])
    db.execute("UPDATE exams SET key_updated_at = ? WHERE id = ?", (now(), exam_id))


def import_key_files(db, exam, key_file, paper_file):
    """Save the uploaded files, parse them and replace the exam's questions. Raises ValueError."""
    docs = exam_dir(exam["id"], "docs")
    key_path = save_upload(key_file, docs)
    paper_path = save_upload(paper_file, docs) if paper_file and paper_file.filename else None
    weights = {"grammar": exam["w_grammar"], "keyword": exam["w_keyword"], "semantic": exam["w_semantic"]}
    questions = build_questions(key_path, paper_path, exam["default_marks"], weights)
    replace_questions(db, exam["id"], questions)
    db.execute("UPDATE exams SET key_file = ?, paper_file = ? WHERE id = ?",
               (os.path.basename(key_path), os.path.basename(paper_path) if paper_path else "", exam["id"]))
    return len(questions)


def add_sheet(db, exam_id, original_name, writer):
    folder = exam_dir(exam_id, "sheets")
    safe = secure_filename(os.path.basename(original_name)) or "sheet"
    path = os.path.join(folder, f"{uuid.uuid4().hex[:8]}_{safe}")
    writer(path)
    roll, name = guess_identity(original_name)
    db.execute(
        "INSERT INTO submissions (exam_id, roll_no, name, filename, stored_path, status, uploaded_at)"
        " VALUES (?,?,?,?,?, 'pending', ?)", (exam_id, roll, name, os.path.basename(original_name), path, now()))


# ----------------------------------------------------------------- auth
def generate_teacher_code(username):
    prefix = re.sub(r"[^a-z0-9]", "", username.lower())

    while True:
        length = secrets.choice((3, 4))
        digits = "".join(secrets.choice(string.digits) for _ in range(length))
        code = prefix + digits

        exists = get_db().execute(
            "SELECT id FROM teachers WHERE teacher_code = ?", (code,)
        ).fetchone()

        if not exists:
            return code


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.teacher:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            flash("Username must be 3–30 characters using letters, numbers or underscores.", "error")

        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")

        elif len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")

        elif password != confirm:
            flash("Passwords do not match.", "error")

        else:
            db = get_db()

            existing = db.execute(
                "SELECT id FROM teachers WHERE username = ? OR email = ?",
                (username, email)
            ).fetchone()

            if existing:
                flash("Username or email is already registered.", "error")
            else:
                code = generate_teacher_code(username)

                try:
                    db.execute(
                        "INSERT INTO teachers "
                        "(username, email, teacher_code, password_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            username,
                            email,
                            code,
                            generate_password_hash(password),
                            now()
                        )
                    )
                    db.commit()

                    return render_template(
                        "register.html",
                        teacher_code=code
                    )

                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Account details already exist. Please try another username or email.", "error")

    return render_template("register.html", teacher_code=None)

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.teacher:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        addr = request.remote_addr or "?"

        if auth.is_locked(addr):
            flash("Too many failed attempts. Wait a few minutes and try again.", "error")
            return render_template("login.html"), 429

        identifier = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        teacher_code = request.form.get("teacher_code", "").strip().lower()

        row = get_db().execute(
            "SELECT * FROM teachers WHERE lower(username) = ? OR lower(email) = ?",
            (identifier, identifier)
        ).fetchone()

        password_ok = check_password_hash(
            row["password_hash"] if row else generate_password_hash("dummy-password"),
            password
        )

        code_ok = (
            row is not None
            and row["teacher_code"] is not None
            and secrets.compare_digest(row["teacher_code"].lower(), teacher_code)
        )

        if row and password_ok and code_ok:
            auth.clear_failures(addr)
            session.clear()
            session["teacher_id"] = row["id"]
            session.permanent = True
            return redirect(auth.safe_next(request.args.get("next")))

        auth.record_failure(addr)
        flash("Incorrect username/email, password or Teacher Code.", "error")

    return render_template("login.html")

@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        row = get_db().execute("SELECT * FROM teachers WHERE id = ?", (g.teacher["id"],)).fetchone()
        new, again = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        if not check_password_hash(row["password_hash"], request.form.get("current_password", "")):
            flash("The current password is wrong.", "error")
        elif len(new) < 8:
            flash("The new password needs at least 8 characters.", "error")
        elif new != again:
            flash("The two new passwords don't match.", "error")
        else:
            get_db().execute("UPDATE teachers SET password_hash = ? WHERE id = ?", (generate_password_hash(new), row["id"]))
            get_db().commit()
            flash("Password changed.", "ok")
            return redirect(url_for("main.account"))
    return render_template("account.html")


# ----------------------------------------------------------------- exams
@bp.route("/")
@login_required
def dashboard():
    exams = get_db().execute(
        "SELECT e.*, (SELECT COUNT(*) FROM questions q WHERE q.exam_id = e.id) AS n_questions,"
        " (SELECT COUNT(*) FROM submissions s WHERE s.exam_id = e.id) AS n_sheets,"
        " (SELECT COUNT(*) FROM submissions s WHERE s.exam_id = e.id AND s.status = 'done') AS n_done"
        " FROM exams e WHERE e.teacher_id = ? ORDER BY e.id DESC", (g.teacher["id"],)).fetchall()
    return render_template("dashboard.html", exams=exams)


@bp.route("/exams/new", methods=["GET", "POST"])
@login_required
def exam_new():
    form = {"title": "", "subject": "", "default_marks": DEFAULT_MARKS, **{f"w_{k}": v for k, v in DEFAULT_WEIGHTS.items()}}
    if request.method == "POST":
        form.update(title=request.form.get("title", "").strip()[:150], subject=request.form.get("subject", "").strip()[:100],
                    default_marks=num(request.form.get("default_marks"), DEFAULT_MARKS, 0.5, 100),
                    w_grammar=num(request.form.get("w_grammar"), 20, 0, 100),
                    w_keyword=num(request.form.get("w_keyword"), 50, 0, 100),
                    w_semantic=num(request.form.get("w_semantic"), 30, 0, 100))
        key_file, paper_file = request.files.get("answer_key"), request.files.get("question_paper")
        error = None
        if not form["title"]:
            error = "Give the exam a title."
        elif not key_file or not key_file.filename:
            error = "Upload the answer key."
        elif ext_of(key_file.filename) not in KEY_EXTS:
            error = "The answer key must be a PDF, CSV or TXT file."
        elif paper_file and paper_file.filename and ext_of(paper_file.filename) not in KEY_EXTS:
            error = "The question paper must be a PDF, CSV or TXT file."
        elif form["w_grammar"] + form["w_keyword"] + form["w_semantic"] <= 0:
            error = "At least one scoring weight must be above zero."
        if not error:
            db = get_db()
            cur = db.execute(
                "INSERT INTO exams (teacher_id, title, subject, default_marks, w_grammar, w_keyword, w_semantic,"
                " key_updated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (g.teacher["id"], form["title"], form["subject"], form["default_marks"], form["w_grammar"],
                 form["w_keyword"], form["w_semantic"], now(), now()))
            exam = db.execute("SELECT * FROM exams WHERE id = ?", (cur.lastrowid,)).fetchone()
            try:
                count = import_key_files(db, exam, key_file, paper_file)
                db.commit()
                flash(f"Found {count} questions. Check them below before uploading answer sheets.", "ok")
                return redirect(url_for("main.exam_key", exam_id=exam["id"]))
            except Exception as exc:
                db.rollback()
                shutil.rmtree(os.path.join(UPLOAD_DIR, f"exam_{exam['id']}"), ignore_errors=True)
                error = str(exc) if isinstance(exc, ValueError) else f"Could not read those files: {exc}"
        flash(error, "error")
    return render_template("exam_new.html", form=form)


@bp.route("/exams/<int:exam_id>")
@login_required
def exam_detail(exam_id):
    exam = owned_exam(exam_id)
    db = get_db()
    sort = request.args.get("sort", "roll")
    order = {"roll": "s.roll_no COLLATE NOCASE, s.name COLLATE NOCASE, s.id",
             "name": "s.name COLLATE NOCASE, s.id",
             "score": "s.percentage DESC, s.id"}.get(sort, "s.id")
    subs = db.execute(f"SELECT s.* FROM submissions s WHERE s.exam_id = ? ORDER BY {order}", (exam_id,)).fetchall()
    q_stats = db.execute("SELECT COUNT(*) AS n, SUM(model_answer = '') AS no_answer, SUM(max_marks) AS total_marks"
                         " FROM questions WHERE exam_id = ?", (exam_id,)).fetchone()
    done = [s for s in subs if s["status"] == "done"]
    stats = None
    if done:
        pcts = [s["percentage"] for s in done]
        stats = {"n": len(done), "avg": sum(pcts) / len(pcts), "max": max(pcts), "min": min(pcts)}
    pending = sum(1 for s in subs if s["status"] in ("pending", "processing"))
    stale = db.execute("SELECT COUNT(*) FROM submissions WHERE exam_id = ? AND status = 'done' AND evaluated_at < ?",
                       (exam_id, exam["key_updated_at"])).fetchone()[0]
    rolls = [s["roll_no"].lower() for s in subs if s["roll_no"]]
    dupes = {r for r in rolls if rolls.count(r) > 1}
    return render_template("exam.html", exam=exam, subs=subs, q=q_stats, stats=stats, pending=pending, stale=stale,
                           dupes=dupes, sort=sort)


@bp.route("/exams/<int:exam_id>/key", methods=["GET", "POST"])
@login_required
def exam_key(exam_id):
    exam = owned_exam(exam_id)
    db = get_db()
    errors, rows = [], None
    if request.method == "POST":
        f = request.form
        fields = [f.getlist(k) for k in ("number", "question", "answer", "marks", "wg", "wk", "ws", "keywords")]
        n_rows = min(len(x) for x in fields)
        rows, seen = [], set()
        for i in range(n_rows):
            number, question, answer, marks, wg, wk, ws, kw = (x[i].strip() for x in fields)
            if not (number or question or answer):
                continue  # blank row
            row = {"number": number, "question": question, "model_answer": answer, "max_marks": marks,
                   "w_grammar": wg, "w_keyword": wk, "w_semantic": ws, "keywords_text": kw}
            rows.append(row)
            try:
                n = int(number)
                assert n >= 1
            except (ValueError, AssertionError):
                errors.append(f"Row {i + 1}: the question number must be a whole number (1, 2, 3 ...).")
                continue
            if n in seen:
                errors.append(f"Question {n} appears twice.")
            seen.add(n)
            m = num(marks, -1, -1, 1000)
            weights = [num(wg, -1, -1, 100), num(wk, -1, -1, 100), num(ws, -1, -1, 100)]
            if m <= 0:
                errors.append(f"Q{n}: marks must be above zero.")
            if min(weights) < 0 or sum(weights) <= 0:
                errors.append(f"Q{n}: weights must be 0-100 and not all zero.")
        if not rows:
            errors.append("Add at least one question.")
        if not errors:
            questions = []
            for r in rows:
                kws = [k.strip() for k in re.split(r"[,\n]", r["keywords_text"]) if k.strip()]
                if not kws and r["model_answer"]:
                    kws = generate_keywords(r["model_answer"], r["question"])
                questions.append({"number": int(r["number"]), "question": r["question"], "model_answer": r["model_answer"],
                                  "max_marks": float(r["max_marks"]), "w_grammar": float(r["w_grammar"]),
                                  "w_keyword": float(r["w_keyword"]), "w_semantic": float(r["w_semantic"]), "keywords": kws})
            questions.sort(key=lambda q: q["number"])
            replace_questions(db, exam_id, questions)
            db.commit()
            flash(f"Saved {len(questions)} questions.", "ok")
            return redirect(url_for("main.exam_detail", exam_id=exam_id))
        for e in errors[:8]:
            flash(e, "error")
    if rows is None:
        rows = []
        for q in db.execute("SELECT * FROM questions WHERE exam_id = ? ORDER BY number", (exam_id,)):
            rows.append({**dict(q), "keywords_text": ", ".join(json.loads(q["keywords"] or "[]"))})
    return render_template("key.html", exam=exam, rows=rows)


@bp.route("/exams/<int:exam_id>/import", methods=["POST"])
@login_required
def exam_import(exam_id):
    exam = owned_exam(exam_id)
    key_file, paper_file = request.files.get("answer_key"), request.files.get("question_paper")
    if not key_file or not key_file.filename or ext_of(key_file.filename) not in KEY_EXTS:
        flash("Choose an answer key file (PDF, CSV or TXT).", "error")
        return redirect(url_for("main.exam_detail", exam_id=exam_id))
    if paper_file and paper_file.filename and ext_of(paper_file.filename) not in KEY_EXTS:
        flash("The question paper must be a PDF, CSV or TXT file.", "error")
        return redirect(url_for("main.exam_detail", exam_id=exam_id))
    db = get_db()
    try:
        count = import_key_files(db, exam, key_file, paper_file)
        db.commit()
        flash(f"Replaced the key with {count} questions. Re-evaluate the sheets to apply it.", "ok")
        return redirect(url_for("main.exam_key", exam_id=exam_id))
    except Exception as exc:
        db.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else f"Could not read those files: {exc}", "error")
        return redirect(url_for("main.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/upload", methods=["POST"])
@login_required
def upload_sheets(exam_id):
    owned_exam(exam_id)
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM questions WHERE exam_id = ?", (exam_id,)).fetchone()[0] == 0:
        flash("Add the questions and answers first.", "error")
        return redirect(url_for("main.exam_detail", exam_id=exam_id))
    added, skipped = 0, []
    for f in request.files.getlist("sheets"):
        if not f.filename:
            continue
        ext = ext_of(f.filename)
        if ext == ".zip":
            try:
                with zipfile.ZipFile(f.stream) as zf:
                    total = 0
                    for info in zf.infolist():
                        base = os.path.basename(info.filename)
                        if info.is_dir() or not base or base.startswith(".") or "__MACOSX" in info.filename:
                            continue
                        total += info.file_size
                        if ext_of(base) not in SHEET_EXTS or info.file_size > MAX_ZIP_MEMBER or total > MAX_ZIP_TOTAL:
                            skipped.append(base)
                            continue

                        def write(path, info=info, zf=zf):
                            with zf.open(info) as src, open(path, "wb") as dst:
                                shutil.copyfileobj(src, dst)

                        add_sheet(db, exam_id, base, write)
                        added += 1
            except zipfile.BadZipFile:
                skipped.append(f.filename)
        elif ext in SHEET_EXTS:
            add_sheet(db, exam_id, f.filename, lambda path, f=f: f.save(path))
            added += 1
        else:
            skipped.append(f.filename)
    db.commit()
    if added:
        worker.enqueue(exam_id)
        flash(f"Uploaded {added} answer sheet{'s' if added != 1 else ''}. Scoring has started.", "ok")
    if skipped:
        flash("Skipped (unsupported type): " + ", ".join(skipped[:6]) + (" ..." if len(skipped) > 6 else ""), "error")
    if not added and not skipped:
        flash("Choose at least one answer sheet.", "error")
    return redirect(url_for("main.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/reevaluate", methods=["POST"])
@login_required
def reevaluate(exam_id):
    owned_exam(exam_id)
    db = get_db()
    db.execute("UPDATE submissions SET status = 'pending' WHERE exam_id = ? AND status IN ('done', 'error')", (exam_id,))
    db.commit()
    worker.enqueue(exam_id)
    flash("Scoring every sheet again with the current key.", "ok")
    return redirect(url_for("main.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/export.csv")
@login_required
def export_csv(exam_id):
    exam = owned_exam(exam_id)
    db = get_db()
    qs = db.execute("SELECT number FROM questions WHERE exam_id = ? ORDER BY number", (exam_id,)).fetchall()
    subs = db.execute("SELECT * FROM submissions WHERE exam_id = ? ORDER BY roll_no COLLATE NOCASE, id", (exam_id,)).fetchall()
    marks = {}
    for a in db.execute("SELECT a.submission_id, a.number, a.marks FROM answers a JOIN submissions s ON s.id = a.submission_id"
                        " WHERE s.exam_id = ?", (exam_id,)):
        marks[(a["submission_id"], a["number"])] = a["marks"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Roll No", "Name", "Status", "Total", "Out of", "Percentage"] + [f"Q{q['number']}" for q in qs])
    for s in subs:
        w.writerow([csv_safe(s["roll_no"]), csv_safe(s["name"]), s["status"],
                    "" if s["total"] is None else s["total"], "" if s["max_total"] is None else s["max_total"],
                    "" if s["percentage"] is None else s["percentage"]]
                   + [marks.get((s["id"], q["number"]), "") for q in qs])
    fname = re.sub(r"[^A-Za-z0-9_-]+", "_", exam["title"]).strip("_") or "results"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}_results.csv"'})


@bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
@login_required
def exam_delete(exam_id):
    owned_exam(exam_id)
    db = get_db()
    db.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
    db.commit()
    shutil.rmtree(os.path.join(UPLOAD_DIR, f"exam_{exam_id}"), ignore_errors=True)
    flash("Exam deleted.", "ok")
    return redirect(url_for("main.dashboard"))


# ----------------------------------------------------------------- submissions
@bp.route("/submissions/<int:sub_id>")
@login_required
def submission_detail(sub_id):
    sub = owned_submission(sub_id)
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE id = ?", (sub["exam_id"],)).fetchone()
    rows = []
    for a in db.execute(
            "SELECT a.*, q.question, q.model_answer, q.w_grammar, q.w_keyword, q.w_semantic FROM answers a"
            " LEFT JOIN questions q ON q.exam_id = ? AND q.number = a.number WHERE a.submission_id = ? ORDER BY a.number",
            (sub["exam_id"], sub_id)):
        rows.append({**dict(a), "matched": json.loads(a["matched"]), "missing": json.loads(a["missing"])})
    return render_template("submission.html", sub=sub, exam=exam, rows=rows)


@bp.route("/submissions/<int:sub_id>/identity", methods=["POST"])
@login_required
def submission_identity(sub_id):
    owned_submission(sub_id)
    get_db().execute("UPDATE submissions SET roll_no = ?, name = ? WHERE id = ?",
                     (request.form.get("roll_no", "").strip()[:40], request.form.get("name", "").strip()[:100], sub_id))
    get_db().commit()
    flash("Saved.", "ok")
    return redirect(url_for("main.submission_detail", sub_id=sub_id))


@bp.route("/submissions/<int:sub_id>/file")
@login_required
def submission_file(sub_id):
    sub = owned_submission(sub_id)
    if not os.path.exists(sub["stored_path"]):
        abort(404)
    return send_file(sub["stored_path"], download_name=sub["filename"])


@bp.route("/submissions/<int:sub_id>/delete", methods=["POST"])
@login_required
def submission_delete(sub_id):
    sub = owned_submission(sub_id)
    db = get_db()
    db.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
    db.commit()
    try:
        os.remove(sub["stored_path"])
    except OSError:
        pass
    flash("Answer sheet removed.", "ok")
    return redirect(url_for("main.exam_detail", exam_id=sub["exam_id"]))
