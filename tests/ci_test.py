# -*- coding: utf-8 -*-
"""CI 오프라인 테스트 — 네트워크·DB 불필요. import 성공 + report 병합 무손실.

CI에서 앱 부팅 전에 빠르게 돌려 문법/임포트/핵심 병합 로직 회귀를 즉시 잡는다.
실행: python tests/ci_test.py   (pytest로도 수집 가능: 함수명 test_*)
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import main as M  # noqa: E402  (import 자체가 문법/임포트 회귀 검사)

_MAIN_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")

# 참조되지만 일부러 CREATE 하지 않는 테이블(부재를 코드가 가드함).
# ext_expenses: 실제 소스는 구글시트, 엔드포인트가 to_regclass로 부재 가드.
_NO_CREATE_ALLOWLIST = {"ext_expenses"}


def test_all_referenced_tables_have_create():
    """빈/신규 DB 회귀 방지 — FROM/INTO/UPDATE/JOIN으로 쓰는 모든 ext_/project_hierarchy
    테이블은 부팅 경로에 CREATE TABLE이 있어야 한다(허용목록 제외).
    과거 ext_leave_balance·ext_daily_metrics 등 6개가 누락돼 빈 DB에서 500 났던
    사고를 정적으로 차단한다."""
    src = open(_MAIN_SRC, encoding="utf-8").read()
    referenced = set(
        m.lower() for m in re.findall(
            r"(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+(ext_[a-z_]+|project_hierarchy)", src, re.I)
    )
    created = set(
        m.lower() for m in re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+(ext_[a-z_]+|project_hierarchy)", src, re.I)
    )
    missing = referenced - created - _NO_CREATE_ALLOWLIST
    assert not missing, "CREATE 누락 테이블(빈 DB에서 500 위험): %s" % sorted(missing)


def test_merge_no_loss():
    """동시편집 무손실: 서버 {1,2} + 들어오는 {2,3} → {1,2,3}"""
    m, _ = M._merge_report(
        {"teamPriorities": [{"id": 1}, {"id": 2}]},
        {"teamPriorities": [{"id": 2}, {"id": 3}]},
    )
    ids = sorted(x["id"] for x in m["teamPriorities"])
    assert ids == [1, 2, 3], ids


def test_merge_no_resurrect():
    """삭제 미복원: _deletedIds=[2] 면 서버의 2가 되살아나지 않음"""
    m, _ = M._merge_report(
        {"teamPriorities": [{"id": 1}, {"id": 2}]},
        {"teamPriorities": [{"id": 1}], "_deletedIds": [2]},
    )
    assert sorted(x["id"] for x in m["teamPriorities"]) == [1]


def test_checklist_merge_keeps_concurrent_section():
    """동시편집: 들어온 값엔 없지만 저장본에만 있는 섹션(다른 사람이 방금 추가)은 보존."""
    base = {"sections": [{"id": "s1", "items": []}, {"id": "X", "items": [{"id": "Xi1"}]}], "weeks": {}}
    incoming = {"sections": [{"id": "s1", "items": []}], "weeks": {}}  # X를 모르는 stale 저장
    m = M._merge_checklist(base, incoming)
    assert sorted(s["id"] for s in m["sections"]) == ["X", "s1"]


def test_checklist_merge_respects_delete():
    """삭제 tombstone이 있으면 저장본의 섹션/항목이 부활하지 않음."""
    base = {"sections": [{"id": "s1", "items": []}, {"id": "X", "items": []}], "weeks": {}}
    incoming = {"sections": [{"id": "s1", "items": []}], "weeks": {}, "_deletedSectionIds": ["X"]}
    m = M._merge_checklist(base, incoming)
    assert [s["id"] for s in m["sections"]] == ["s1"]
    assert "X" in m["_deletedSectionIds"]


def test_checklist_merge_weeks_deep():
    """주차 상태는 itemId 단위 deep-merge — 서로 다른 칸이 둘 다 보존."""
    base = {"sections": [{"id": "s1", "items": []}], "weeks": {"w1": {"a": {"s": "✅"}}}}
    incoming = {"sections": [{"id": "s1", "items": []}], "weeks": {"w1": {"b": {"s": "⏳"}}}}
    m = M._merge_checklist(base, incoming)
    assert m["weeks"]["w1"]["a"]["s"] == "✅"
    assert m["weeks"]["w1"]["b"]["s"] == "⏳"


def test_heartbeat_spec_covers_supervised():
    """B3 회귀 방지 — 슈퍼바이저로 등록한 모든 백그라운드 작업은 _HEARTBEAT_SPEC에
    staleness 임계값이 있어야 모니터가 점검한다(누락 시 그 작업은 감시 사각)."""
    src = open(_MAIN_SRC, encoding="utf-8").read()
    supervised = set(re.findall(r'_start_supervised\("([a-z_]+)"', src))
    assert supervised, "슈퍼바이즈된 작업을 찾지 못함(파서 확인)"
    missing = sorted(t for t in supervised if t not in M._HEARTBEAT_SPEC)
    assert not missing, "_HEARTBEAT_SPEC 누락(모니터 감시 안 됨): %s" % missing


def test_index_html_versioning():
    """진입점 index.html 자산 버전(?v=) 주입 — 모든 app.js/css 참조가 동일 내용해시 버전.
    (immutable 캐시가 안전하려면 버전이 일관되고 옛 수동버전이 남지 않아야 함)"""
    html = M._get_index_html()
    vers = set(re.findall(r'(?:app\.js|tailwind\.css|style\.css)\?v=([0-9a-f]+)', html))
    assert len(vers) == 1, "자산 버전 불일치: %s" % vers
    assert "?v=20260" not in html, "옛 수동 버전(20260…) 잔존 — 주입 실패"


def test_login_throttle():
    """무차별 대입 방어: 임계 횟수 실패 시 잠금(>0초), reset 후 해제."""
    key = "test:1.2.3.4"
    M._login_reset(key)
    assert M._login_locked(key) == 0
    for _ in range(M._LOGIN_FAIL_MAX):
        M._login_register_fail(key)
    assert M._login_locked(key) > 0, "임계 실패 후 잠겨야 함"
    M._login_reset(key)
    assert M._login_locked(key) == 0, "reset 후 해제돼야 함"


def test_daily_summary_to_maps():
    """운영 daily-summary → 지표 맵: 순매출·구매수 전 기간, API비용은 설치자 설정 기준일 이후만.
    기준일 이전 LLM 0은 '데이터 없음'이라 기존 값을 덮으면 안 된다(기존 집계 보존)."""
    import util
    from util import _daily_summary_to_maps
    util.CF_CUTOVER_DATE = "2040-01-31"
    summary = {"llm_cost_available": True, "days": [
        {"date": "2040-01-30", "net_sales": {"amount": 2000, "count": 57},
         "llm": {"request_count": 0, "cost_usd": 0}},
        {"date": "2040-02-01", "net_sales": {"amount": 1000, "count": 36},
         "llm": {"request_count": 80, "cost_usd": 2.5}},
    ]}
    rev, buy, api = _daily_summary_to_maps(summary, 1000, 1.0)
    assert rev == {"2040-01-30": 2000, "2040-02-01": 1000}
    assert buy == {"2040-01-30": 57, "2040-02-01": 36}
    assert api == {"2040-02-01": round(2.5 * 1000)}
    # 운영 AE 자격 없음 → API 비용 비움(기존 소스 유지)
    _, _, api2 = _daily_summary_to_maps(dict(summary, llm_cost_available=False), 1000)
    assert api2 == {}


def test_redirect_mode():
    """이전 모드(REDIRECT_TO): 모든 요청을 새 주소로 308, 헬스체크만 통과. 미설정이면 그대로 통과."""
    import asyncio
    from starlette.requests import Request as _Req
    from starlette.responses import Response as _Resp

    def _req(path, query=b""):
        return _Req({"type": "http", "method": "POST", "path": path, "query_string": query,
                     "headers": [], "scheme": "https", "server": ("old.example", 443)})

    async def _next(_r):
        return _Resp("passed")

    saved = M.REDIRECT_TO
    try:
        M.REDIRECT_TO = "https://new.example"
        r = asyncio.run(M.redirect_to_new_host_middleware(_req("/api/webhooks/linear", b"a=1"), _next))
        assert r.status_code == 308 and r.headers["location"] == "https://new.example/api/webhooks/linear?a=1"
        r = asyncio.run(M.redirect_to_new_host_middleware(_req("/api/health"), _next))
        assert r.body == b"passed"
        M.REDIRECT_TO = ""
        r = asyncio.run(M.redirect_to_new_host_middleware(_req("/"), _next))
        assert r.body == b"passed"
    finally:
        M.REDIRECT_TO = saved


if __name__ == "__main__":
    fails = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("  PASS " + _name)
            except Exception as e:
                fails += 1
                print("  FAIL " + _name + "  | " + repr(e)[:120])
    if fails:
        print("ci_test: %d FAILED" % fails)
        sys.exit(1)
    print("ci_test: all passed")
