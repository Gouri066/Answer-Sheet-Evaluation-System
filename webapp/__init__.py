import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, g, render_template, request
from werkzeug.security import generate_password_hash

from config.paths import INSTANCE_DIR, UPLOAD_DIR
from webapp import auth, db, worker


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as fh:
        return fh.read().strip()


def _seed_teacher():
    """No self-registration exists. The first teacher comes from env vars or is generated once."""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]:
            return
        user = os.environ.get("TEACHER_USERNAME", "teacher")
        password = os.environ.get("TEACHER_PASSWORD")
        generated = password is None
        password = password or secrets.token_urlsafe(9)
        conn.execute("INSERT INTO teachers (username, password_hash, created_at) VALUES (?,?,?)",
                     (user, generate_password_hash(password), db.now()))
        conn.commit()
        if generated:
            print("\n" + "=" * 60 + f"\n  First-run teacher account created\n  username: {user}\n  password: {password}"
                  "\n  (shown once - change it under Account, or run: python manage.py reset-password)\n" + "=" * 60 + "\n")
    finally:
        conn.close()


def create_app():
    app = Flask(__name__)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.config.update(
        SECRET_KEY=_secret_key(),
        MAX_CONTENT_LENGTH=800 * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",  # set when served over HTTPS
    )
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    _seed_teacher()

    app.teardown_appcontext(db.close_db)
    app.jinja_env.globals["csrf_token"] = auth.csrf_token

    @app.before_request
    def _before():
        db.get_db()
        auth.load_teacher()
        auth.check_csrf()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if resp.mimetype == "text/html":
            resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        if g.get("teacher"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def _error(err):
        messages = {400: getattr(err, "description", "Bad request."), 404: "That page doesn't exist, or it isn't yours.",
                    413: "That upload is too large. Split it into smaller batches."}
        return render_template("error.html", code=err.code, message=messages.get(err.code, "Something went wrong.")), err.code

    from webapp.views import bp
    app.register_blueprint(bp)
    worker.resume_pending()
    return app
