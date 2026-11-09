"""CI의 격리된 앱에서 근태 SELECT SQL과 응답 계약만 확인한다."""
import os
from local_test_support import require_local_tests, require_test_password
require_local_tests(base=os.environ.get("DASH_BASE", "http://localhost:8000"), database_url=os.environ.get("DATABASE_URL"))
require_test_password("E2E_PW" if __file__.endswith("e2e_test.py") else "SMOKE_PW")
import sys

if os.environ.get("SMOKE_CI") != "1":
    raise SystemExit("CI 전용 검사: SMOKE_CI=1 및 DASH_BASE=http://localhost:8000만 허용합니다")
if not os.environ.get("SMOKE_PW"):
    raise SystemExit("CI 테스트 전용 임시 비밀번호가 필요합니다")

import json
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("CI 근태 검사는 리다이렉트를 허용하지 않습니다")


def main():
    opener = build_opener(ProxyHandler({}), NoRedirect())
    token = ""

    def api(path, body=None):
        headers = {"Content-Type": "application/json", "User-Agent": "weekly-report-attendance-ci/1.0"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(os.environ.get("DASH_BASE", "http://localhost:8000") + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("CI 근태 API 응답 상태 오류")
            return json.load(response)

    # seed_test_db와 같은 실행별 임시 자격증명이며 저장소·로그에 값을 남기지 않는다.
    token = api("/api/auth/login-user", {"email": os.environ.get("SMOKE_EMAIL", "member@example.test"),
                                       "password": os.environ["SMOKE_PW"]}).get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("CI 테스트 계정 로그인 실패")
    monthly = api("/api/attendance/monthly?year=2026&month=11")
    assert all(isinstance(monthly.get(key), list) for key in ("rows", "records", "halfday_warnings"))
    assert monthly.get("year") == "2026" and monthly.get("month") == "11"
    assert monthly.get("month_start") == "2026-11-01" and monthly.get("month_end") == "2026-12-01"
    assert monthly.get("timezone") == "Asia/Seoul"
    assert monthly.get("period_type") == "month" and isinstance(monthly.get("coverage_warnings"), list)
    annual = api("/api/attendance/monthly?year=2026&month=all")
    assert all(isinstance(annual.get(key), list) for key in ("rows", "records", "halfday_warnings", "coverage_warnings"))
    assert annual.get("year") == "2026" and annual.get("month") == "all"
    assert annual.get("period_type") == "year" and annual.get("timezone") == "Asia/Seoul"
    assert annual.get("period_start") == "2026-01-01" and annual.get("period_end") == "2027-01-01"
    assert all(all(isinstance(row.get(flag), bool) for flag in ("is_missing", "missing_clock_in", "missing_clock_out"))
               for row in annual["records"])
    daily = api("/api/attendance?date=2026-11-05")
    assert isinstance(daily.get("rows"), list) and isinstance(daily.get("halfday_warnings"), list)
    assert daily.get("date") == "2026-11-05" and daily.get("timezone") == "Asia/Seoul"
    print("attendance_ci: monthly/daily SELECT and response contracts passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # 자격증명·JWT·응답 본문을 로그에 출력하지 않는다.
        print("attendance_ci: failed (" + type(error).__name__ + ")", file=sys.stderr)
        raise SystemExit(1)
