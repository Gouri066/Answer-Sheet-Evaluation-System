import os
import sqlite3
from datetime import datetime, timezone

from flask import g

from config.paths import DB_PATH, INSTANCE_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT UNIQUE,
    teacher_code TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    default_marks REAL NOT NULL,
    w_grammar REAL NOT NULL,
    w_keyword REAL NOT NULL,
    w_semantic REAL NOT NULL,
    paper_file TEXT NOT NULL DEFAULT '',
    key_file TEXT NOT NULL DEFAULT '',
    key_updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    model_answer TEXT NOT NULL DEFAULT '',
    max_marks REAL NOT NULL,
    w_grammar REAL NOT NULL,
    w_keyword REAL NOT NULL,
    w_semantic REAL NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    UNIQUE (exam_id, number)
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    roll_no TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    raw_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    total REAL,
    max_total REAL,
    percentage REAL,
    backend TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    evaluated_at TEXT
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    answer_text TEXT NOT NULL DEFAULT '',
    grammar REAL, keyword REAL, semantic REAL,
    marks REAL NOT NULL,
    max_marks REAL NOT NULL,
    matched TEXT NOT NULL DEFAULT '[]',
    missing TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sub_exam ON submissions(exam_id);
CREATE INDEX IF NOT EXISTS idx_ans_sub ON answers(submission_id);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# def init_db():
#     conn = connect()
#     conn.executescript(SCHEMA)
#     conn.commit()
#     conn.close()

def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)

        # Add new teacher account fields to existing databases
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(teachers)").fetchall()
        }

        if "email" not in columns:
            conn.execute("ALTER TABLE teachers ADD COLUMN email TEXT")

        if "teacher_code" not in columns:
            conn.execute("ALTER TABLE teachers ADD COLUMN teacher_code TEXT")

        # Ensure email and Teacher Code are unique
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_email "
            "ON teachers(email)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_code "
            "ON teachers(teacher_code)"
        )

        conn.commit()
    finally:
        conn.close()
        
def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
