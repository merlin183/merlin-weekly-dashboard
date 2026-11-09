"""Explicitly create the first administrator in an otherwise empty installation.

Run: python scripts/bootstrap_admin.py --name NAME --email EMAIL
Passwords are entered without echo. For isolated CI, --password-stdin reads one bounded line.
Existing users are never replaced or promoted by this command.
"""
import argparse
import getpass
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
from src.passwords import MAX_PASSWORD_BYTES, hash_password, validate_password


def bootstrap_admin(conn, name: str, email: str, password: str) -> str:
    name, email = name.strip(), email.strip().lower()
    if not name or not email or "@" not in email or len(name) > 200 or len(email) > 254:
        raise ValueError("An explicit valid name and email are required")
    password_hash = hash_password(password)
    try:
        with conn.cursor() as cur:
            # Exclusive lock closes the race between two simultaneous bootstrap commands.
            cur.execute("LOCK TABLE ext_users IN EXCLUSIVE MODE")
            cur.execute("SELECT 1 FROM ext_users LIMIT 1")
            if cur.fetchone():
                raise ValueError("Bootstrap refused: this installation already has users")
            identifier = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO ext_users (id, name, email, password_hash, role) VALUES (%s, %s, %s, %s, 'admin')",
                (identifier, name, email, password_hash),
            )
        conn.commit()
        return identifier
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-stdin", action="store_true", help="Read one password line from a secure pipe (CI only)")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        parser.error("DATABASE_URL is required")
    if args.password_stdin:
        raw = sys.stdin.buffer.readline(MAX_PASSWORD_BYTES + 2)
        if len(raw.rstrip(b"\r\n")) > MAX_PASSWORD_BYTES:
            parser.error("Password exceeds the 1024-byte limit")
        password = raw.rstrip(b"\r\n").decode("utf-8")
    else:
        if not sys.stdin.isatty():
            parser.error("Use an interactive terminal or the explicit --password-stdin option")
        password = getpass.getpass("New administrator password (at least 12 characters): ")
        try:
            validate_password(password)
        except ValueError as exc:
            parser.error(str(exc))
        if getpass.getpass("Repeat password: ") != password:
            parser.error("Passwords do not match")
    import psycopg2
    try:
        validate_password(password)
        # This command is the installer's explicit authorization to create local schema.
        from src.main import initialize_schema
        initialize_schema()
        with psycopg2.connect(url, connect_timeout=10) as conn:
            bootstrap_admin(conn, args.name, args.email, password)
    except ValueError as exc:
        parser.error(str(exc))
    print("Initial administrator created. No password was printed.")


if __name__ == "__main__":
    main()
