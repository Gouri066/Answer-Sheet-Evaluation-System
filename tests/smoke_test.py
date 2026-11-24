"""End-to-end check of the web app using Flask's test client.   python tests/smoke_test.py"""
import io
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["INSTANCE_DIR"] = tempfile.mkdtemp()
os.environ["TEACHER_USERNAME"] = "teacher"
os.environ["TEACHER_PASSWORD"] = "correct-horse-1"
os.environ["SEMANTIC_BACKEND"] = os.environ.get("SEMANTIC_BACKEND", "auto")

from webapp import create_app  # noqa: E402

app = create_app()
c = app.test_client()
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(url, data=None, csrf=None):
    data = dict(data or {})
    data["csrf_token"] = csrf or token(c.get("/login").get_data(as_text=True))
    return c.post(url, data=data, content_type="multipart/form-data")


# 1. everything requires login
for url in ("/", "/exams/new", "/exams/1", "/submissions/1", "/account", "/exams/1/export.csv"):
    r = c.get(url)
    ok(r.status_code == 302 and "/login" in r.headers["Location"], f"{url} redirects to login")
ok(c.get("/register").status_code == 404, "no registration page exists")

# 2. login
r = post("/login", {"username": "teacher", "password": "wrong"})
ok(b"Wrong username or password" in r.data, "wrong password rejected")
r = c.post("/login", data={"username": "teacher", "password": "correct-horse-1"})
ok(r.status_code == 400, "login without CSRF token rejected")
r = post("/login", {"username": "teacher", "password": "correct-horse-1"})
ok(r.status_code == 302, "teacher can sign in")
csrf = token(c.get("/").get_data(as_text=True))

# 3. create exam from the repo's CSV key
key = open(os.path.join(ROOT, "data", "QuestionAnswersDataSet.csv"), "rb").read()
r = post("/exams/new", {"title": "SE Interview", "subject": "CS", "default_marks": "5", "w_grammar": "20",
                        "w_keyword": "50", "w_semantic": "30", "answer_key": (io.BytesIO(key), "key.csv")}, csrf=csrf)
ok(r.status_code == 302 and "/exams/1/key" in r.headers["Location"], "exam created, key parsed")
page = c.get("/exams/1/key").get_data(as_text=True)
ok(page.count('name="number"') == 201, "key editor shows 200 questions (+1 template row)")

# 4. bulk upload the four sample sheets plus a hand-made one with typos / wrong answers
files = []
tests = os.path.join(ROOT, "Test")
for i, f in enumerate(sorted(os.listdir(tests))):
    files.append((io.BytesIO(open(os.path.join(tests, f), "rb").read()), f"S00{i+1}_Student_{i+1}.pdf"))
bad = "Name: Weak Student\nRoll No: W99\nQ1: Compilashun is when computer reads stuf\nQ2: Polymorphism is a type of food\nQ3: I do not know\n"
files.append((io.BytesIO(bad.encode()), "weak.txt"))
r = c.post("/exams/1/upload", data={"csrf_token": csrf, "sheets": files}, content_type="multipart/form-data")
ok(r.status_code == 302, "bulk upload accepted")
for _ in range(120):
    html = c.get("/exams/1").get_data(as_text=True)
    if "Scoring" not in html or "refreshes by itself" not in html:
        break
    time.sleep(1)
ok("refreshes by itself" not in html, "background scoring finished")
ok(html.count("badge done") == 5, "all 5 sheets scored")
ok("W99" in html and "Weak Student" in html, "name/roll read from sheet header")

# 5. scores
import sqlite3  # noqa: E402
db = sqlite3.connect(os.path.join(os.environ["INSTANCE_DIR"], "evaluation.db")); db.row_factory = sqlite3.Row
rows = {r["filename"]: r for r in db.execute("SELECT * FROM submissions")}
for fn, r in rows.items():
    print(f"   {fn:45s} {r['total']}/{r['max_total']} ({r['percentage']}%)")
ok(all(rows[f]["percentage"] > 99 for f in ("S001_Student_1.pdf", "S003_Student_3.pdf", "S004_Student_4.pdf")), "complete sheets score ~100%")
ok(rows["weak.txt"]["percentage"] < 5, "weak sheet scores very low")
ok(rows["S002_Student_2.pdf"]["percentage"] < 85, "sheet with ~37 unanswered questions scores lower")

# 6. detail page, CSV export, other teacher isolation
sid = rows["weak.txt"]["id"]
d = c.get(f"/submissions/{sid}").get_data(as_text=True)
ok("Compilashun" in d and "Student's answer" in d, "detail page shows detected answers")
r = c.get("/exams/1/export.csv")
ok(r.status_code == 200 and r.data.count(b"\n") >= 6, "CSV export works")
ok(r.data.decode("utf-8-sig").splitlines()[0].startswith("Roll No,Name,Status,Total"), "CSV header ok")

# 7. editing key marks sheets stale; re-evaluate works
form = {"number": ["1"], "question": ["Q"], "answer": ["Compilation translates source code into machine code."],
        "marks": ["10"], "wg": ["20"], "wk": ["50"], "ws": ["30"], "keywords": [""], "csrf_token": csrf}
time.sleep(1.5)  # timestamps have 1-second resolution
r = c.post("/exams/1/key", data=form)
ok(r.status_code == 302, "key edit saved")
time.sleep(1.1)
ok("were scored" in c.get("/exams/1").get_data(as_text=True) or "was scored" in c.get("/exams/1").get_data(as_text=True), "stale-score banner shown")
c.post("/exams/1/reevaluate", data={"csrf_token": csrf})
time.sleep(3)
ok(c.get("/exams/1").get_data(as_text=True).count("badge done") == 5, "re-evaluation completed")

# 8. second teacher cannot see first teacher's exam
from werkzeug.security import generate_password_hash  # noqa: E402
db.execute("INSERT INTO teachers (username, password_hash, created_at) VALUES ('other', ?, 'x')", (generate_password_hash("other-pass-123"),)); db.commit()
c2 = app.test_client()
t2 = token(c2.get("/login").get_data(as_text=True))
c2.post("/login", data={"username": "other", "password": "other-pass-123", "csrf_token": t2})
ok(c2.get("/exams/1").status_code == 404 and c2.get(f"/submissions/{sid}").status_code == 404, "another teacher gets 404 for these records")
print("\nAll checks passed.")
