# -*- coding: utf-8 -*-
"""C1 — API 스모크 테스트 (핵심 경로 행복경로 + 권한 가드 + report 병합 무손실).

설계 원칙(중요):
  - 명시적으로 활성화한 폐기 가능한 로컬 DB와 서버에서만 실행한다.
  - 비용 추가/결재 decide 등 슬랙 DM을 유발하는 경로는 호출하지 않는다(Riley·Morgan 오발송 방지).
  - 새 의존성 없음(urllib+psycopg2). DB 의존 테스트는 DATABASE_URL 있을 때만 실행.

실행:
  python tests/smoke_test.py           # 전체(권한 가드 포함, DB 필요)
  DASH_BASE=... python tests/smoke_test.py          # API만(테스트 환경 대상 가능)
환경변수:
  DASH_BASE   대상 로컬 서버(기본: http://localhost:8000)
  SMOKE_EMAIL/SMOKE_PW  로그인 계정(기본 member@example.test / generated temporary password)
출력: 각 검사 PASS/FAIL + 요약. 실패 시 종료코드 1 (CI 게이트용).
"""
import os, sys, json, uuid, urllib.request as u, urllib.error
from local_test_support import require_local_tests, require_test_password, local_urlopen
require_local_tests(base=os.environ.get("DASH_BASE", "http://localhost:8000"), database_url=os.environ.get("DATABASE_URL"))
require_test_password("E2E_PW" if __file__.endswith("e2e_test.py") else "SMOKE_PW")

BASE = os.environ.get("DASH_BASE", "http://localhost:8000")
EMAIL = os.environ.get("SMOKE_EMAIL", "member@example.test")
PW = os.environ.get("SMOKE_PW", "")
# CI 최소환경 플래그: 선택 통합이 미설정인 빈 로컬 DB를 허용한다.
CI = os.environ.get("SMOKE_CI") == "1"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + detail) if detail else ""))


def api(path, token=None, method=None, body=None, timeout=30):
    # Local test requests have a recognizable user agent.
    h = {"Content-Type": "application/json", "User-Agent": "weekly-report-smoke/1.0"}
    if token:
        h["Authorization"] = "Bearer " + token
    b = json.dumps(body).encode() if body is not None else None
    r = u.Request(BASE + path, data=b, headers=h, method=method or ("POST" if body else "GET"))
    try:
        resp = local_urlopen(r, timeout=timeout)
        ct = resp.read().decode()
        try:
            return resp.status, json.loads(ct)
        except Exception:
            return resp.status, {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return "ERR", {"e": str(e)[:80]}


def db():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


print("=== C1 Smoke test ===  BASE=%s" % BASE)

# ──────────────────────────────────────────────────────────
# A. 오프라인 단위 테스트 — report 병합 무손실/삭제 미복원 (네트워크 불필요)
# ──────────────────────────────────────────────────────────
print("[A] report 병합 단위 테스트")
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import main as M
    # 무손실: 서버 {1,2} + 들어오는 {2,3} -> {1,2,3} (양쪽 추가분 보존)
    merged, _ = M._merge_report({"teamPriorities": [{"id": 1}, {"id": 2}]},
                                {"teamPriorities": [{"id": 2}, {"id": 3}]})
    ids = sorted(x["id"] for x in merged["teamPriorities"])
    check("merge 무손실 (동시편집 항목 보존)", ids == [1, 2, 3], "ids=%s" % ids)
    # 삭제 미복원: _deletedIds=[2] 면 서버의 2가 되살아나지 않음
    merged2, _ = M._merge_report({"teamPriorities": [{"id": 1}, {"id": 2}]},
                                 {"teamPriorities": [{"id": 1}], "_deletedIds": [2]})
    ids2 = sorted(x["id"] for x in merged2["teamPriorities"])
    check("merge 삭제 미복원", ids2 == [1], "ids=%s" % ids2)
except Exception as e:
    check("report 병합 단위 테스트 import", False, repr(e)[:100])

# ──────────────────────────────────────────────────────────
# B. 인증 / 공개·보호 경로 가드
# ──────────────────────────────────────────────────────────
print("[B] 인증/가드")
st, d = api("/api/health")
check("health 200 ok", st == 200 and d.get("ok") is True, "status=%s" % st)

st, d = api("/api/auth/login-user", body={"email": EMAIL, "password": PW})
token = d.get("token")
check("로그인 행복경로 → token", st == 200 and bool(token), "status=%s" % st)

st, d = api("/api/auth/login-user", body={"email": EMAIL, "password": "wrong-" + uuid.uuid4().hex[:6]})
check("틀린 비번 거부(2xx 아님·토큰없음)", st != 200 and not d.get("token"), "status=%s" % st)

st, _ = api("/api/report")  # 토큰 없이 보호 경로
check("보호 경로 토큰없이 차단(401/403)", st in (401, 403), "status=%s" % st)

st, d = api("/api/auth/me", token=token)
check("auth/me 토큰으로 200", st == 200, "status=%s" % st)

# ──────────────────────────────────────────────────────────
# C. 핵심 읽기 경로 스모크 (모두 200, 안전·비파괴)
# ──────────────────────────────────────────────────────────
print("[C] 핵심 읽기 경로")
st, d = api("/api/report", token=token)
# 200(데이터 있음) 또는 204(빈 DB·리포트 없음 — CI). 프로덕션이면 200+dict.
check("GET /api/report (200/204)", st == 200 or (CI and st == 204), "status=%s" % st)

# 핵심 읽기 — 빈 DB에서도 200 (테이블 자동생성·빈목록 반환)
CORE_READS = [
    "/api/notifications?limit=5",
    "/api/approvals",
    "/api/exec-status",
    "/api/approval-settings",
    "/api/users/approvers",
]
for path in CORE_READS:
    st, _ = api(path, token=token, timeout=45)
    check("GET %s 200" % path.split("?")[0], st == 200, "status=%s" % st)

# 외부 키/추가 테이블 의존 — CI 최소환경에선 soft(상태만 기록, 게이트 통과).
# 프로덕션 실행(SMOKE_CI 미설정)에선 200 강제.
SOFT_READS = [
    "/api/linear/my-issues",  # LINEAR 키 필요
    "/api/leave/balance",  # ext_leave_balance 의존
    "/api/metrics/cache?start_date=2026-06-02&end_date=2026-06-08",  # ext_daily_metrics 의존
]
for path in SOFT_READS:
    st, _ = api(path, token=token, timeout=45)
    name = "GET %s 200" % path.split("?")[0]
    if CI and st != 200:
        print("  SKIP(CI) " + name + "  | status=%s (최소환경 — 프로덕션 실행 시 검증)" % st)
    else:
        check(name, st == 200, "status=%s" % st)

# ──────────────────────────────────────────────────────────
# D. 권한 가드 + 자가정리 쓰기 (DATABASE_URL 있을 때만)
# ──────────────────────────────────────────────────────────
if os.environ.get("DATABASE_URL"):
    print("[D] 권한 가드 (자가정리)")
    # D-1) exec-status PUT 행복경로 + 정리 (대표현황 저장 500 회귀 겸용, 알림 없음)
    WK = "_smoke_" + uuid.uuid4().hex[:6]
    st, _ = api("/api/exec-status", token=token, method="PUT",
                body={"name": "_smoke_", "week_label": WK, "current_tasks": "x", "meeting_notes": "y"})
    check("exec-status PUT 200(저장)", st == 200, "status=%s" % st)
    try:
        c = db(); cur = c.cursor(); cur.execute("DELETE FROM ext_exec_status WHERE week_label=%s", (WK,)); c.commit(); c.close()
    except Exception:
        pass

    # D-2) 전자결재 삭제 권한: member 403, pm 200 (테스트 행 자가정리, 알림 미유발)
    RID = "zz_smoke_" + uuid.uuid4().hex[:6]
    try:
        c = db(); cur = c.cursor()
        cur.execute("""INSERT INTO ext_approval_requests
            (id,req_type,title,amount,detail,requester,approver,status,attachments,items,cert_info,book_info,created_at,updated_at,pm_approver,admin_approver,pm_status,admin_status)
            VALUES (%s,'personal_expense','[smoke]',1,'','_other_','Blair','승인','[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'{}'::jsonb,NOW(),NOW(),'Morgan','Blair','승인','승인')""", (RID,))
        cur.execute("UPDATE ext_users SET role='member' WHERE id='smoke-tester'")
        c.commit(); c.close()
        mt = api("/api/auth/login-user", body={"email": EMAIL, "password": PW})[1].get("token")
        st, _ = api("/api/approvals/" + RID, token=mt, method="DELETE")
        check("member 타인 결재 삭제 차단(403)", st == 403, "status=%s" % st)
        c = db(); cur = c.cursor(); cur.execute("UPDATE ext_users SET role='pm' WHERE id='smoke-tester'"); c.commit(); c.close()
        pt = api("/api/auth/login-user", body={"email": EMAIL, "password": PW})[1].get("token")
        st, _ = api("/api/approvals/" + RID, token=pt, method="DELETE")
        check("pm 결재 삭제 허용(200)", st == 200, "status=%s" % st)
    finally:
        try:
            c = db(); cur = c.cursor()
            cur.execute("DELETE FROM ext_approval_requests WHERE id=%s", (RID,))
            cur.execute("UPDATE ext_users SET role='member' WHERE id='smoke-tester'")
            c.commit(); c.close()
        except Exception:
            pass
else:
    print("[D] 권한 가드 — DATABASE_URL 없어 건너뜀(API만 모드)")

# ──────────────────────────────────────────────────────────
print("\n=== SUMMARY: %d passed, %d failed ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
