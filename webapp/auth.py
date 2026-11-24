import secrets
import time
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, redirect, request, session, url_for

from webapp.db import get_db

MAX_FAILS, WINDOW = 5, 600  # 5 wrong passwords per 10 minutes per address
_fails = {}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.teacher is None:
            return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def load_teacher():
    g.teacher = None
    tid = session.get("teacher_id")
    if tid:
        g.teacher = get_db().execute("SELECT id, username FROM teachers WHERE id = ?", (tid,)).fetchone()
        if g.teacher is None:
            session.clear()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Reload the page and try again.")


def is_locked(addr):
    hits = [t for t in _fails.get(addr, []) if time.time() - t < WINDOW]
    _fails[addr] = hits
    return len(hits) >= MAX_FAILS


def record_failure(addr):
    _fails.setdefault(addr, []).append(time.time())


def clear_failures(addr):
    _fails.pop(addr, None)


def safe_next(target):
    """Only allow redirects to paths on this site."""
    if target and target.startswith("/") and not target.startswith("//") and not urlparse(target).netloc:
        return target
    return url_for("main.dashboard")
