"""Seed one synthetic member in a disposable, explicitly enabled loopback test database."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from src.passwords import hash_password
from local_test_support import require_local_tests, require_test_password


def main():
    url = os.environ.get("DATABASE_URL", "")
    require_local_tests(database_url=url)
    email = os.environ.get("SMOKE_EMAIL", "member@example.test").strip().lower()
    if not email.endswith("@example.test"):
        raise SystemExit("Synthetic test accounts must use the reserved example.test domain")
    password_hash = hash_password(require_test_password())
    import psycopg2
    with psycopg2.connect(url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM ext_users WHERE id='smoke-tester'")
            current = cur.fetchone()
            if current and current[0] != email:
                raise SystemExit("Refusing to replace an unrelated account")
            cur.execute(
                "INSERT INTO ext_users (id, name, email, password_hash, role) "
                "VALUES ('smoke-tester', 'Test Member', %s, %s, 'member') "
                "ON CONFLICT (id) DO UPDATE SET password_hash=EXCLUDED.password_hash, role='member', is_active=TRUE",
                (email, password_hash),
            )
    print("Synthetic local test member prepared; password not printed.")


if __name__ == "__main__":
    main()
