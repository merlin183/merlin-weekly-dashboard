# -*- coding: utf-8 -*-
"""회귀 테스트 — 2026-06 세션에서 고친 버그들이 재발하지 않는지 검증.

실행: python tests/regression_test.py
  (Railway 환경변수 DATABASE_URL 필요. 권한 테스트는 합성 smoke-tester 계정만 사용·임시 승격·원복한다.)
출력: 각 검사 PASS/FAIL + 요약. ASCII만 출력(콘솔 인코딩 안전).
"""
import os, json, time, uuid, urllib.request as u, urllib.error
from local_test_support import require_local_tests, require_test_password, local_urlopen
require_local_tests(base=os.environ.get("DASH_BASE", "http://localhost:8000"), database_url=os.environ.get("DATABASE_URL"))
require_test_password("E2E_PW" if __file__.endswith("e2e_test.py") else "SMOKE_PW")
import psycopg2

BASE = os.environ.get("DASH_BASE", "http://localhost:8000")
PASS, FAIL = [], []


def api(path, token=None, method=None, body=None):
    # Local test requests have a recognizable user agent.
    h = {"Content-Type": "application/json", "User-Agent": "weekly-report-smoke/1.0"}
    if token:
        h["Authorization"] = "Bearer " + token
    b = json.dumps(body).encode() if body is not None else None
    r = u.Request(BASE + path, data=b, headers=h, method=method or ("POST" if body else "GET"))
    try:
        resp = local_urlopen(r, timeout=30)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return "ERR", {"e": str(e)}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + detail) if detail else ""))


def login(email):
    st, d = api("/api/auth/login-user", body={"email": email, "password": os.environ["SMOKE_PW"]})
    return d.get("token")


print("=== Regression test ===")
member_tok = login("member@example.test")
check("login member(test)", bool(member_tok))

# 1) /api/health -> 200 ok (B2)
st, d = api("/api/health")
check("health endpoint 200 ok", st == 200 and d.get("ok") is True, "status=%s" % st)

# 2) exec-status save (대표현황 저장 500 회귀)
WK = "_regtest_"
st, d = api("/api/exec-status", token=member_tok, method="PUT",
            body={"name": "_reg_", "week_label": WK, "current_tasks": "x", "meeting_notes": "y"})
check("exec-status PUT 200 (no 500)", st == 200, "status=%s" % st)
# cleanup
try:
    c = db(); cur = c.cursor(); cur.execute("DELETE FROM ext_exec_status WHERE week_label=%s", (WK,)); c.commit(); c.close()
except Exception:
    pass

# 3) linear/my-issues -> 200, dict, issues have 'id' (내 이슈 dup-key 회귀 + Linear 강건화)
st, d = api("/api/linear/my-issues", token=member_tok)
ok_status = (st == 200 and isinstance(d, dict) and "byAssignee" in d) or (st in (400, 503) and os.environ.get("SMOKE_CI") == "1")
check("my-issues 200 + byAssignee", ok_status, "status=%s" % st)
has_id = True
sample = None
if ok_status:
    for issues in d.get("byAssignee", {}).values():
        for it in issues:
            sample = it
            if "id" not in it:
                has_id = False
            break
        if sample:
            break
check("my-issues items contain 'id' key (dup-key fix)", has_id if sample else True,
      "no issues to check" if not sample else "ok")

# 4) approval delete permission (전자결재 삭제 권한 회귀)
RID = "zz_regtest_" + uuid.uuid4().hex[:6]
try:
    c = db(); cur = c.cursor()
    cur.execute("""INSERT INTO ext_approval_requests
        (id,req_type,title,amount,detail,requester,approver,status,attachments,items,cert_info,book_info,created_at,updated_at,pm_approver,admin_approver,pm_status,admin_status)
        VALUES (%s,'personal_expense','[regtest]',1,'','_other_','Blair','승인','[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'{}'::jsonb,NOW(),NOW(),'Morgan','Blair','승인','승인')""", (RID,))
    cur.execute("UPDATE ext_users SET role='member' WHERE id='smoke-tester'")
    c.commit(); c.close()
    # member deletes someone else's approval -> 403
    mt = login("member@example.test")
    st, _ = api("/api/approvals/" + RID, token=mt, method="DELETE")
    check("member cannot delete others' approval (403)", st == 403, "status=%s" % st)
    # elevate to pm -> can delete -> 200
    c = db(); cur = c.cursor(); cur.execute("UPDATE ext_users SET role='pm' WHERE id='smoke-tester'"); c.commit(); c.close()
    pt = login("member@example.test")
    st, _ = api("/api/approvals/" + RID, token=pt, method="DELETE")
    check("pm can delete any approval (200)", st == 200, "status=%s" % st)
finally:
    try:
        c = db(); cur = c.cursor()
        cur.execute("DELETE FROM ext_approval_requests WHERE id=%s", (RID,))
        cur.execute("UPDATE ext_users SET role='member' WHERE id='smoke-tester'")
        c.commit(); c.close()
    except Exception:
        pass

# 5) expense watermark store format (비용 대량 오발송 회귀) — last_no는 정수여야 함
try:
    c = db(); cur = c.cursor()
    cur.execute("SELECT data->>'seeded', jsonb_typeof(data->'last_no') FROM ext_app_store WHERE key='expense_notified'")
    r = cur.fetchone(); c.close()
    if r:
        check("expense watermark last_no is number", r[1] == "number", "type=%s" % r[1])
    else:
        check("expense watermark optional for unconfigured sheet", os.environ.get("SMOKE_CI") == "1", "not configured")
except Exception as e:
    check("expense watermark check", False, str(e)[:60])

print("\n=== SUMMARY: %d passed, %d failed ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
