"""Offline release regressions for explicit installation, credentials and private uploads."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import main as M
from src.installation_policy import require_jwt_secret
from src.passwords import hash_password, verify_password
from starlette.requests import Request
from starlette.responses import Response

spec = importlib.util.spec_from_file_location("bootstrap_admin", ROOT / "scripts/bootstrap_admin.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def request(path="/api/auth/login", body=None, token="", cookie=""):
    raw = json.dumps(body or {}).encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if cookie:
        headers.append((b"cookie", f"weekly_file_session={cookie}".encode()))
    return Request({"type": "http", "method": "GET" if "file-proxy" in path else "POST",
                    "path": path, "headers": headers, "scheme": "http",
                    "client": ("127.0.0.1", 50000), "server": ("localhost", 8000),
                    "query_string": b""}, receive)


class Cursor:
    def __init__(self, existing=False):
        self.existing, self.queries = existing, []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, values=None):
        self.queries.append((sql, values))
    def fetchone(self):
        return (1,) if self.existing else None


class Connection:
    def __init__(self, existing=False):
        self.cur, self.committed, self.rolled_back = Cursor(existing), False, False
    def cursor(self):
        return self.cur
    def commit(self):
        self.committed = True
    def rollback(self):
        self.rolled_back = True
    def close(self):
        pass


class InstallationSecurityTests(unittest.TestCase):
    def test_factory_installation_has_no_people_or_approval_policy(self):
        policy_spec = importlib.util.spec_from_file_location("empty_installation", ROOT / "src/installation_policy.py")
        policy = importlib.util.module_from_spec(policy_spec)
        with patch.dict(os.environ, {}, clear=True):
            policy_spec.loader.exec_module(policy)
        self.assertFalse(policy.BACKGROUND_TASKS_ENABLED)
        self.assertFalse(policy.STORAGE_AUTO_CREATE_BUCKET)
        self.assertFalse(policy.LEGACY_SHARED_LOGIN_ENABLED)
        self.assertEqual(policy.team_config_defaults(), {
            "members": [], "profileVisibility": {"hiddenNames": []},
            "approvalDefaults": {"primary": "", "reference": ""},
        })
        self.assertEqual(policy.EXPENSE_NOTIFY_NAMES, [])
        self.assertEqual(policy.LINEAR_EXCLUDED_TEAM_NAMES, [])
        self.assertEqual(policy.METRICS_SKIPPED_DATES, frozenset())

    def test_metrics_skipped_dates_are_explicit_and_validated(self):
        from src.installation_policy import env_dates
        with patch.dict(os.environ, {"METRICS_SKIPPED_DATES": "2040-02-29, 2040-03-01,2040-02-29"}):
            self.assertEqual(env_dates("METRICS_SKIPPED_DATES"), frozenset({"2040-02-29", "2040-03-01"}))
        for value in ("2041-02-29", "2040-2-1", "yesterday", "2040-03-01; DELETE FROM records"):
            with self.subTest(value=value), patch.dict(os.environ, {"METRICS_SKIPPED_DATES": value}):
                with self.assertRaises(ValueError):
                    env_dates("METRICS_SKIPPED_DATES")

    def test_user_schema_initialization_never_inserts_accounts(self):
        conn = Connection()
        with patch.object(M, "_pg_conn", return_value=conn):
            M._init_users_table()
        self.assertTrue(conn.committed)
        self.assertFalse(any("INSERT" in sql.upper() for sql, _ in conn.cur.queries))

    def test_first_schema_initialization_creates_tables_before_upsert_indexes(self):
        created_tables, indexed_tables = set(), set()
        conn = Connection()
        original_execute = conn.cur.execute
        def execute(sql, values=None):
            original_execute(sql, values)
            table = re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.I)
            if table:
                created_tables.add(table.group(1))
            index = re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS\s+\w+\s+ON\s+(\w+)", sql, re.I)
            if index:
                self.assertIn(index.group(1), created_tables, "Index creation must follow its table on first boot")
                indexed_tables.add(index.group(1))
        conn.cur.execute = execute
        with patch.object(M, "DATABASE_URL", "postgresql://localhost/disposable"), patch.object(M.psycopg2, "connect", return_value=Connection()), patch.object(M, "_pg_conn", return_value=conn), patch.object(M._installation, "env_bool", return_value=False):
            M.initialize_schema()
        self.assertEqual(indexed_tables, {"ext_exec_status", "ext_leave_balance", "ext_daily_metrics"})

    def test_schema_lock_spans_all_helpers_and_optional_seed_then_commits(self):
        events, locked = [], {"active": False}
        guard, pooled = Connection(), Connection()
        guard.close = unittest.mock.Mock(side_effect=lambda: events.append("close"))
        def acquire(sql, values=None):
            self.assertEqual(sql, "SELECT pg_advisory_xact_lock(%s)")
            self.assertEqual(values, (M._SCHEMA_LOCK_KEY,))
            locked["active"] = True
            events.append("lock")
        def pooled_execute(sql, values=None):
            self.assertTrue(locked["active"], "Every DDL statement must be inside the schema lock lifetime")
            events.append("ddl")
        def pooled_commit():
            self.assertTrue(locked["active"], "Helper commits must not release the dedicated lock transaction")
            events.append("helper-commit")
        def optional_seed():
            self.assertTrue(locked["active"])
            events.append("optional-seed")
        def finish():
            self.assertTrue(locked["active"])
            self.assertEqual(events[-1], "optional-seed")
            locked["active"] = False
            events.append("commit")
        guard.cur.execute = acquire
        guard.commit = unittest.mock.Mock(side_effect=finish)
        guard.rollback = unittest.mock.Mock()
        pooled.cur.execute, pooled.commit = pooled_execute, pooled_commit
        with patch.object(M, "DATABASE_URL", "postgresql://localhost/disposable"), patch.object(M.psycopg2, "connect", return_value=guard) as connect, patch.object(M, "_pg_conn", return_value=pooled), patch.object(M._installation, "env_bool", return_value=True), patch.object(M, "_seed_korean_holidays", side_effect=optional_seed):
            M.initialize_schema()
        connect.assert_called_once_with("postgresql://localhost/disposable", **M._PG_CONN_KWARGS)
        self.assertFalse(guard.autocommit)
        self.assertNotIn(M._SCHEMA_LOCK_KEY, (M._LEADER_LOCK_KEY, M._REPORT_LOCK_KEY))
        self.assertGreater(events.count("ddl"), 20)
        self.assertEqual(events[0], "lock")
        self.assertEqual(events[-3:], ["optional-seed", "commit", "close"])
        self.assertFalse(locked["active"])
        guard.commit.assert_called_once()
        guard.rollback.assert_not_called()
        guard.close.assert_called_once()

    def test_schema_lock_rolls_back_and_closes_when_a_helper_fails(self):
        events, locked = [], {"active": False}
        guard = Connection()
        def acquire(sql, values=None):
            locked["active"] = True
            events.append("lock")
        def failure():
            self.assertTrue(locked["active"])
            events.append("helper-failure")
            raise RuntimeError("synthetic schema failure")
        def rollback():
            self.assertTrue(locked["active"])
            locked["active"] = False
            events.append("rollback")
        guard.cur.execute = acquire
        guard.commit = unittest.mock.Mock()
        guard.rollback = unittest.mock.Mock(side_effect=rollback)
        guard.close = unittest.mock.Mock(side_effect=lambda: events.append("close"))
        with patch.object(M, "DATABASE_URL", "postgresql://localhost/disposable"), patch.object(M.psycopg2, "connect", return_value=guard), patch.object(M, "_pg_conn", return_value=Connection()), patch.object(M, "_ensure_core_tree_table", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "synthetic schema failure"):
                M.initialize_schema()
        self.assertEqual(events, ["lock", "helper-failure", "rollback", "close"])
        self.assertFalse(locked["active"])
        guard.commit.assert_not_called()
        guard.rollback.assert_called_once()
        guard.close.assert_called_once()

    def test_table_column_upgrades_are_idempotent_without_discarding_fresh_tables(self):
        for initialize in (M._ensure_core_tree_table, M._ensure_member_reports_table):
            with self.subTest(helper=initialize.__name__):
                conn = Connection()
                conn.close = unittest.mock.Mock()
                original_execute = conn.cur.execute
                def execute(sql, values=None):
                    # Both CREATE definitions already contain their upgrade columns.
                    if sql.lstrip().upper().startswith("ALTER TABLE") and "ADD COLUMN IF NOT EXISTS" not in sql:
                        raise RuntimeError("duplicate column would roll back a freshly created table")
                    original_execute(sql, values)
                conn.cur.execute = execute
                with patch.object(M, "_pg_conn", return_value=conn) as connect:
                    initialize()
                    initialize()
                self.assertTrue(conn.committed)
                self.assertFalse(conn.rolled_back)
                self.assertEqual(connect.call_count, 2)
                self.assertEqual(conn.close.call_count, 2)

    def test_table_column_upgrade_failure_rolls_back_and_releases_connection(self):
        for initialize in (M._ensure_core_tree_table, M._ensure_member_reports_table):
            with self.subTest(helper=initialize.__name__):
                conn = Connection()
                conn.close = unittest.mock.Mock()
                conn.cur.execute = unittest.mock.Mock(side_effect=RuntimeError("synthetic DDL failure"))
                with patch.object(M, "_pg_conn", return_value=conn) as connect:
                    with self.assertRaisesRegex(RuntimeError, "synthetic DDL failure"):
                        initialize()
                self.assertTrue(conn.rolled_back)
                self.assertFalse(conn.committed)
                connect.assert_called_once()
                conn.close.assert_called_once()

    def test_password_salts_are_independent_and_versioned(self):
        password = secrets.token_urlsafe(24)
        first, second = hash_password(password), hash_password(password)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("pbkdf2_sha256$600000$"))
        self.assertTrue(verify_password(password, first))
        self.assertFalse(verify_password(password + "x", first))

    def test_password_verifier_rejects_unversioned_and_unbounded_inputs(self):
        for stored in ("", "0" * 64, "pbkdf2_sha256$999999999$bad$bad", None):
            self.assertFalse(verify_password("synthetic-password", stored))
        self.assertFalse(verify_password("x" * 1025, "irrelevant"))
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_jwt_rejects_empty_and_placeholder_keys(self):
        for value in ("", " ", "x" * 64, "change-me-" * 8):
            with self.assertRaises(ValueError):
                require_jwt_secret(value)
            with patch.object(M, "JWT_SECRET", value):
                self.assertIsNone(M._jwt_decode("invalid"))
                with self.assertRaises(ValueError):
                    M._jwt_encode({"user_id": "synthetic"})

    def test_jwt_round_trip_and_tamper(self):
        with patch.object(M, "JWT_SECRET", secrets.token_urlsafe(48)):
            payload = {"user_id": "synthetic-id", "name": "Alex", "role": "member"}
            token = M._jwt_encode(payload)
            self.assertNotIn("exp", payload)
            self.assertEqual(M._jwt_decode(token)["user_id"], "synthetic-id")
            self.assertIsNone(M._jwt_decode(token + "invalid"))

    def test_blank_legacy_login_never_succeeds(self):
        for enabled in (False, True):
            with patch.object(M, "LEGACY_SHARED_LOGIN_ENABLED", enabled), patch.object(M, "DASHBOARD_PASSWORD", ""), patch.object(M, "_add_session") as add:
                response = asyncio.run(M.auth_login(request(body={"password": ""})))
                self.assertEqual(response.status_code, 401)
                add.assert_not_called()
        with patch.object(M, "LEGACY_SHARED_LOGIN_ENABLED", False), patch.object(M, "_load_sessions") as load:
            self.assertFalse(M._check_session("old-token"))
            load.assert_not_called()

    def test_bootstrap_creates_only_explicit_first_admin(self):
        conn = Connection()
        password = secrets.token_urlsafe(24)
        identifier = bootstrap.bootstrap_admin(conn, "Alex Example", "admin@example.test", password)
        self.assertTrue(conn.committed)
        insert = conn.cur.queries[-1]
        self.assertIn("'admin'", insert[0])
        self.assertEqual(insert[1][:3], (identifier, "Alex Example", "admin@example.test"))
        self.assertTrue(verify_password(password, insert[1][3]))

    def test_bootstrap_refuses_existing_workspace(self):
        conn = Connection(existing=True)
        with self.assertRaisesRegex(ValueError, "already has users"):
            bootstrap.bootstrap_admin(conn, "Alex", "admin@example.test", secrets.token_urlsafe(24))
        self.assertTrue(conn.rolled_back)
        self.assertFalse(any("INSERT" in sql for sql, _ in conn.cur.queries))

    def test_bootstrap_requires_explicit_identity_and_strong_password(self):
        for name, email, password in (("", "admin@example.test", "a" * 20), ("Alex", "", "a" * 20), ("Alex", "admin@example.test", "short")):
            conn = Connection()
            with self.assertRaises(ValueError):
                bootstrap.bootstrap_admin(conn, name, email, password)
            self.assertEqual(conn.cur.queries, [])

    def test_file_proxy_requires_authentication(self):
        async def next_handler(_request):
            return Response("private")
        response = asyncio.run(M.auth_middleware(request("/api/file-proxy/example.png"), next_handler))
        self.assertEqual(response.status_code, 401)

    def test_file_cookie_is_only_accepted_for_file_requests(self):
        async def next_handler(_request):
            return Response("private")
        with patch.object(M, "JWT_SECRET", secrets.token_urlsafe(48)), patch.object(M, "_is_user_active_cached", return_value=True):
            token = M._jwt_encode({"user_id": "synthetic", "name": "Alex", "role": "member"})
            allowed = asyncio.run(M.auth_middleware(request("/api/file-proxy/example.png", cookie=token), next_handler))
            denied = asyncio.run(M.auth_middleware(request("/api/todos", cookie=token), next_handler))
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(denied.status_code, 401)

    def test_active_content_uses_private_authenticated_storage_and_sandbox(self):
        seen = []
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, **kwargs):
                seen.append((url, kwargs))
                return type("Download", (), {"status_code": 200, "headers": {"content-type": "text/html"}, "content": b"<script>alert(1)</script>"})()
        with patch.object(M, "SUPABASE_URL", "https://storage.example.test"), patch.object(M, "SUPABASE_SERVICE_ROLE_KEY", "synthetic-only"), patch.object(M.httpx, "AsyncClient", Client):
            for name in ("example.html", "example.svg"):
                response = asyncio.run(M.file_proxy(name))
                self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
                self.assertIn("sandbox", response.headers["content-security-policy"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(all("/object/authenticated/" in url for url, _ in seen))
        self.assertTrue(all("Authorization" in args["headers"] for _, args in seen))
        self.assertTrue(all(args["follow_redirects"] is False for _, args in seen))

    def test_url_proxy_rejects_ssrf_and_ambiguous_storage_urls_without_requests(self):
        origin = "https://storage.example.test"
        path = "/storage/v1/object/public/uploads/example.pdf"
        invalid = (
            "http://127.0.0.1/private?reference=supabase.co/storage/",
            "http://169.254.169.254/supabase.co/storage/",
            "https://storage.example.test.attacker.example.test" + path,
            "https://storage.example.test@127.0.0.1" + path,
            "https://person:password@storage.example.test" + path,
            "https://storage.example.test:8443" + path,
            "https://storage.example.test:invalid" + path,
            "http://storage.example.test" + path,
            origin + path + "?url=http://127.0.0.1/private",
            origin + path + "#fragment",
            origin + path.replace("/uploads/", "/another-bucket/"),
            origin + path.replace("example.pdf", "../private.pdf"),
            origin + path.replace("example.pdf", "%2e%2e%2fprivate.pdf"),
            origin + path.replace("example.pdf", "%252e%252e%252fprivate.pdf"),
            origin + path.replace("example.pdf", "bad%0d%0aheader.pdf"),
        )
        with patch.object(M, "SUPABASE_URL", origin), patch.object(M, "SUPABASE_SERVICE_ROLE_KEY", "synthetic-only"), patch.object(M, "STORAGE_BUCKET", "uploads"), patch.object(M.httpx, "AsyncClient") as client:
            for url in invalid:
                with self.subTest(url=url):
                    response = asyncio.run(M.proxy_file(url))
                    self.assertEqual(response.status_code, 400)
            client.assert_not_called()

    def test_url_proxy_only_resolves_configured_bucket_to_private_objects(self):
        seen = []
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, **kwargs):
                seen.append((url, kwargs))
                return type("Download", (), {"status_code": 200, "headers": {"content-type": "text/html"}, "content": b"<script>untrusted()</script>"})()
        with patch.object(M, "SUPABASE_URL", "https://storage.example.test"), patch.object(M, "SUPABASE_SERVICE_ROLE_KEY", "synthetic-only"), patch.object(M, "STORAGE_BUCKET", "private-uploads"), patch.object(M.httpx, "AsyncClient", Client):
            for path in ("public/", "authenticated/", ""):
                response = asyncio.run(M.proxy_file(f"https://storage.example.test/storage/v1/object/{path}private-uploads/example.html"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "application/octet-stream")
                self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
                self.assertIn("sandbox", response.headers["content-security-policy"])
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(url == "https://storage.example.test/storage/v1/object/authenticated/private-uploads/example.html" for url, _ in seen))

    def test_storage_redirect_is_rejected_without_following_it(self):
        seen = []
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, **kwargs):
                seen.append((url, kwargs))
                return type("Redirect", (), {"status_code": 302, "headers": {"location": "http://127.0.0.1/private"}})()
        with patch.object(M, "SUPABASE_URL", "https://storage.example.test"), patch.object(M, "SUPABASE_SERVICE_ROLE_KEY", "synthetic-only"), patch.object(M, "STORAGE_BUCKET", "uploads"), patch.object(M.httpx, "AsyncClient", Client):
            response = asyncio.run(M.proxy_file("https://storage.example.test/storage/v1/object/public/uploads/example.pdf"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0][1]["follow_redirects"])
        self.assertNotIn("location", response.headers)
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_startup_rejects_missing_jwt_before_database_access(self):
        with patch.object(M, "JWT_SECRET", ""), patch.object(M, "initialize_schema") as schema:
            with self.assertRaises(ValueError):
                asyncio.run(M.startup_event())
            schema.assert_not_called()

    def test_default_startup_does_not_provision_or_schedule(self):
        with patch.object(M, "JWT_SECRET", secrets.token_urlsafe(48)), patch.object(M, "initialize_schema"), patch.object(M, "STORAGE_AUTO_CREATE_BUCKET", False), patch.object(M, "BACKGROUND_TASKS_ENABLED", False), patch.object(M.httpx, "AsyncClient") as client, patch.object(M.asyncio, "create_task") as create:
            asyncio.run(M.startup_event())
            client.assert_not_called()
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
