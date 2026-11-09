# -*- coding: utf-8 -*-
"""순수 유틸 함수 (외부 의존성 없음) — main.py에서 분리(G1 모듈 분리).

여기엔 DB·전역·app에 의존하지 않는 순수 함수만 둔다. 그래야 단독 테스트 가능하고
import 순환이 없다. main.py는 두 import 컨텍스트(src.main / main) 모두 지원하는
try/except 폴백으로 가져온다.
"""


def _fmt_krw(n: int) -> str:
    """한국 원화 포맷: ₩1,234,567"""
    return f"₩{n:,}"


def _calc_trend(cur: float, prev: float) -> float:
    """전기간 대비 변동률(%) 계산"""
    if prev == 0:
        return 0.0 if cur == 0 else 100.0
    return round((cur - prev) / abs(prev) * 100, 1)


def _metric_status(trend: float, higher_is_better: bool = True) -> str:
    """트렌드에 따른 상태 색상 결정"""
    if higher_is_better:
        return "green" if trend >= 0 else ("yellow" if trend > -10 else "red")
    else:
        return "green" if trend <= 0 else ("yellow" if trend < 10 else "red")


_REPORT_LIST_SECTIONS = (
    ("meetingAgenda", "items"),       # 회의 안건
    ("directions", "items"),          # 방향성
    ("completedResults", "items"),    # 완료 프로젝트 결과
    ("activeProjects", "projects"),   # 진행 중인 프로젝트
)


def _merge_id_list(server_list, client_list, deleted: set):
    """id 기준 리스트 병합.

    - 클라이언트 순서를 우선하고, 서버에만 있는 항목(다른 사용자가 추가)은 뒤에 보존
    - deleted 집합에 포함된 id는 제외 (삭제된 항목이 되살아나지 않도록)
    반환: (병합 리스트, 클라이언트가 보낸 목록과 달라졌는지 여부)
    """
    server_list = server_list if isinstance(server_list, list) else []
    client_list = client_list if isinstance(client_list, list) else []

    merged, seen = [], set()
    for it in client_list:
        iid = it.get("id") if isinstance(it, dict) else None
        if iid and iid in deleted:
            continue
        merged.append(it)
        if iid:
            seen.add(iid)

    added = False
    for it in server_list:
        sid = it.get("id") if isinstance(it, dict) else None
        if not sid or sid in seen or sid in deleted:
            continue
        merged.append(it)
        added = True

    changed = added or len(merged) != len(client_list)
    return merged, changed


def _merge_report(server_data: dict, incoming: dict):
    """리포트 저장 시 서버 데이터와 id 기준으로 병합한다.

    두 사용자가 대시보드를 동시에 열어두고 항목을 추가하면, 마지막에 저장한
    쪽이 상대방 항목을 통째로 덮어써 항목이 사라지는 문제가 있었다. 모든
    리스트형 섹션(안건·방향성·완료결과·진행프로젝트·팀우선순위·지표·칸반)을
    id 기준으로 병합해 양쪽 추가분을 모두 보존한다. 삭제는 전역 _deletedIds
    목록으로 처리하여, 병합 과정에서 삭제된 항목이 되살아나지 않도록 한다.

    반환: (병합된 incoming, 병합으로 목록이 바뀌었는지 여부)
    """
    if not isinstance(server_data, dict) or not isinstance(incoming, dict):
        return incoming, False

    # 전역 삭제 id 집합 (양쪽 _deletedIds + 레거시 meetingAgenda.deletedIds)
    def _collect_deleted(d):
        ids = {x for x in (d.get("_deletedIds") or []) if x}
        ma = d.get("meetingAgenda") or {}
        ids |= {x for x in (ma.get("deletedIds") or []) if x}
        return ids

    deleted = _collect_deleted(server_data) | _collect_deleted(incoming)
    changed = False

    # 1) {key: {listKey: [...]}} 형태 섹션
    for sec_key, list_key in _REPORT_LIST_SECTIONS:
        i_sec = incoming.get(sec_key)
        if not isinstance(i_sec, dict):
            continue
        s_sec = server_data.get(sec_key) or {}
        merged, ch = _merge_id_list(s_sec.get(list_key), i_sec.get(list_key), deleted)
        i_sec[list_key] = merged
        changed = changed or ch

    # 2) teamPriorities — 최상위 배열
    if isinstance(incoming.get("teamPriorities"), list):
        merged, ch = _merge_id_list(
            server_data.get("teamPriorities"), incoming.get("teamPriorities"), deleted)
        incoming["teamPriorities"] = merged
        changed = changed or ch

    # 3) metricSections — 섹션을 id로 병합 + 각 섹션의 metrics도 병합
    if isinstance(incoming.get("metricSections"), list):
        s_secs = {s.get("id"): s for s in (server_data.get("metricSections") or [])
                  if isinstance(s, dict) and s.get("id")}
        merged_secs, ch = _merge_id_list(
            server_data.get("metricSections"), incoming.get("metricSections"), deleted)
        changed = changed or ch
        for sec in merged_secs:
            if not isinstance(sec, dict):
                continue
            s_sec = s_secs.get(sec.get("id"))
            if s_sec is not None:
                m, mch = _merge_id_list(s_sec.get("metrics"), sec.get("metrics"), deleted)
                sec["metrics"] = m
                changed = changed or mch
        incoming["metricSections"] = merged_secs

    # 4) kanbanColumns — 컬럼 id를 매칭해 각 컬럼의 issues 병합
    if isinstance(incoming.get("kanbanColumns"), list):
        s_cols = {c.get("id"): c for c in (server_data.get("kanbanColumns") or [])
                  if isinstance(c, dict) and c.get("id")}
        for col in incoming["kanbanColumns"]:
            if not isinstance(col, dict):
                continue
            s_col = s_cols.get(col.get("id"))
            if s_col is not None:
                m, ch = _merge_id_list(s_col.get("issues"), col.get("issues"), deleted)
                col["issues"] = m
                changed = changed or ch

    # 병합된 전역 삭제 목록 저장 (최근 500건)
    if deleted:
        incoming["_deletedIds"] = list(deleted)[-500:]

    return incoming, changed


# Optional historical availability boundary, supplied by the installer.
try:
    from src.installation_policy import CF_CUTOVER_DATE
except ImportError:
    from installation_policy import CF_CUTOVER_DATE


def _daily_summary_to_maps(summary: dict, usd_krw_rate: float, markup: float = 1.0):
    """운영 /dashboard/daily-summary 응답 → (순매출, 구매수, API비용 원화) 날짜별 dict.

    - 순매출 = net_sales.amount (승인 − 취소/환불 + 충전, KST). 구매수 = net_sales.count(승인 주문 수).
    - API 비용은 컷오버일 이후만 채운다. 그 이전 날짜의 0은 '데이터 없음'이라 기존 값을 덮으면 안 된다.
    - llm_cost_available=false(운영 AE 자격 없음)면 API 비용은 비워서 기존 소스를 유지한다.
    """
    revenue, buyers, api = {}, {}, {}
    llm_ok = bool(summary.get("llm_cost_available", True))
    for row in summary.get("days") or []:
        d = str(row.get("date") or "")
        if not d:
            continue
        ns = row.get("net_sales") or {}
        revenue[d] = int(round(float(ns.get("amount") or 0)))
        buyers[d] = int(ns.get("count") or 0)
        if llm_ok and d >= CF_CUTOVER_DATE:
            usd = float((row.get("llm") or {}).get("cost_usd") or 0)
            api[d] = round(usd * markup * usd_krw_rate)
    return revenue, buyers, api
