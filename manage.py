"""Teacher account management (there is no public sign-up).

    python manage.py create-teacher <username>
    python manage.py reset-password <username>
    python manage.py list-teachers
"""
import getpass
import sys

from werkzeug.security import generate_password_hash

from webapp import db


def _ask_password():
    pw = getpass.getpass("Password (min 8 characters): ")
    if len(pw) < 8 or pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords must match and be at least 8 characters.")
    return pw


def main(argv):
    db.init_db()
    conn = db.connect()
    if len(argv) >= 2 and argv[0] == "create-teacher":
        conn.execute("INSERT INTO teachers (username, password_hash, created_at) VALUES (?,?,?)",
                     (argv[1], generate_password_hash(_ask_password()), db.now()))
        conn.commit()
        print(f"Teacher '{argv[1]}' created.")
    elif len(argv) >= 2 and argv[0] == "reset-password":
        cur = conn.execute("UPDATE teachers SET password_hash = ? WHERE username = ?",
                           (generate_password_hash(_ask_password()), argv[1]))
        conn.commit()
        print("Password updated." if cur.rowcount else f"No teacher named '{argv[1]}'.")
    elif argv[:1] == ["list-teachers"]:
        for r in conn.execute("SELECT username, created_at FROM teachers"):
            print(r["username"], r["created_at"])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
