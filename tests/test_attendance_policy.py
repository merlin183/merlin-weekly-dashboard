"""앱·DB import 없이 실행하는 근태 표시 정책 회귀."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from attendance_policy import (HALF_DAY_TYPES, arrival_status, attendance_date,
                               build_attendance_view, month_bounds)


START, END = date(2026, 11, 1), date(2026, 12, 1)


def attendance(identifier=1, name="Morgan", day="2026-11-05", status="정상", clock_in="2026-11-05 09:20:01"):
    return {"id": identifier, "user_name": name, "work_date": day, "clock_in": clock_in,
            "clock_out": "2026-11-05 14:03:02", "status": status, "note": "기존 메모 원문"}


def leave(identifier="approved-1", name="Morgan", kind="오전반차", day="2026-11-05", **changes):
    return {"id": identifier, "user_name": name, "leave_type": kind, "start_date": day,
            "end_date": day, "days": Decimal("0.5"), "status": "승인", **changes}


class AttendancePolicyTests(unittest.TestCase):
    def view(self, records=(), leaves=()):
        return build_attendance_view(records, leaves, START, END)

    def test_kst_defaults_leap_year_and_month_boundaries(self):
        now = datetime(2026, 10, 31, 15, 10, tzinfo=timezone.utc)
        self.assertEqual(month_bounds(now=now), (START, END))
        self.assertEqual(attendance_date(now=now), START)
        self.assertEqual(month_bounds("2026", "12"), (date(2026, 12, 1), date(2027, 1, 1)))
        self.assertEqual(month_bounds("2024", "2"), (date(2024, 2, 1), date(2024, 3, 1)))
        self.assertEqual(attendance_date("2024-02-29"), date(2024, 2, 29))

    def test_invalid_year_month_and_dates_are_rejected(self):
        for year, month in (("abc", "1"), ("2026", "0"), ("2026", "13"), ("2026", "1 OR 1=1"),
                            ("0000", "1"), ("9999", "12"), ("2026.0", "1")):
            with self.subTest(year=year, month=month), self.assertRaises(ValueError):
                month_bounds(year, month)
        for value in ("2026-02-29", "20261105", "2026-11-00", "2026-11-05T00:00:00", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                attendance_date(value)

    def test_legacy_early_is_a_read_only_projection_from_original_arrival_time(self):
        source = [attendance(status="조퇴"), attendance(2, status="조퇴", clock_in="2026-11-05 09:31:00")]
        before = deepcopy(source)
        result = self.view(source)
        self.assertEqual([row["status"] for row in result["records"]], ["정상", "지각"])
        self.assertEqual([row["raw_status"] for row in result["records"]], ["조퇴", "조퇴"])
        self.assertEqual(result["rows"][0], {"user_name": "Morgan", "days": 2, "normal": 1, "late": 1,
                                           "early": 0, "morning_half": 0, "afternoon_half": 0})
        self.assertEqual(source, before)
        self.assertEqual(result["records"][0]["clock_out"], before[0]["clock_out"])

    def test_early_without_a_time_is_neutral_and_existing_minute_threshold_is_preserved(self):
        result = self.view([attendance(status="조퇴", clock_in=None)])
        self.assertEqual(result["records"][0]["status"], "확인 필요")
        self.assertFalse(result["records"][0]["is_late"])
        self.assertEqual(arrival_status("2026-11-05 09:30:59"), ("정상", False))
        self.assertEqual(arrival_status("2026-11-05T00:31:00Z"), ("지각", True))
        self.assertEqual(arrival_status("2026-11-04T23:59:00Z"), ("정상", False))

    def test_all_four_paid_and_unpaid_types_keep_original_type_labels(self):
        for kind in HALF_DAY_TYPES:
            with self.subTest(kind=kind):
                result = self.view([attendance()], [leave(kind=kind)])
                self.assertEqual(result["records"][0]["status"], kind)
                self.assertEqual(result["records"][0]["approved_half_days"], [{"id": "approved-1", "leave_type": kind}])

    def test_morning_has_no_guessed_late_rule_but_afternoon_preserves_existing_lateness(self):
        source = [attendance(status="지각", clock_in="2026-11-05 16:40:00")]
        morning = self.view(source, [leave()])
        self.assertEqual(morning["records"][0]["status"], "오전반차")
        self.assertFalse(morning["records"][0]["is_late"])
        self.assertEqual(morning["rows"][0]["late"], 0)
        afternoon = self.view(source, [leave(kind="오후반차")])
        self.assertEqual(afternoon["records"][0]["status"], "지각 · 오후반차")
        self.assertTrue(afternoon["records"][0]["is_late"])
        self.assertEqual(afternoon["rows"][0]["late"], 1)

    def test_only_final_overall_approval_is_used_and_full_leave_is_not_a_half_day(self):
        leaves = [leave(status="대기", pm_status="승인"), leave("rejected", status="반려"),
                  leave("full", kind="연차"), leave("old", kind="반차")]
        result = self.view([attendance(status="지각")], leaves)
        self.assertEqual(result["records"][0]["approved_half_days"], [])
        self.assertEqual(result["records"][0]["status"], "지각")
        self.assertEqual(result["halfday_warnings"], [])

    def test_malformed_and_multi_day_half_days_only_produce_minimal_warnings(self):
        leaves = [leave("multi", end_date="2026-11-06"), leave("whole", days=1), leave("zero", days=0),
                  leave("missing", start_date=None), leave("nan", days=Decimal("NaN"))]
        before = deepcopy(leaves)
        result = self.view([], leaves)
        self.assertEqual(result["records"], [])
        self.assertEqual(len(result["halfday_warnings"]), 5)
        for warning in result["halfday_warnings"]:
            self.assertEqual(set(warning), {"id", "user_name", "leave_type", "start_date", "end_date"})
        self.assertEqual([row["id"] for row in leaves], [row["id"] for row in before])
        self.assertEqual(leaves[0], before[0])

    def test_previous_month_overlap_is_warned_and_non_overlapping_leave_is_ignored(self):
        leaves = [leave("cross", day="2026-10-31", end_date="2026-11-01"),
                  leave("before", day="2026-10-31"), leave("after", day="2026-12-01")]
        result = self.view([], leaves)
        self.assertEqual([row["id"] for row in result["halfday_warnings"]], ["cross"])
        self.assertEqual(result["records"], [])

    def test_approved_leave_without_attendance_creates_only_one_neutral_time_row(self):
        result = self.view([], [leave()])
        record = result["records"][0]
        self.assertEqual(record["key"], "leave:Morgan:2026-11-05")
        self.assertIsNone(record["id"])
        self.assertIsNone(record["clock_in"])
        self.assertIsNone(record["clock_out"])
        self.assertFalse(record["has_attendance"])
        self.assertEqual(result["rows"][0]["days"], 0)
        self.assertEqual(result["rows"][0]["morning_half"], 1)
        self.assertEqual(self.view()["records"], [])

    def test_duplicate_attendance_ids_are_preserved_and_half_days_are_counted_once(self):
        records = [attendance(20), attendance(10)]
        leaves = [leave("paid"), leave("duplicate"), leave("unpaid", kind="무급오전반차"),
                  leave("pm", kind="오후반차")]
        result = self.view(records, leaves)
        self.assertEqual({row["id"] for row in result["records"]}, {10, 20})
        self.assertEqual(len({row["key"] for row in result["records"]}), 2)
        self.assertEqual(result["rows"][0], {"user_name": "Morgan", "days": 2, "normal": 0, "late": 0,
                                           "early": 0, "morning_half": 1, "afternoon_half": 1})
        self.assertEqual(len(result["records"][0]["approved_half_days"]), 4)

    def test_exact_name_date_matching_and_stable_order_preserve_all_original_values(self):
        records = [attendance(2, name="Emery"), attendance(1)]
        leaves = [leave("other", name="Morgan "), leave("next", day="2026-11-06"), leave("self")]
        original = deepcopy((records, leaves))
        result = self.view(records, leaves)
        self.assertEqual(result, self.view(list(reversed(records)), list(reversed(leaves))))
        self.assertEqual((records, leaves), original)
        self.assertEqual(next(row for row in result["records"] if row["id"] == 2)["approved_half_days"], [])
        self.assertEqual(next(row for row in result["records"] if row["id"] == 1)["approved_half_days"],
                         [{"id": "self", "leave_type": "오전반차"}])

    def test_morning_approval_cancellation_restores_late_display_from_unchanged_raw_time_and_status(self):
        now = datetime(2026, 11, 5, 13, 10)
        raw_status = arrival_status(now)[0]
        self.assertEqual(raw_status, "지각")
        source = [attendance(status=raw_status, clock_in=now.isoformat(" "))]
        original = deepcopy(source)
        approved = self.view(source, [leave()])
        self.assertEqual(approved["records"][0]["status"], "오전반차")
        self.assertFalse(approved["records"][0]["is_late"])
        self.assertEqual(approved["rows"][0]["late"], 0)
        for leaves in ([], [leave(status="반려")], [leave(status="대기", pm_status="승인")]):
            result = self.view(source, leaves)
            self.assertEqual(result["records"][0]["status"], "지각")
            self.assertTrue(result["records"][0]["is_late"])
            self.assertEqual(result["rows"][0]["late"], 1)
            self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
