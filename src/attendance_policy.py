"""근태의 조회용 정책. DB·네트워크·앱 전역에 의존하거나 원본 행을 변경하지 않는다."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
HALF_DAY_TYPES = ("오전반차", "무급오전반차", "오후반차", "무급오후반차")
MORNING_TYPES = frozenset(HALF_DAY_TYPES[:2])
AFTERNOON_TYPES = frozenset(HALF_DAY_TYPES[2:])
FULL_LEAVE_TYPES = ("연차", "공가", "경조사", "무급", "경조")


def attendance_date(value="", now=None):
    """빈 날짜는 KST 오늘, 명시 날짜는 YYYY-MM-DD만 허용한다."""
    if not value:
        current = now or datetime.now(KST)
        return current.astimezone(KST).date() if current.tzinfo else current.date()
    if isinstance(value, datetime):
        return value.astimezone(KST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    return date.fromisoformat(value)


def month_bounds(year="", month="", now=None):
    """선택월의 [첫날, 다음월 첫날) 구간. DB 연결 전에 입력을 검증한다."""
    current = now or datetime.now(KST)
    if current.tzinfo:
        current = current.astimezone(KST)
    y, m = str(year or current.year), str(month or current.month)
    if not re.fullmatch(r"[0-9]{4}", y) or not re.fullmatch(r"[0-9]{1,2}", m):
        raise ValueError("연도와 월 형식이 올바르지 않습니다")
    start = date(int(y), int(m), 1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


def period_bounds(year="", month="", now=None):
    """월간 상세만 명시적인 all을 연간으로 확장한다. 기존 월 기본값은 유지한다."""
    if month != "all":
        start, end = month_bounds(year, month, now=now)
        if start.year == 9999:
            raise ValueError("연간 공휴일을 조회할 수 있는 연도는 9998년까지입니다")
        return start, end, "month"
    current = now or datetime.now(KST)
    if current.tzinfo:
        current = current.astimezone(KST)
    value = str(year or current.year)
    if not re.fullmatch(r"[0-9]{4}", value):
        raise ValueError("연도 형식이 올바르지 않습니다")
    start = date(int(value), 1, 1)
    return start, date(start.year + 1, 1, 1), "year"


def arrival_status(clock_in):
    """기존 09:30 분 단위 지각 기준만 사용한다. 반차 근무시간은 추정하지 않는다."""
    try:
        value = clock_in if isinstance(clock_in, datetime) else datetime.fromisoformat(str(clock_in).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "확인 필요", False
    if value.tzinfo:
        value = value.astimezone(KST)
    late = value.hour > 9 or (value.hour == 9 and value.minute > 30)
    return ("지각" if late else "정상"), late


def display_status(raw_status, clock_in, half_days=()):
    """승인 오전은 별도 시각 기준을 만들지 않고, 오후는 기존 지각도 함께 표시한다."""
    if raw_status in ("정상", "지각"):
        status, late = raw_status, raw_status == "지각"
    else:
        status, late = arrival_status(clock_in)
    types = {item["leave_type"] for item in half_days}
    labels = [kind for kind in HALF_DAY_TYPES if kind in types]
    if types & MORNING_TYPES:
        return " · ".join(labels), False
    if labels:
        return " · ".join((["지각"] if late else []) + labels), late
    return status, late


def _date_text(value):
    return value.isoformat() if isinstance(value, date) else value


def approved_half_day_map(leaves, start, end):
    """최종 승인·단일일·정확히 0.5일인 반차만 연결하고 나머지는 메타 경고로 보존한다."""
    linked, warnings = {}, []
    for row in leaves:
        kind = row.get("leave_type")
        if row.get("status") != "승인" or kind not in HALF_DAY_TYPES:
            continue
        try:
            if not row.get("start_date") or not row.get("end_date"):
                raise ValueError("반차 날짜가 없습니다")
            first = attendance_date(row.get("start_date"))
            last = attendance_date(row.get("end_date"))
        except (ValueError, TypeError):
            first = last = None
        try:
            days = Decimal(str(row.get("days")))
            valid = (first is not None and first == last and days.is_finite() and days == Decimal("0.5")
                     and isinstance(row.get("user_name"), str) and bool(row["user_name"]))
        except (ValueError, TypeError, InvalidOperation):
            valid = False
        if first is not None and last is not None and (first >= end or last < start):
            continue
        if not valid:
            warnings.append({key: _date_text(row.get(key)) for key in
                             ("id", "user_name", "leave_type", "start_date", "end_date")})
            continue
        key = (row["user_name"], first.isoformat())
        annotation = {"id": row.get("id"), "leave_type": kind}
        if annotation not in linked.setdefault(key, []):
            linked[key].append(annotation)
    for items in linked.values():
        items.sort(key=lambda item: (HALF_DAY_TYPES.index(item["leave_type"]), str(item["id"])))
    warnings.sort(key=lambda row: (str(row["user_name"]), str(row["start_date"]), str(row["id"])))
    return linked, warnings


def build_attendance_view(attendance, leaves, start, end):
    """원시 근태를 모두 보존한 상세와 월간 합계를 만든다. 누락 근태를 추정하지 않는다."""
    if not isinstance(start, date) or not isinstance(end, date) or start >= end:
        raise ValueError("근태 조회 기간이 올바르지 않습니다")
    linked, warnings = approved_half_day_map(leaves, start, end)
    records, groups, present = [], {}, set()

    def summary(name):
        return groups.setdefault(name, {"user_name": name, "days": 0, "normal": 0, "late": 0,
                                        "early": 0, "morning_half": 0, "afternoon_half": 0})

    for row in attendance:
        day = attendance_date(row["work_date"])
        if not start <= day < end:
            continue
        name = row["user_name"]
        key = (name, day.isoformat())
        present.add(key)
        half_days = [dict(item) for item in linked.get(key, [])]
        status, late = display_status(row.get("status"), row.get("clock_in"), half_days)
        record = {key: row.get(key) for key in ("id", "user_name", "clock_in", "clock_out", "note")}
        record.update(key=f"attendance:{row['id']}", work_date=day.isoformat(), raw_status=row.get("status"),
                      status=status, is_late=late, has_attendance=True, approved_half_days=half_days)
        records.append(record)
        total = summary(name)
        total["days"] += 1
        total["normal"] += int(not half_days and status == "정상")
        total["late"] += int(late)

    for (name, day), half_days in linked.items():
        total = summary(name)
        types = {item["leave_type"] for item in half_days}
        total["morning_half"] += int(bool(types & MORNING_TYPES))
        total["afternoon_half"] += int(bool(types & AFTERNOON_TYPES))
        if (name, day) not in present:
            status, late = display_status(None, None, half_days)
            records.append({"key": f"leave:{name}:{day}", "id": None, "user_name": name, "work_date": day,
                            "clock_in": None, "clock_out": None, "note": "", "raw_status": None,
                            "status": status, "is_late": late, "has_attendance": False,
                            "approved_half_days": [dict(item) for item in half_days]})
    records.sort(key=lambda row: (row["work_date"], row["user_name"], row["key"]))
    return {"rows": [groups[name] for name in sorted(groups)], "records": records, "halfday_warnings": warnings}


def _approved_vacation_map(leaves, start, end):
    """근무량을 추정하지 않고 유효한 최종 승인 휴가의 사실만 날짜에 연결한다."""
    half_days, invalid_half_days = approved_half_day_map(leaves, start, end)
    linked = {key: [{**item, "days": 0.5} for item in items] for key, items in half_days.items()}
    warnings = [{"code": "invalid_approved_leave", "user_name": row.get("user_name"), "id": row.get("id")}
                for row in invalid_half_days]
    for row in leaves:
        if row.get("status") != "승인" or row.get("leave_type") in HALF_DAY_TYPES:
            continue
        metadata = {"user_name": row.get("user_name"), "id": row.get("id")}
        if row.get("leave_type") not in FULL_LEAVE_TYPES:
            warnings.append({"code": "unrecognized_leave_type", **metadata})
            continue
        try:
            if not row.get("start_date") or not row.get("end_date"):
                raise ValueError("휴가 날짜가 없습니다")
            first, last = attendance_date(row["start_date"]), attendance_date(row["end_date"])
            days = Decimal(str(row.get("days")))
            if (first > last or not days.is_finite() or days <= 0
                    or not isinstance(row.get("user_name"), str) or not row["user_name"]):
                raise ValueError("휴가 기간이나 일수가 올바르지 않습니다")
        except (ValueError, TypeError, InvalidOperation):
            warnings.append({"code": "invalid_approved_leave", **metadata})
            continue
        first, last = max(first, start), min(last, end - timedelta(days=1))
        for offset in range(max(0, (last - first).days + 1)):
            key = (row["user_name"], (first + timedelta(days=offset)).isoformat())
            annotation = {"id": row.get("id"), "leave_type": row["leave_type"], "days": row["days"]}
            if annotation not in linked.setdefault(key, []):
                linked[key].append(annotation)
    for items in linked.values():
        items.sort(key=lambda item: (item["leave_type"], str(item["id"])))
    return linked, warnings


def add_business_day_coverage(records, leaves, roster, holidays, start, end, today=None):
    """상세만 보완한다. 입사·휴일·승인 근거가 없는 경우 적색 판정을 추정하지 않는다."""
    if not isinstance(start, date) or not isinstance(end, date) or not 0 < (end - start).days <= 366:
        raise ValueError("근태 보완 기간은 최대 1년이어야 합니다")
    as_of = attendance_date(today)
    holiday_dates = set()
    for value in holidays:
        try:
            if value:
                holiday_dates.add(attendance_date(value))
        except (ValueError, TypeError):
            continue
    known_years = {day.year for day in holiday_dates}
    linked, warnings = _approved_vacation_map(leaves, start, end)
    for year in range(start.year, (end - timedelta(days=1)).year + 1):
        if year not in known_years:
            warnings.append({"code": "missing_holiday_calendar", "year": year})

    # 이름으로 저장된 근태이므로 비활성·관리자 동명이인의 입사일도 함께 확인한다.
    accounts, candidates = {}, set()
    for person in roster:
        name = person.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            hire = attendance_date(person["hire_date"]) if person.get("hire_date") else None
        except (ValueError, TypeError):
            hire = None
        accounts.setdefault(name, set()).add(hire)
        if person.get("is_active") is True and person.get("role") in ("member", "pm"):
            candidates.add(name)
    eligible = {}
    for name in sorted(candidates):
        dates = accounts[name]
        if len(dates) > 1:
            warnings.append({"code": "conflicting_hire_dates", "user_name": name})
        elif None in dates:
            warnings.append({"code": "missing_hire_date", "user_name": name})
        else:
            eligible[name] = next(iter(dates))

    def business_day(day):
        return day.weekday() < 5 and day not in holiday_dates

    def red_day(name, day):
        return (name in eligible and eligible[name] <= day < as_of and day.year in known_years
                and business_day(day))

    def annotate(record, annotations):
        record["approved_leaves"] = [dict(item) for item in annotations]
        full_types = [kind for kind in FULL_LEAVE_TYPES if any(item["leave_type"] == kind for item in annotations)]
        if full_types:
            prefix = record.get("status", "") if record.get("approved_half_days") else ("지각" if record.get("is_late") else "")
            record["status"] = " · ".join(([prefix] if prefix else []) + [f"휴가({kind})" for kind in full_types])

    result, present = [], set()
    for original in records:
        record = deepcopy(original)
        day = attendance_date(record["work_date"])
        key = (record["user_name"], day.isoformat())
        present.add(key)
        annotations = linked.get(key, [])
        exempt = bool(annotations or record.get("approved_half_days"))
        can_mark = red_day(record["user_name"], day) and not exempt
        missing_in = bool(can_mark and not record.get("clock_in"))
        missing_out = bool(can_mark and not record.get("clock_out"))
        record.update(is_missing=missing_in or missing_out,
                      missing_clock_in=missing_in, missing_clock_out=missing_out)
        if missing_in or missing_out:
            label = "출퇴근 미기록" if missing_in and missing_out else ("출근 미기록" if missing_in else "퇴근 미기록")
            record["status"] = " · ".join(part for part in (record.get("status"), label) if part)
        annotate(record, annotations)
        result.append(record)

    # 승인 휴가만 있는 날짜도 사실대로 표시하되 근무일수·지각 합계에는 전달하지 않는다.
    for (name, day_text), annotations in linked.items():
        day = attendance_date(day_text)
        if (name, day_text) in present or not business_day(day):
            continue
        half_days = [{"id": item["id"], "leave_type": item["leave_type"]}
                     for item in annotations if item["leave_type"] in HALF_DAY_TYPES]
        status, late = display_status(None, None, half_days)
        record = {"key": f"leave:{name}:{day_text}", "id": None, "user_name": name, "work_date": day_text,
                  "clock_in": None, "clock_out": None, "note": "", "raw_status": None,
                  "status": status, "is_late": late, "has_attendance": False,
                  "approved_half_days": half_days, "is_missing": False,
                  "missing_clock_in": False, "missing_clock_out": False}
        annotate(record, annotations)
        result.append(record)
        present.add((name, day_text))

    for name, hire in eligible.items():
        first, last = max(start, hire), min(end, as_of)
        for offset in range(max(0, (last - first).days)):
            day = first + timedelta(days=offset)
            day_text = day.isoformat()
            if (name, day_text) in present or not red_day(name, day):
                continue
            result.append({"key": f"missing:{name}:{day_text}", "id": None, "user_name": name,
                           "work_date": day_text, "clock_in": None, "clock_out": None, "note": "",
                           "raw_status": None, "status": "출퇴근 미기록", "is_late": False,
                           "has_attendance": False, "approved_half_days": [], "approved_leaves": [],
                           "is_missing": True, "missing_clock_in": True, "missing_clock_out": True})
    result.sort(key=lambda row: (row["work_date"], row["user_name"], row["key"]))
    warnings = list({tuple(sorted(item.items())): item for item in warnings}.values())
    warnings.sort(key=lambda row: (row["code"], str(row.get("user_name", "")), str(row.get("year", "")), str(row.get("id", ""))))
    return {"records": result, "coverage_warnings": warnings}
