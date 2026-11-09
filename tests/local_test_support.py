"""Safety boundary for scripts which mutate disposable local test data."""
import os
import urllib.request
from urllib.parse import urlparse


def require_local_tests(base: str | None = None, database_url: str | None = None):
    if os.environ.get("LOCAL_TESTS_ENABLED") != "1":
        raise SystemExit("LOCAL_TESTS_ENABLED=1 is required for disposable local integration tests")
    for value, schemes in ((base, {"http"}), (database_url, {"postgres", "postgresql"})):
        if value is None:
            continue
        parsed = urlparse(value)
        if parsed.scheme not in schemes or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise SystemExit("Integration tests allow only explicit literal loopback HTTP and PostgreSQL targets")
        if parsed.username and schemes == {"http"}:
            raise SystemExit("HTTP target must not contain credentials")


def require_test_password(name="SMOKE_PW"):
    password = os.environ.get(name, "")
    if not password:
        raise SystemExit(f"{name} must be a generated temporary test password")
    return password


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Local integration tests never follow HTTP redirects")


def local_urlopen(request, timeout=30):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
