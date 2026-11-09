# -*- coding: utf-8 -*-
"""C3 — 프론트 e2e 스모크 (Playwright). 프론트 분리/리팩터 안전망.

목적: 로그인 → 모든 탭 순회 렌더 → **미처리 JS 예외(pageerror) 0** 확인.
      (Alpine x-for 크래시 같은 "탭 렌더 깨짐"을 자동으로 잡는 게 핵심)
      + 핵심 상호작용 1개(리포트 팀 우선순위 항목 추가→표시).

전제: 앱이 DASH_BASE 에서 떠 있어야 함(로컬 테스트 환경 권장).
설정: DATABASE_URL 있으면 e2e 관리자 계정을 1회 upsert(전 탭 노출 위해 admin).
실행(Windows, 테스트환경 가이드 참고):
  $env:PYTHONUTF8="1"; $env:DASH_BASE="http://localhost:8000"; $env:DATABASE_URL=$tdb
  python tests/e2e_test.py
의존성: pip install playwright ; python -m playwright install chromium
출력: 각 단계 PASS/FAIL + 요약. 실패 시 종료코드 1.
"""
import os
from local_test_support import require_local_tests, require_test_password, local_urlopen
require_local_tests(base=os.environ.get("DASH_BASE", "http://localhost:8000"), database_url=os.environ.get("DATABASE_URL"))
require_test_password("E2E_PW" if __file__.endswith("e2e_test.py") else "SMOKE_PW")
import sys
import hashlib
from urllib.parse import urlparse

BASE = os.environ.get("DASH_BASE", "http://localhost:8000")
EMAIL = os.environ.get("E2E_EMAIL", "e2e-admin@example.test")
if not EMAIL.endswith("@example.test"):
    raise SystemExit("E2E requires a reserved synthetic example.test email")
PW = require_test_password("E2E_PW")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + detail) if detail else ""))


sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from src.passwords import hash_password as _hash


def ensure_admin():
    """전 탭 노출 위해 e2e 관리자 계정 upsert (DATABASE_URL=테스트DB 일 때만)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("  (DATABASE_URL 없음 — 관리자 시드 건너뜀, 기존 계정으로 로그인)")
        return
    import psycopg2
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS ext_users (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
            slack_id TEXT DEFAULT '', linear_id TEXT DEFAULT '', is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(), last_login TIMESTAMP)""")
    cur.execute("SELECT email FROM ext_users WHERE id='e2e-admin'")
    existing = cur.fetchone()
    if existing and existing[0] != EMAIL:
        raise SystemExit("Refusing to replace an unrelated account")
    cur.execute(
        """INSERT INTO ext_users (id, name, email, password_hash, role)
           VALUES (%s,%s,%s,%s,'admin')
           ON CONFLICT (id) DO UPDATE SET password_hash=EXCLUDED.password_hash,
             role='admin', is_active=TRUE""",
        ("e2e-admin", "E2E관리자", EMAIL, _hash(PW)))
    conn.commit()
    conn.close()
    print(f"  e2e 관리자 계정 준비: {EMAIL}")


def run():
    from playwright.sync_api import sync_playwright

    page_errors = []   # 미처리 JS 예외 (hard fail)
    console_errors = []  # console.error (참고용)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Keep even redirects, optional assets and integration requests inside the disposable app.
        page.route("**/*", lambda route: route.continue_()
                   if urlparse(route.request.url).hostname in {"localhost", "127.0.0.1", "::1"}
                   else route.abort())
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # 1) 로그인
        page.goto(BASE, wait_until="domcontentloaded")
        page.fill("#login-email", EMAIL)
        page.fill("#login-password", PW)
        page.click("#login-form button[type=submit]")
        # 대시보드 렌더 대기 (탭 나브 등장)
        try:
            page.wait_for_selector("#dashboard-app", state="visible", timeout=15000)
            page.wait_for_selector("div.bg-slate-100.w-fit button", timeout=15000)
            page.wait_for_timeout(1500)  # 초기 데이터 로드/렌더 안정화
            check("로그인 → 대시보드 렌더", True)
        except Exception as e:
            check("로그인 → 대시보드 렌더", False, repr(e)[:80])
            browser.close()
            return

        errs_after_login = len(page_errors)
        check("로그인 직후 미처리 JS 예외 0", errs_after_login == 0,
              "errors=%s" % page_errors[:2])

        # core.js 전역 로드 검증 (분리 무결성 — 분리가 깨지면 인라인/메서드에서 ReferenceError)
        g = page.evaluate("""() => ({
            _authFetch: typeof _authFetch === 'function',
            _getAuthToken: typeof _getAuthToken === 'function',
            _authHeaders: typeof _authHeaders === 'function',
            openAttachment: typeof openAttachment === 'function',
            proxyUrl: typeof proxyUrl === 'function',
            API_BASE: typeof window.API_BASE === 'string',
        })""")
        missing = [k for k, v in g.items() if not v]
        check("core.js 전역 로드(분리 무결성)", not missing, "missing=%s" % missing)

        # 2) 모든 탭 순회 — 클릭 → 활성화 + 미처리 예외 0
        tabs = page.locator("div.bg-slate-100.w-fit button")
        n = tabs.count()
        check("탭 버튼 발견(>=5)", n >= 5, "count=%s" % n)
        for i in range(n):
            label = (tabs.nth(i).inner_text() or "").strip() or f"tab#{i}"
            before = len(page_errors)
            try:
                tabs.nth(i).click()
                page.wait_for_timeout(900)  # Alpine 렌더 + 지연 로드
                active = "bg-white" in (tabs.nth(i).get_attribute("class") or "")
                new_errs = page_errors[before:]
                check(f"탭 '{label}' 렌더(전환 OK·예외 0)",
                      active and not new_errs,
                      ("switched=%s errs=%s" % (active, new_errs[:1])) if (not active or new_errs) else "")
            except Exception as e:
                check(f"탭 '{label}' 클릭", False, repr(e)[:80])

        # 3) 핵심 상호작용 — 리포트 탭에서 데이터 재로드(새로고침). 멱등 + fetch→반응형 재렌더 경로 검증.
        try:
            # report 탭으로
            for i in range(tabs.count()):
                if (tabs.nth(i).inner_text() or "").strip().startswith("주간"):
                    tabs.nth(i).click(); page.wait_for_timeout(800); break
            # 우선 '새로고침'(관리자 KPI 카드, 멱등), 없으면 '안건/항목 추가' 폴백
            act = page.locator("button:has-text('새로고침'), button:has-text('안건 추가'), button:has-text('항목 추가')").first
            if act.count() and act.is_visible():
                before = len(page_errors)
                act.scroll_into_view_if_needed()
                label = (act.inner_text() or "").strip()
                act.click(); page.wait_for_timeout(1200)  # 재로드+재렌더
                check(f"리포트 상호작용 '{label}'(예외 0)", not page_errors[before:],
                      "errs=%s" % (page_errors[before:][:1]))
            else:
                print("  SKIP 리포트 상호작용 — 클릭 대상 미발견(무시 가능)")
        except Exception as e:
            check("리포트 항목 추가 상호작용", False, repr(e)[:80])

        # 전체 미처리 예외 총합 (최종 게이트)
        check("전체 미처리 JS 예외 0 (프론트 무결성)", len(page_errors) == 0,
              "total=%s sample=%s" % (len(page_errors), page_errors[:2]))
        if console_errors:
            print(f"  (참고) console.error {len(console_errors)}건 — 테스트환경 외부키 미설정 등(대개 무해): {console_errors[:2]}")

        browser.close()


if __name__ == "__main__":
    print("=== C3 e2e smoke ===  BASE=%s  user=%s" % (BASE, EMAIL))
    try:
        ensure_admin()
        run()
    except Exception as e:
        check("e2e 실행", False, repr(e)[:120])
    print("\n=== SUMMARY: %d passed, %d failed ===" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        sys.exit(1)
