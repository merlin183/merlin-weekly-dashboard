"""영업일 보완의 조회 projection만 검증한다. 앱·DB·네트워크는 사용하지 않는다."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from attendance_policy import (HALF_DAY_TYPES, add_business_day_coverage, build_attendance_view,
                               month_bounds, period_bounds)


START, END, TODAY = date(2026, 11, 1), date(2026, 12, 1), date(2026, 11, 5)
HOLIDAYS = ("2026-01-01",)


def person(name="Morgan", hire="2026-01-01", role="pm", active=True, identifier="employee"):
    return {"id": identifier, "name": name, "hire_date": hire, "role": role, "is_active": active}


def attendance(identifier=1, day="2026-11-02", name="Morgan", **changes):
    return {"id": identifier, "user_name": name, "work_date": day, "status": "정상",
            "clock_in": f"{day} 09:20:00", "clock_out": f"{day} 18:00:00", "note": "기존 메모", **changes}


def leave(identifier="leave", day="2026-11-02", name="Morgan", kind="연차", **changes):
    return {"id": identifier, "user_name": name, "leave_type": kind, "start_date": day,
            "end_date": day, "days": Decimal("1"), "status": "승인", **changes}


class AttendanceCoverageTests(unittest.TestCase):
    def view(self, records=(), leaves=(), roster=None, holidays=HOLIDAYS, start=START, end=END, today=TODAY):
        original = build_attendance_view(records, leaves, start, end)
        before = deepcopy(original)
        result = add_business_day_coverage(original["records"], leaves, [person()] if roster is None else roster,
                                           holidays, start, end, today=today)
        self.assertEqual(original, before)
        self.assertNotIn("rows", result, "coverage cannot replace summary counts")
        return original, result

    def test_explicit_all_is_bounded_annual_while_empty_month_keeps_existing_kst_default(self):
        now = datetime(2026, 10, 31, 15, 10, tzinfo=timezone.utc)
        self.assertEqual(period_bounds("", "", now), (START, END, "month"))
        self.assertEqual(period_bounds("", "all", now), (date(2026, 1, 1), date(2027, 1, 1), "year"))
        self.assertEqual(period_bounds("2024", "all", now), (date(2024, 1, 1), date(2025, 1, 1), "year"))
        self.assertEqual(period_bounds("2026", "12", now), (date(2026, 12, 1), date(2027, 1, 1), "month"))
        for year, month in (("9999", "all"), ("9999", "11"), ("0000", "all"), ("2026.0", "all"),
                            ("2026", "ALL"), ("2026", "13"), ("2026", "0")):
            with self.subTest(year=year, month=month), self.assertRaises(ValueError):
                period_bounds(year, month)
        with self.assertRaises(ValueError):
            month_bounds("2026", "all")

    def test_only_past_weekdays_after_hire_excluding_registered_holidays_are_missing(self):
        _, result = self.view(roster=[person(hire="2026-11-02")], holidays=(*HOLIDAYS, "2026-11-03"))
        self.assertEqual([row["work_date"] for row in result["records"]], ["2026-11-02", "2026-11-04"])
        for row in result["records"]:
            self.assertEqual(row["key"], f"missing:Morgan:{row['work_date']}")
            self.assertIsNone(row["id"])
            self.assertIsNone(row["clock_in"])
            self.assertIsNone(row["clock_out"])
            self.assertIsNone(row["raw_status"])
            self.assertFalse(row["has_attendance"])
            self.assertFalse(row["is_late"])
            self.assertTrue(row["is_missing"] and row["missing_clock_in"] and row["missing_clock_out"])
        self.assertEqual(result["coverage_warnings"], [])

    def test_partial_punches_flag_only_empty_times_without_creating_duplicate_dates(self):
        records = [attendance(1, clock_out=None), attendance(2, "2026-11-03", clock_in=None),
                   attendance(3, "2026-11-04", clock_in=None, clock_out=None)]
        before = deepcopy(records)
        original, result = self.view(records)
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual([(row["missing_clock_in"], row["missing_clock_out"]) for row in result["records"]],
                         [(False, True), (True, False), (True, True)])
        self.assertEqual([row["status"] for row in result["records"]],
                         ["정상 · 퇴근 미기록", "정상 · 출근 미기록", "정상 · 출퇴근 미기록"])
        for row, raw in zip(result["records"], records):
            self.assertTrue(row["is_missing"])
            self.assertEqual(row["key"], f"attendance:{raw['id']}")
            self.assertEqual({key: row[key] for key in raw if key != "status"},
                             {key: raw[key] for key in raw if key != "status"})
            self.assertEqual(row["raw_status"], raw["status"])
        self.assertEqual(original["rows"][0]["days"], 3)
        self.assertEqual(records, before)

    def test_partial_missing_status_keeps_existing_lateness_and_raw_status(self):
        original, result = self.view([attendance(status="지각", clock_in="2026-11-02 13:10:00", clock_out=None)])
        row = next(row for row in result["records"] if row["has_attendance"])
        self.assertEqual(row["status"], "지각 · 퇴근 미기록")
        self.assertEqual(row["raw_status"], "지각")
        self.assertTrue(row["is_late"] and row["is_missing"] and row["missing_clock_out"])
        self.assertFalse(row["missing_clock_in"])
        self.assertEqual(original["rows"][0]["late"], 1)

    def test_today_future_pre_hire_weekend_and_holiday_real_rows_remain_without_red(self):
        dates = ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06")
        records = [attendance(index, day, clock_out=None) for index, day in enumerate(dates)]
        _, result = self.view(records, roster=[person(hire="2026-11-03")],
                              holidays=(*HOLIDAYS, "2026-11-03"))
        real = [row for row in result["records"] if row["has_attendance"]]
        self.assertEqual([row["work_date"] for row in real], list(dates))
        self.assertTrue(all(not row["is_missing"] and not row["missing_clock_in"] and not row["missing_clock_out"]
                            for row in real))

    def test_only_current_active_member_pm_with_known_actual_hire_can_create_missing(self):
        roster = [person(), person("직원", role="member"), person("대표", role="admin"),
                  person("퇴사자", active=False), person("테스트", hire=None, role="member"),
                  person("입사일오류", hire="bad"), person("미래입사", hire="2027-01-01")]
        _, result = self.view(roster=roster)
        self.assertEqual({row["user_name"] for row in result["records"]}, {"Morgan", "직원"})
        self.assertEqual({(item["code"], item.get("user_name")) for item in result["coverage_warnings"]},
                         {("missing_hire_date", "테스트"), ("missing_hire_date", "입사일오류")})

    def test_conflicting_hire_in_inactive_or_admin_namesake_suppresses_new_red(self):
        roster = [person(), person(hire="2026-02-01", role="admin", identifier="other-admin"),
                  person("Emery"), person("Emery", hire=None, active=False, identifier="other-inactive"),
                  person("동일입사"), person("동일입사", role="member", identifier="second-active"),
                  person("관리자만", hire=None, role="admin")]
        records = [attendance(clock_out=None)]
        _, result = self.view(records, roster=roster)
        self.assertEqual({row["user_name"] for row in result["records"] if row["is_missing"]}, {"동일입사"})
        self.assertEqual({(item["code"], item["user_name"]) for item in result["coverage_warnings"]},
                         {("conflicting_hire_dates", "Morgan"), ("conflicting_hire_dates", "Emery")})
        self.assertFalse(next(row for row in result["records"] if row["has_attendance"])["missing_clock_out"])
        self.assertEqual(len(result["records"]), 4)

    def test_unknown_holiday_year_defers_missing_but_keeps_real_and_approved_rows(self):
        original, result = self.view([attendance(clock_out=None)], [leave("vacation", "2026-11-03")], holidays=())
        self.assertEqual(len(result["records"]), 2)
        self.assertTrue(all(not row["is_missing"] and not row["missing_clock_in"] and not row["missing_clock_out"]
                            for row in result["records"]))
        self.assertIn({"code": "missing_holiday_calendar", "year": 2026}, result["coverage_warnings"])
        self.assertEqual(original["rows"][0]["days"], 1)

    def test_full_leave_types_and_half_quantity_keep_original_type_and_amount_neutral(self):
        for kind in ("연차", "공가", "경조사", "무급", "경조"):
            with self.subTest(kind=kind):
                leaves = [leave(kind=kind, days=Decimal("0.5"))]
                before = deepcopy(leaves)
                _, result = self.view(leaves=leaves)
                row = next(row for row in result["records"] if row["work_date"] == "2026-11-02")
                self.assertEqual(row["status"], f"휴가({kind})")
                self.assertEqual(row["key"], "leave:Morgan:2026-11-02")
                self.assertEqual(row["approved_leaves"], [{"id": "leave", "leave_type": kind, "days": Decimal("0.5")}])
                self.assertFalse(row["is_missing"] or row["missing_clock_in"] or row["missing_clock_out"])
                self.assertEqual(leaves, before)

    def test_full_leave_on_existing_late_row_exempts_punch_red_without_rewriting_late_or_summary(self):
        records = [attendance(status="지각", clock_in="2026-11-02 10:20:00", clock_out=None)]
        original, result = self.view(records, [leave()])
        row = next(row for row in result["records"] if row["has_attendance"])
        self.assertEqual(row["status"], "지각 · 휴가(연차)")
        self.assertTrue(row["is_late"])
        self.assertEqual(row["raw_status"], "지각")
        self.assertFalse(row["is_missing"] or row["missing_clock_in"] or row["missing_clock_out"])
        self.assertEqual(original["rows"][0]["late"], 1)

    def test_four_valid_half_days_preserve_existing_projection_and_exempt_all_punch_flags(self):
        for kind in HALF_DAY_TYPES:
            with self.subTest(kind=kind):
                leaves = [leave(kind=kind, days=Decimal("0.5"))]
                original, result = self.view([attendance(clock_in=None, clock_out=None)], leaves)
                row = next(row for row in result["records"] if row["has_attendance"])
                self.assertEqual(row["status"], original["records"][0]["status"])
                self.assertEqual(row["approved_half_days"], original["records"][0]["approved_half_days"])
                self.assertFalse(row["is_missing"] or row["missing_clock_in"] or row["missing_clock_out"])
                original, result = self.view(leaves=leaves)
                row = next(row for row in result["records"] if row["work_date"] == "2026-11-02")
                self.assertEqual(row["key"], original["records"][0]["key"])
                self.assertEqual(row["approved_half_days"], original["records"][0]["approved_half_days"])

    def test_pending_rejected_unknown_and_malformed_leave_do_not_infer_vacation(self):
        leaves = [leave("pending", status="대기"), leave("rejected", status="반려"),
                  leave("absence", kind="결근"), leave("ambiguous-half", kind="반차"),
                  leave("unknown", kind="새 유형"), leave("bad-half", kind="오전반차", days=1),
                  leave("bad-date", start_date="bad"), leave("reversed", end_date="2026-11-01")]
        # 잘못된 반차는 기존 helper도 경고만 반환한다. 종일의 원문 오류는 새 helper에서 확인한다.
        _, result = self.view(leaves=leaves)
        row = next(row for row in result["records"] if row["work_date"] == "2026-11-02")
        self.assertTrue(row["is_missing"])
        self.assertEqual(row["approved_leaves"], [])
        self.assertEqual({item["id"] for item in result["coverage_warnings"]},
                         {"absence", "ambiguous-half", "unknown", "bad-half", "bad-date", "reversed"})
        self.assertTrue(all(set(item) <= {"code", "user_name", "id"} for item in result["coverage_warnings"]))

    def test_nonfinite_zero_or_negative_full_leave_quantity_warns_without_editing_original(self):
        for amount in (Decimal("NaN"), Decimal("Infinity"), 0, -1, None):
            with self.subTest(amount=str(amount)):
                leaves = [leave(days=amount, reason="공개하지 않는 사유", email="private@example.test")]
                before = deepcopy(leaves)
                _, result = self.view(leaves=leaves)
                self.assertTrue(next(row for row in result["records"] if row["work_date"] == "2026-11-02")["is_missing"])
                self.assertEqual(result["coverage_warnings"], [{"code": "invalid_approved_leave", "user_name": "Morgan", "id": "leave"}])
                self.assertEqual(repr(leaves), repr(before))

    def test_cross_year_full_leave_uses_overlap_and_clips_synthetic_business_days_to_requested_year(self):
        start, end = date(2027, 1, 1), date(2028, 1, 1)
        leaves = [leave(day="2026-12-31", end_date="2027-01-04", days=Decimal("2"))]
        _, result = self.view(leaves=leaves, start=start, end=end, today=date(2027, 1, 5),
                              holidays=("2027-01-01",))
        self.assertEqual([row["work_date"] for row in result["records"]], ["2027-01-04"])
        self.assertEqual(result["records"][0]["status"], "휴가(연차)")

    def test_future_approved_rows_and_actual_pre_hire_rows_are_preserved(self):
        records = [attendance(1, "2026-11-02"), attendance(2, "2026-11-06", clock_out=None)]
        original, result = self.view(records, [leave(day="2026-11-09")], roster=[person(hire="2026-11-10")])
        self.assertEqual([row["work_date"] for row in result["records"]], ["2026-11-02", "2026-11-06", "2026-11-09"])
        self.assertTrue(all(not row["is_missing"] and not row["missing_clock_out"] for row in result["records"]))
        self.assertEqual(original["rows"][0]["days"], 2)

    def test_duplicate_attendance_rows_and_approved_half_day_keys_are_never_deduplicated(self):
        records = [attendance(1), attendance(2)]
        leaves = [leave("half", "2026-11-03", kind="오전반차", days=0.5)]
        original, result = self.view(records, leaves)
        keys = [row["key"] for row in result["records"]]
        self.assertIn("attendance:1", keys)
        self.assertIn("attendance:2", keys)
        self.assertIn("leave:Morgan:2026-11-03", keys)
        self.assertEqual(sum(row["work_date"] == "2026-11-02" for row in result["records"]), 2)
        self.assertEqual(original["rows"][0]["days"], 2)
        self.assertEqual(original["rows"][0]["morning_half"], 1)

    def test_leap_year_bulk_projection_is_bounded_complete_and_stably_sorted(self):
        start, end, _ = period_bounds("2024", "all")
        _, result = self.view(roster=[person(hire="2024-01-01")], start=start, end=end,
                              holidays=("2024-01-01",), today=date(2026, 11, 5))
        expected = [start + timedelta(days=offset) for offset in range(366)
                    if (start + timedelta(days=offset)).weekday() < 5 and offset != 0]
        self.assertEqual([row["work_date"] for row in result["records"]], [day.isoformat() for day in expected])
        self.assertIn("missing:Morgan:2024-02-29", [row["key"] for row in result["records"]])
        self.assertEqual(len({row["key"] for row in result["records"]}), len(expected))
        with self.assertRaises(ValueError):
            add_business_day_coverage([], [], [], [], start, date(2026, 1, 1), today=TODAY)


if __name__ == "__main__":
    unittest.main()
