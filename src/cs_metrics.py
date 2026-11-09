"""CS 지표 집계 — A1 인입률 · D1 환불율 (독립 /cs 페이지용).

데이터 소스:
  - Installer-configured BigQuery orders table → 주간 고유구매자 + 환불율 (created 주 기준, KST)
  - 채널톡 Open API `/open/v5/user-chats` → 주간 CS 인입 건수 (createdAt 기준, KST)

정의:
  - A1 인입률 = 주간 CS 인입 건수 ÷ 주간 고유 구매자 수 (%)
  - D1 환불율 = 주간 취소(cancelled_at) 건수 ÷ 주간 결제(PAID) 건수 (%)
              + 금액 환불율 = cancelled_amount ÷ total_amount

비밀값은 환경변수로만: CHANNELTALK_ACCESS_KEY / CHANNELTALK_ACCESS_SECRET.
모든 외부 호출은 graceful degrade — 실패해도 페이지는 떠야 함(부분 None + errors).
"""
import os
import json
import time as _time
import datetime as _dt
import concurrent.futures as _cf
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

KST = ZoneInfo("Asia/Seoul")

BQ_PROJECT = os.environ.get("BQ_PROJECT", "")
_DEFAULT_KEY_FILE = ""
BQ_ORDERS_TABLE = os.environ.get("BQ_ORDERS_TABLE", "")
CHANNELTALK_BASE = "https://api.channel.io"
# 상담일지 구글시트 (CS KPI 트래킹). env로 덮어쓰기 가능.
CS_SHEET_ID = os.environ.get("CS_SHEET_ID", "")
CS_SHEET_TAB = os.environ.get("CS_SHEET_TAB", "")
# 감성/불만 분류 (Gemini). 채팅ID 기준 영구 캐시(DB) — 과거 대화 감정은 안 바뀜.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CS_SENTIMENT_MODEL = os.environ.get("CS_SENTIMENT_MODEL", "gemini-3.1-flash-lite-preview")


def _sa_credentials(scopes=None):
    """서비스계정 자격증명. 운영(GOOGLE_CREDENTIALS_JSON env) / 로컬(키 파일) 모두 지원.
    main.py와 동일한 해석 순서: env JSON → GOOGLE_APPLICATION_CREDENTIALS → 기본 키파일."""
    from google.oauth2 import service_account
    gcp_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if gcp_json:
        return service_account.Credentials.from_service_account_info(json.loads(gcp_json), scopes=scopes)
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or _DEFAULT_KEY_FILE
    if not key_path:
        raise RuntimeError("Explicit Google service-account credentials are required")
    return service_account.Credentials.from_service_account_file(key_path, scopes=scopes)


def _bq_credentials():
    return _sa_credentials()

# 결과 캐시 (채널톡 페이지네이션이 무거우니 기본 30분) — weeks별로 보관
_CACHE_TTL = int(os.environ.get("CS_METRICS_TTL", "1800"))
_cache: dict[int, tuple[float, dict]] = {}


def _monday_kst(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def _ms_to_kst_date(ms: int) -> _dt.date:
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).astimezone(KST).date()


def fetch_weekly_orders(weeks: int) -> tuple[dict, str | None]:
    """BigQuery: 주(월요일, KST)별 결제건수·고유구매자·취소건수·금액. 실패 시 ({}, err)."""
    if not BQ_PROJECT or not BQ_ORDERS_TABLE:
        return {}, "BQ_PROJECT / BQ_ORDERS_TABLE 미설정"
    try:
        from google.cloud import bigquery

        bq = bigquery.Client(project=BQ_PROJECT, credentials=_bq_credentials())
        days = weeks * 7 + 7
        q = f"""
        WITH o AS (
          SELECT
            DATE_TRUNC(DATE(created_at, 'Asia/Seoul'), WEEK(MONDAY)) AS wk,
            COALESCE(user_id, customer_phone) AS buyer_key,
            payment_status,
            total_amount,
            cancelled_amount,
            cancelled_at
          FROM `{BQ_ORDERS_TABLE}`
          WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        )
        SELECT
          wk,
          COUNTIF(payment_status='PAID') AS paid_orders,
          COUNT(DISTINCT IF(payment_status='PAID', buyer_key, NULL)) AS buyers,
          COUNTIF(cancelled_at IS NOT NULL) AS cancelled_orders,
          SUM(IF(payment_status='PAID', total_amount, 0)) AS gross,
          SUM(IFNULL(cancelled_amount, 0)) AS cancelled_amount
        FROM o GROUP BY wk ORDER BY wk
        """
        out: dict = {}
        for r in bq.query(q).result():
            out[str(r.wk)] = {
                "paid_orders": int(r.paid_orders or 0),
                "buyers": int(r.buyers or 0),
                "cancelled_orders": int(r.cancelled_orders or 0),
                "gross": int(r.gross or 0),
                "cancelled_amount": int(r.cancelled_amount or 0),
            }
        return out, None
    except Exception as e:
        return {}, f"BigQuery 실패: {type(e).__name__}: {str(e)[:160]}"


def fetch_weekly_inflow(weeks: int) -> tuple[dict, str | None]:
    """채널톡: 주(월요일, KST)별 CS 인입 건수. 실패 시 ({}, err)."""
    key = os.environ.get("CHANNELTALK_ACCESS_KEY", "")
    sec = os.environ.get("CHANNELTALK_ACCESS_SECRET", "")
    if not key or not sec:
        return {}, "채널톡 키 미설정(CHANNELTALK_ACCESS_KEY/SECRET)"
    headers = {"x-access-key": key, "x-access-secret": sec}
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=weeks * 7 + 7)
    counts: dict = {}
    scanned = 0
    try:
        with httpx.Client(timeout=20, headers=headers) as cli:
            for state in ("closed", "opened", "snoozed"):
                since = None
                pages = 0
                while pages < 80:  # state당 최대 8000건
                    url = f"{CHANNELTALK_BASE}/open/v5/user-chats?state={state}&limit=100&sortOrder=desc"
                    if since:
                        url += f"&since={since}"
                    r = cli.get(url)
                    if r.status_code != 200:
                        return {}, f"채널톡 HTTP {r.status_code}: {r.text[:120]}"
                    d = r.json()
                    chats = d.get("userChats") or []
                    if not chats:
                        break
                    stop = False
                    for c in chats:
                        ca = c.get("createdAt")
                        if not ca:
                            continue
                        scanned += 1
                        t = _dt.datetime.fromtimestamp(ca / 1000, _dt.timezone.utc)
                        if t < cutoff:
                            stop = True
                            continue
                        wk = str(_monday_kst(_ms_to_kst_date(ca)))
                        counts[wk] = counts.get(wk, 0) + 1
                    pages += 1
                    since = d.get("next")
                    if stop or not since:
                        break
        return counts, None
    except Exception as e:
        return {}, f"채널톡 실패: {type(e).__name__}: {str(e)[:160]}"


def fetch_weekly_inflow_sheet(weeks: int) -> tuple[dict, str | None]:
    """상담일지 시트의 날짜열(A)만 읽어 주간 인입 집계. 채널톡 순차 페이지네이션 대비
    1회 호출이라 빠르고 주 수에 무관. 상담일지 탭과 동일 출처."""
    if not CS_SHEET_ID:
        return {}, "CS_SHEET_ID 미설정"
    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=_sa_credentials(_SHEETS_SCOPE), cache_discovery=False)
        vals = svc.spreadsheets().values().get(
            spreadsheetId=CS_SHEET_ID, range=f"'{CS_SHEET_TAB}'!A2:A").execute().get("values", [])
        cutoff = _dt.datetime.now(KST).date() - _dt.timedelta(days=weeks * 7 + 7)
        counts: dict = {}
        for row in vals:
            if not row or not row[0]:
                continue
            try:
                d = _dt.datetime.strptime(str(row[0]).strip()[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if d < cutoff:
                continue
            wk = str(_monday_kst(d))
            counts[wk] = counts.get(wk, 0) + 1
        return counts, None
    except Exception as e:
        return {}, f"시트 인입 실패: {type(e).__name__}: {str(e)[:160]}"


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else None


def compute_cs_metrics(weeks: int = 8, use_cache: bool = True) -> dict:
    """A1 인입률 · D1 환불율 주간 시계열 + 최신주 + WoW."""
    weeks = max(2, min(weeks, 26))
    if use_cache:
        hit = _cache.get(weeks)
        if hit and hit[0] > _time.time():
            return hit[1]

    # BQ(구매자·환불율)와 시트(인입)를 병렬 실행 — 합산 대기 대신 최댓값
    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_orders = ex.submit(fetch_weekly_orders, weeks)
        f_inflow = ex.submit(fetch_weekly_inflow_sheet, weeks)
        orders, oerr = f_orders.result()
        inflow, ierr = f_inflow.result()
    inflow_source = "sheet"
    # 시트 실패 시 채널톡으로 폴백
    if ierr:
        inflow_ct, ierr_ct = fetch_weekly_inflow(weeks)
        if not ierr_ct:
            inflow, ierr, inflow_source = inflow_ct, None, "channeltalk"

    # 표시할 주 목록 = 최근 weeks주 (현재 주 포함), 월요일 기준
    today = _dt.datetime.now(KST).date()
    this_mon = _monday_kst(today)
    wk_list = [str(this_mon - _dt.timedelta(days=7 * i)) for i in range(weeks)]  # desc

    rows = []
    for wk in wk_list:
        o = orders.get(wk, {})
        buyers = o.get("buyers")
        paid = o.get("paid_orders")
        cancelled = o.get("cancelled_orders")
        gross = o.get("gross")
        camt = o.get("cancelled_amount")
        inf = inflow.get(wk) if inflow else None
        rows.append({
            "week": wk,
            "partial": wk == str(this_mon),
            "inflow": inf,
            "buyers": buyers,
            "paid_orders": paid,
            "cancelled_orders": cancelled,
            "inflow_rate": _pct(inf, buyers) if (inf is not None and buyers) else None,
            "refund_rate": _pct(cancelled, paid) if (cancelled is not None and paid) else None,
            "refund_rate_amount": _pct(camt, gross) if (camt is not None and gross) else None,
        })

    # WoW (직전 주 대비, 퍼센트포인트 차이) — rows는 desc
    for i, row in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        for k in ("inflow_rate", "refund_rate"):
            cur, pv = row.get(k), (prev.get(k) if prev else None)
            row[f"{k}_wow"] = round(cur - pv, 2) if (cur is not None and pv is not None) else None

    # 최신 '완료된' 주 = partial 아닌 첫 행
    latest = next((r for r in rows if not r["partial"]), rows[0] if rows else None)

    # 경보 (최신 완료주 기준)
    alerts = []
    if latest:
        rw = latest.get("refund_rate_wow")
        if rw is not None and rw >= 0.5:
            alerts.append({"level": "bad", "text": f"환불율 전주대비 +{rw}p (현재 {latest.get('refund_rate')}%)"})
        iw = latest.get("inflow_rate_wow")
        if iw is not None and iw >= 2:
            alerts.append({"level": "warn", "text": f"인입률 전주대비 +{iw}p (현재 {latest.get('inflow_rate')}%)"})

    errors = [e for e in (oerr, ierr) if e]
    result = {
        "generated_at": _dt.datetime.now(KST).isoformat(timespec="seconds"),
        "weeks": rows,
        "latest": latest,
        "sources": {"bigquery": oerr is None, "inflow": ierr is None, "inflow_source": inflow_source},
        "alerts": alerts,
        "errors": errors,
        "definitions": {
            "inflow_rate": f"주간 CS 인입({'상담일지 시트' if inflow_source=='sheet' else '채널톡'}) ÷ 주간 고유 구매자(BQ)",
            "refund_rate": "주간 취소건수 ÷ 주간 결제건수 (생성주 기준)",
        },
    }
    if not errors:  # 성공 시 항상 캐시 저장(예열이 use_cache=False로 갱신 가능하게)
        _cache[weeks] = (_time.time() + _CACHE_TTL, result)
    return result


# ════════════════════════════════════════════════════════════════════
# 상담일지 (구글시트 "CS KPI 트래킹" → 상담일지 탭)
# ════════════════════════════════════════════════════════════════════
_SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_RAW_CAP = 2500  # 원본대화 셀 최대 길이 (페이로드 보호)
_consult_cache: dict[int, tuple[float, dict]] = {}

# 상담일지 표준 컬럼 (헤더가 비거나 다를 때 폴백)
_CONSULT_COLS = ["날짜", "상담채널", "고객ID", "문의유형(대분류)", "문의유형(상세)",
                 "처리시간(분)", "1차해결여부", "고객점수(1-5)", "AI요약",
                 "전체태그", "담당자", "채팅ID", "원본대화"]

# 문의유형(대분류) 표기 통일 — 같은 카테고리의 표기 흔들림을 정규 명칭으로 병합.
# (단일 카테고리만 매핑 — '서비스 이용/결제·환불' 같은 다중태그는 의미가 달라 건드리지 않음)
_TYPE_CANON = {
    "결제/환불": "결제·환불",
    "결과·해석 문의": "결과 문의",
    "결과·문의": "결과 문의",
}


def _norm_type(t):
    return _TYPE_CANON.get((t or "").strip(), (t or "").strip())


def fetch_consultations() -> tuple[list, list, str | None]:
    """상담일지 탭 전체 읽기 → (header, rows, err). 문의유형(대분류) 표기 통일 적용."""
    if not CS_SHEET_ID:
        return [], [], "CS_SHEET_ID 미설정"
    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=_sa_credentials(_SHEETS_SCOPE), cache_discovery=False)
        rng = f"'{CS_SHEET_TAB}'!A1:M"
        vals = svc.spreadsheets().values().get(
            spreadsheetId=CS_SHEET_ID, range=rng).execute().get("values", [])
        if not vals:
            return _CONSULT_COLS, [], None
        header = vals[0] if vals[0] and vals[0][0] == "날짜" else _CONSULT_COLS
        body = vals[1:] if header is vals[0] else vals
        for r in body:  # 문의유형(대분류)=index 3 표기 통일
            if len(r) > 3:
                r[3] = _norm_type(r[3])
        return header, body, None
    except Exception as e:
        return [], [], f"시트 읽기 실패: {type(e).__name__}: {str(e)[:160]}"


_completed_cache = {"exp": 0.0, "ids": set()}


def fetch_completed_ids() -> tuple[set, str | None]:
    """'처리완료' 탭의 채팅ID 집합(완료된 상담). 30분 캐시. 열린(미처리) 큐 판정용."""
    if _completed_cache["exp"] > _time.time():
        return _completed_cache["ids"], None
    if not CS_SHEET_ID:
        return set(), "CS_SHEET_ID 미설정"
    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=_sa_credentials(_SHEETS_SCOPE), cache_discovery=False)
        vals = svc.spreadsheets().values().get(
            spreadsheetId=CS_SHEET_ID, range="'처리완료'!A2:A").execute().get("values", [])
        ids = set(r[0].strip() for r in vals if r and r[0].strip())
        _completed_cache.update({"exp": _time.time() + _CACHE_TTL, "ids": ids})
        return ids, None
    except Exception as e:
        return set(), f"처리완료 읽기 실패: {type(e).__name__}: {str(e)[:120]}"


def _filter_rows_by_date(rows, date_from, date_to):
    """rows를 날짜(YYYY-MM-DD) 범위로 필터. from/to 모두 None이면 그대로."""
    if not date_from and not date_to:
        return rows
    out = []
    for r in rows:
        d, _ = _parse_dt(r[0] if r else "")
        if d is None:
            continue
        if date_from and str(d) < date_from:
            continue
        if date_to and str(d) > date_to:
            continue
        out.append(r)
    return out


def _to_num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _summarize_consultations(rows: list) -> dict:
    """전 행 집계: 채널별·유형별 건수, 평균 처리시간, 1차해결률, 고객점수 평균, 기간."""
    total = len(rows)
    by_channel: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_type_detail: dict[str, dict[str, int]] = {}  # 대분류 -> {상세: count}
    dur_sum = dur_n = 0.0
    resolved = 0
    score_sum = score_n = 0.0
    dates = []
    for r in rows:
        ch = (r[1] if len(r) > 1 else "") or "미상"
        by_channel[ch] = by_channel.get(ch, 0) + 1
        t = (r[3] if len(r) > 3 else "") or "미분류"
        by_type[t] = by_type.get(t, 0) + 1
        det = (r[4] if len(r) > 4 else "").strip() or "(상세없음)"
        by_type_detail.setdefault(t, {})
        by_type_detail[t][det] = by_type_detail[t].get(det, 0) + 1
        d = _to_num(r[5]) if len(r) > 5 else None
        if d is not None:
            dur_sum += d; dur_n += 1
        if len(r) > 6 and str(r[6]).strip().upper() in ("O", "Y", "TRUE", "1", "해결"):
            resolved += 1
        sc = _to_num(r[7]) if len(r) > 7 else None
        if sc is not None:
            score_sum += sc; score_n += 1
        if len(r) > 0 and r[0]:
            dates.append(str(r[0]))
    def top(d):
        return sorted([{"key": k, "count": v} for k, v in d.items()], key=lambda x: -x["count"])
    return {
        "total": total,
        "by_channel": top(by_channel),
        "by_type": top(by_type),
        "by_type_detail": {k: top(dv) for k, dv in by_type_detail.items()},
        "avg_duration_min": round(dur_sum / dur_n, 1) if dur_n else None,
        "first_resolution_rate": round(100.0 * resolved / total, 1) if total else None,
        "avg_score": round(score_sum / score_n, 2) if score_n else None,
        "score_responses": int(score_n),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def _parse_dt(s):
    """'YYYY-MM-DD HH:MM' (시는 0-padding 없을 수 있음) → (date, hour) 관대하게 파싱. 실패 시 (None,None)."""
    try:
        s = str(s).strip()
        dpart = s[:10]
        d = _dt.datetime.strptime(dpart, "%Y-%m-%d").date()
        hour = None
        if len(s) > 10:
            tpart = s[10:].strip()
            if ":" in tpart:
                hour = int(tpart.split(":")[0])
        return d, hour
    except Exception:
        return None, None


_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _rowdict(r):
    return {_CONSULT_COLS[i]: (r[i] if i < len(r) else "") for i in range(min(len(_CONSULT_COLS), 12))}


def _consult_insights(rows: list, completed_ids=None, reopen_days: int = 7, max_weeks: int = 8) -> dict:
    """기간 필터된 rows 인사이트. 주차는 데이터에서 도출.
    - 1차 미해결: 1차해결여부 != O (재응대 필요했던 건)
    - 열린(미처리) 건: '처리완료' 탭에 없는 채팅ID (실제 오픈 큐)
    """
    completed_ids = completed_ids or set()
    grid = [[0] * 24 for _ in range(7)]
    wd_tot = [0] * 7
    hr_tot = [0] * 24
    type_week: dict[str, dict[str, int]] = {}
    weeks_present = set()
    cust_dates: dict[str, list[_dt.date]] = {}
    first_unres = []   # 1차 미해결(X)
    open_rows = []     # 처리완료 탭에 없는 = 실제 열린 건

    for r in rows:
        d, hour = _parse_dt(r[0] if r else "")
        if d is not None:
            wd = d.weekday(); wd_tot[wd] += 1
            if hour is not None and 0 <= hour <= 23:
                grid[wd][hour] += 1; hr_tot[hour] += 1
            wk = str(_monday_kst(d)); weeks_present.add(wk)
            t = (r[3] if len(r) > 3 else "") or "미분류"
            type_week.setdefault(t, {})
            type_week[t][wk] = type_week[t].get(wk, 0) + 1
        cid = (r[2] if len(r) > 2 else "") or ""
        if cid and d is not None:
            cust_dates.setdefault(cid, []).append(d)
        if str(r[6] if len(r) > 6 else "").strip().upper() not in ("O", "Y", "TRUE", "1", "해결"):
            first_unres.append(r)
        chatid = (r[11] if len(r) > 11 else "") or ""
        if chatid and chatid not in completed_ids:
            open_rows.append(r)

    total_contacts = sum(len(v) for v in cust_dates.values())
    reopened = 0
    for ds in cust_dates.values():
        ds.sort()
        for i in range(1, len(ds)):
            if (ds[i] - ds[i - 1]).days <= reopen_days:
                reopened += 1
    reopen_rate = round(100.0 * reopened / total_contacts, 1) if total_contacts else None

    weeks_asc = sorted(weeks_present)[-max_weeks:]  # 데이터 기반, 최근 max_weeks
    trend = []
    for t, wm in type_week.items():
        series = [wm.get(w, 0) for w in weeks_asc]   # asc: 오래된→최근
        latest = series[-1] if series else 0
        prev = series[-2] if len(series) >= 2 else 0
        wow = round(100.0 * (latest - prev) / prev, 0) if prev else None
        trend.append({"type": t, "series": series, "latest": latest, "prev": prev,
                      "wow_pct": wow, "total": sum(wm.values())})
    trend.sort(key=lambda x: -x["total"])
    spikes = [x for x in trend if x["latest"] >= 5 and x["wow_pct"] is not None and x["wow_pct"] >= 30]

    first_unres.sort(key=lambda r: (r[0] if r and len(r) > 0 else ""), reverse=True)
    open_rows.sort(key=lambda r: (r[0] if r and len(r) > 0 else ""), reverse=True)

    fr = None
    if rows:
        resolved = sum(1 for r in rows if str(r[6] if len(r) > 6 else "").strip().upper() in ("O", "Y", "TRUE", "1", "해결"))
        fr = round(100.0 * resolved / len(rows), 1)

    alerts = []
    if len(open_rows):
        alerts.append({"level": "bad", "text": f"열린(미처리) 상담 {len(open_rows)}건 — 확인 필요"})
    if fr is not None and fr < 80:
        alerts.append({"level": "bad", "text": f"1차 해결률 {fr}% (<80%)"})
    for s in spikes[:4]:
        alerts.append({"level": "warn", "text": f"'{s['type']}' 급증 — 최근주 {s['latest']}건 (전주대비 +{int(s['wow_pct'])}%)"})

    return {
        "heatmap": {"grid": grid, "weekday_total": wd_tot, "hour_total": hr_tot, "weekdays": _WEEKDAYS},
        "reopen_rate": reopen_rate, "reopen_days": reopen_days,
        "type_trend": trend[:8], "trend_weeks": weeks_asc, "spikes": spikes,
        "first_unresolved_count": len(first_unres), "first_unresolved_rows": [_rowdict(r) for r in first_unres[:60]],
        "open_count": len(open_rows), "open_rows": [_rowdict(r) for r in open_rows[:60]],
        "alerts": alerts,
    }


# ════════════════════════════════════════════════════════════════════
# 감성/불만 전수 태깅 (Gemini) — CSAT 공란 우회. 채팅ID 기준 DB 영구 캐시.
# ════════════════════════════════════════════════════════════════════
def _pg():
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL 미설정")
    return psycopg2.connect(url, connect_timeout=10)


def _ensure_sentiment_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_cs_sentiment(
        chat_id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())""")


def get_sentiment_cache(chat_ids) -> dict:
    ids = [c for c in set(chat_ids) if c]
    if not ids:
        return {}
    conn = _pg(); cur = conn.cursor()
    _ensure_sentiment_table(cur); conn.commit()
    out = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        cur.execute("SELECT chat_id, label FROM ext_cs_sentiment WHERE chat_id = ANY(%s)", (chunk,))
        for cid, lab in cur.fetchall():
            out[cid] = lab
    conn.close()
    return out


def _gemini_classify(convos: list) -> dict:
    """convos: [(idx, text)] → {idx: label}. Gemini 1콜."""
    prompt = ("다음은 고객상담 대화들이다. 각 대화에서 고객의 감정을 만족/중립/불만 중 하나로 분류하라.\n"
              "- 불만: 화남·실망·항의·환불요구·반복문의·오류제보로 인한 불편\n"
              "- 만족: 감사·해결됨·긍정 반응\n- 중립: 단순 문의·정보 요청\n"
              '반드시 JSON만 출력: {"results":[{"i":1,"label":"불만"}, ...]}\n\n')
    for idx, text in convos:
        prompt += f"[{idx}] {str(text)[:800]}\n\n"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CS_SENTIMENT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    r = httpx.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    i, j = txt.find("{"), txt.rfind("}")
    obj = json.loads(txt[i:j + 1])
    out = {}
    for x in obj.get("results", []):
        lab = str(x.get("label", "")).strip()
        if lab in ("만족", "중립", "불만"):
            try:
                out[int(x["i"])] = lab
            except Exception:
                pass
    return out


def classify_new_sentiments(rows, max_new: int = 200, batch: int = 12) -> dict:
    """캐시에 없는 상담 최대 max_new건을 Gemini로 분류·저장. 증분."""
    if not GEMINI_API_KEY:
        return {"classified": 0, "error": "GEMINI_API_KEY 미설정"}
    cand, seen = [], set()
    rows = sorted(rows, key=lambda r: (r[0] if r and len(r) > 0 else ""), reverse=True)  # 최근부터 분류
    for r in rows:
        cid = (r[11] if len(r) > 11 else "") or ""
        if not cid or cid in seen:
            continue
        seen.add(cid)
        text = ((r[12] if len(r) > 12 else "") or (r[8] if len(r) > 8 else "")).strip()
        if text:
            cand.append((cid, text))
    cached = get_sentiment_cache([c for c, _ in cand])
    todo = [(c, t) for c, t in cand if c not in cached][:max_new]
    total_uncached = sum(1 for c, _ in cand if c not in cached)
    if not todo:
        return {"classified": 0, "remaining": 0, "total_cached": len(cached)}
    conn = _pg(); cur = conn.cursor(); _ensure_sentiment_table(cur); conn.commit()
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        idx_map = {k + 1: chunk[k][0] for k in range(len(chunk))}
        try:
            res = _gemini_classify([(k + 1, chunk[k][1]) for k in range(len(chunk))])
        except Exception:
            continue
        for ii, lab in res.items():
            cid = idx_map.get(ii)
            if cid:
                cur.execute("INSERT INTO ext_cs_sentiment(chat_id,label) VALUES(%s,%s) "
                            "ON CONFLICT(chat_id) DO UPDATE SET label=EXCLUDED.label", (cid, lab))
                done += 1
        conn.commit()
    conn.close()
    return {"classified": done, "remaining": max(0, total_uncached - done), "total_cached": len(cached) + done}


def sentiment_summary(rows) -> dict:
    """캐시만 읽어 불만율·커버리지 산출(빠름). 분류 자체는 classify_new_sentiments가 채움."""
    try:
        cids = [(r[11] if len(r) > 11 else "") for r in rows]
        cids = [c for c in cids if c]
        cache = get_sentiment_cache(set(cids))
    except Exception as e:
        return {"classified": 0, "total": len(rows), "coverage": 0, "complaint_rate": None,
                "counts": {"만족": 0, "중립": 0, "불만": 0}, "error": str(e)[:120]}
    counts = {"만족": 0, "중립": 0, "불만": 0}
    for c in cids:
        lab = cache.get(c)
        if lab in counts:
            counts[lab] += 1
    classified = sum(counts.values()); total = len(cids)
    return {
        "counts": counts, "classified": classified, "total": total,
        "coverage": round(100.0 * classified / total, 1) if total else 0,
        "complaint_rate": round(100.0 * counts["불만"] / classified, 1) if classified else None,
    }


def sentiment_breakdown(rows) -> dict:
    """감성 캐시 × 상담일지 조인 → 유형(대분류)·상세별 불만율. '어디가 불만을 끄는가' 도출.
    제품개선 후보 = 불만율 높고 물량 있는 상세유형."""
    try:
        cache = get_sentiment_cache(set((r[11] if len(r) > 11 else "") for r in rows))
    except Exception as e:
        return {"by_type": [], "by_detail": [], "error": str(e)[:120]}
    from collections import defaultdict
    def blank():
        return {"만족": 0, "중립": 0, "불만": 0}
    bt = defaultdict(blank); bd = defaultdict(blank)
    for r in rows:
        lab = cache.get(r[11] if len(r) > 11 else "")
        if lab not in ("만족", "중립", "불만"):
            continue
        t = (r[3] if len(r) > 3 else "") or "미분류"
        det = ((r[4] if len(r) > 4 else "") or "").strip() or "(상세없음)"
        bt[t][lab] += 1
        bd[(t, det)][lab] += 1

    def pack(d, keyfn):
        out = []
        for k, v in d.items():
            tot = sum(v.values())
            out.append({**keyfn(k), "total": tot, "complaint": v["불만"],
                        "rate": round(100.0 * v["불만"] / tot, 1) if tot else 0})
        return sorted(out, key=lambda x: -x["complaint"])
    by_detail = pack(bd, lambda k: {"type": k[0], "detail": k[1]})
    for x in by_detail:  # 제품개선 후보: 불만율≥60% & 물량≥30
        x["candidate"] = x["rate"] >= 60 and x["total"] >= 30
    return {"by_type": pack(bt, lambda k: {"type": k})[:12], "by_detail": by_detail[:15]}


def compute_consultations(limit: int = 300, date_from: str = None, date_to: str = None,
                          use_cache: bool = True) -> dict:
    """상담일지 요약+인사이트(선택 기간) + 최근 limit행(원본대화 캡). 30분 캐시."""
    limit = max(50, min(limit, 2000))
    date_from = date_from or None
    date_to = date_to or None
    ck = (limit, date_from or "", date_to or "")
    if use_cache:
        hit = _consult_cache.get(ck)
        if hit and hit[0] > _time.time():
            return hit[1]

    header, all_rows, err = fetch_consultations()
    completed, _cerr = fetch_completed_ids()
    rows = _filter_rows_by_date(all_rows, date_from, date_to)  # 기간 필터
    summary = _summarize_consultations(rows) if rows else {}
    insights = _consult_insights(rows, completed) if rows else {}
    if rows:
        sent = sentiment_summary(rows)
        insights["sentiment"] = sent
        insights["complaint_breakdown"] = sentiment_breakdown(rows)
        # 불만율 경보 (커버리지 30%+ 일 때만 신뢰)
        if sent.get("coverage", 0) >= 30 and sent.get("complaint_rate") is not None and sent["complaint_rate"] >= 25:
            insights.setdefault("alerts", []).append(
                {"level": "bad", "text": f"불만 비율 {sent['complaint_rate']}% (분류 {sent['coverage']}% 기준)"})

    # 최근순 정렬 (날짜 desc) 후 limit
    rows_sorted = sorted(rows, key=lambda r: (r[0] if r and len(r) > 0 else ""), reverse=True)
    recent = rows_sorted[:limit]
    # dict화 + 원본대화 캡
    out_rows = []
    for r in recent:
        rec = {_CONSULT_COLS[i]: (r[i] if i < len(r) else "") for i in range(len(_CONSULT_COLS))}
        raw = rec.get("원본대화", "") or ""
        if len(raw) > _RAW_CAP:
            rec["원본대화"] = raw[:_RAW_CAP] + " …(생략)"
        out_rows.append(rec)

    result = {
        "generated_at": _dt.datetime.now(KST).isoformat(timespec="seconds"),
        "range": {"from": date_from, "to": date_to, "filtered": len(rows), "all": len(all_rows)},
        "summary": summary,
        "insights": insights,
        "columns": _CONSULT_COLS,
        "rows": out_rows,
        "shown": len(out_rows),
        "source_ok": err is None,
        "errors": [err] if err else [],
    }
    if not err:  # 성공 시 항상 캐시 저장
        _consult_cache[ck] = (_time.time() + _CACHE_TTL, result)
    return result


def build_weekly_report_text() -> str:
    """주간 CS 리포트 Slack 텍스트 생성(캐시 사용 — 예열돼 있으면 즉시)."""
    m = compute_cs_metrics(8, use_cache=True)
    c = compute_consultations(limit=120, use_cache=True)
    L = m.get("latest") or {}
    ins = c.get("insights") or {}
    s = c.get("summary") or {}
    sent = ins.get("sentiment") or {}

    def p(v):
        return "—" if v is None else f"{v}%"

    def w(v):
        return "" if v is None else f" (전주 {'+' if v > 0 else ''}{v}p)"

    out = [f"*📊 주간 CS 리포트* — 최신 완료주 {L.get('week', '-')}",
           f"• A1 인입률 *{p(L.get('inflow_rate'))}*{w(L.get('inflow_rate_wow'))}  ·  D1 환불율 *{p(L.get('refund_rate'))}*{w(L.get('refund_rate_wow'))}",
           f"• 주간 구매자 {L.get('buyers', '-')}명 · CS 인입 {L.get('inflow', '-')}건",
           f"• 1차해결률 {p(s.get('first_resolution_rate'))} · 재문의율 {p(ins.get('reopen_rate'))} · 1차미해결 {ins.get('first_unresolved_count', '-')}건 · 열린건 {ins.get('open_count', '-')}건"]
    if sent.get("complaint_rate") is not None:
        out.append(f"• 불만율(AI) {sent['complaint_rate']}% (분류 {sent.get('coverage', 0)}%)")
    spikes = ins.get("spikes") or []
    if spikes:
        out.append("• ⚠️ 급증 유형: " + ", ".join(f"{x['type']} +{int(x['wow_pct'])}%" for x in spikes[:3]))
    alerts = (m.get("alerts") or []) + [a for a in (ins.get("alerts") or []) if a.get("level") == "bad"]
    if alerts:
        out.append("*경보:*")
        out += [f"  • {a.get('text', '')}" for a in alerts[:6]]
    # 대시보드 주소는 배포 환경마다 다름(Railway → Cloudflare) — PUBLIC_BASE_URL 우선
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        out.append(f"<{base}/cs|→ CS 대시보드 열기>")
    return "\n".join(out)
