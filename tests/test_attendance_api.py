"""실제 main.py 함수 AST만 추출해 가짜 cursor로 실행한다. 앱/DB import·네트워크는 없다."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, date
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import attendance_policy as policy


SOURCE = Path(__file__).resolve().parent.parent / "src/main.py"


class FakeResponse:
    def __init__(self, content, status_code, media_type):
        self.status_code = status_code
        self.body = content.encode()


class FakeRequest:
    def __init__(self, user, body=None):
        self.state = SimpleNamespace(user=user)
        self.body = body or {}

    async def json(self):
        return self.body


class FakeCursor:
    attendance_columns = ("id", "user_name", "work_date", "clock_in", "clock_out", "status", "note")
    leave_columns = ("id", "user_name", "leave_type", "start_date", "end_date", "days", "status")

    def __init__(self, attendance=(), leaves=(), roster=(), holidays=()):
        self.attendance, self.leaves = deepcopy(attendance), deepcopy(leaves)
        self.roster, self.holidays = deepcopy(roster), deepcopy(holidays)
        self.queries, self.results, self.description = [], [], []

    def execute(self, sql, parameters=()):
        self.queries.append((sql, parameters))
        if sql.startswith("SELECT") and "FROM ext_attendance" in sql:
            if sql.startswith("SELECT id FROM") or sql.startswith("SELECT id, status,"):
                rows = [row for row in self.attendance if row["user_name"] == parameters[0]
                        and row["work_date"] == parameters[1]]
                columns = ("id",) if sql.startswith("SELECT id FROM") else ("id", "status", "clock_in", "note")
            else:
                rows = [row for row in self.attendance if parameters[0] <= row["work_date"] < parameters[1]]
                if "AND user_name = %s" in sql:
                    rows = [row for row in rows if row["user_name"] == parameters[2]]
                columns = self.attendance_columns
            self.description = [(name,) for name in columns]
            self.results = [tuple(row.get(name) for name in columns) for row in rows]
        elif sql.startswith("SELECT") and "FROM ext_leave_requests" in sql:
            half_only = "leave_type = ANY" in sql
            end_index, start_index = (2, 3) if half_only else (1, 2)
            rows = [row for row in self.leaves if row["status"] == parameters[0]
                    and (not half_only or row["leave_type"] in parameters[1])
                    and row["start_date"] < parameters[end_index] and row["end_date"] >= parameters[start_index]]
            if "AND user_name = %s" in sql:
                rows = [row for row in rows if row["user_name"] == parameters[-1]]
            self.description = [(name,) for name in self.leave_columns]
            self.results = [tuple(row.get(name) for name in self.leave_columns) for row in rows]
        elif sql.startswith("SELECT") and "FROM ext_holidays" in sql:
            self.description = [("date",)]
            self.results = [(day,) for day in self.holidays if parameters[0] <= day < parameters[1]]
        elif sql.startswith("SELECT") and "FROM ext_users" in sql:
            columns = ("id", "name", "role", "is_active", "hire_date")
            rows = self.roster
            if "WHERE name = %s" in sql:
                rows = [row for row in rows if row["name"] == parameters[0]]
            self.description = [(name,) for name in columns]
            self.results = [tuple(row.get(name) for name in columns) for row in rows]
        elif sql.startswith("SELECT COALESCE(SUM(used_days)"):
            self.results = [(0,)]
        else:
            self.results = []

    def fetchall(self):
        return self.results

    def fetchone(self):
        return self.results[0] if self.results else None


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor, self.commits, self.closed = cursor, 0, False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 5, 13, 10, tzinfo=policy.KST)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def actor(role="member", name="Morgan", identifier="personal"):
    return {"role": role, "name": name, "user_id": identifier}


def record(identifier=1, name="Morgan", status="조퇴", clock_in="2026-11-05 09:20:00"):
    return {"id": identifier, "user_name": name, "work_date": "2026-11-05", "clock_in": clock_in,
            "clock_out": "2026-11-05 14:20:00", "status": status, "note": "원문"}


def approved(identifier="half", name="Morgan", kind="오전반차", **changes):
    return {"id": identifier, "user_name": name, "leave_type": kind, "start_date": "2026-11-05",
            "end_date": "2026-11-05", "status": "승인", "days": 0.5, **changes}


def employee(name="Morgan", **changes):
    return {"id": "employee", "name": name, "role": "pm", "is_active": True,
            "hire_date": "2026-11-02", **changes}


class AttendanceApiTests(unittest.TestCase):
    def environment(self, attendance=(), leaves=(), roster=(), holidays=("2026-01-01",)):
        cursor = FakeCursor(attendance, leaves, roster, holidays)
        connection = FakeConnection(cursor)
        opened = []
        def connect():
            opened.append(True)
            return connection
        namespace = {"Request": object, "Response": FakeResponse, "json": json,
                     "datetime": FixedClock, "dt_date": date, "timedelta": timedelta, "KST": policy.KST,
                     "HALF_DAY_TYPES": policy.HALF_DAY_TYPES, "_attendance_date": policy.attendance_date,
                     "_attendance_month_bounds": policy.month_bounds, "_build_attendance_view": policy.build_attendance_view,
                     "_attendance_arrival_status": policy.arrival_status, "_pg_conn": connect,
                     "_attendance_period_bounds": policy.period_bounds,
                     "_add_attendance_coverage": policy.add_business_day_coverage}
        targets = {"_attendance_scope", "_attendance_can_read_halfdays", "_read_attendance_records", "_read_attendance_halfdays",
                   "get_attendance", "get_attendance_monthly", "clock_in", "clock_out", "update_attendance",
                   "_read_attendance_approved_leaves", "_read_attendance_holidays", "_read_attendance_roster"}
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
                node.decorator_list = []
                exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                             "<actual-attendance-function>", "exec"), namespace)
        return namespace, cursor, connection, opened

    def test_monthly_member_scope_covers_both_queries_and_read_does_not_commit(self):
        originals = [record(), record(2, "Emery")]
        leaves = [approved(), approved("other", "Emery")]
        env, cursor, connection, _ = self.environment(originals, leaves)
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", "11"))
        self.assertEqual({row["user_name"] for row in result["records"]}, {"Morgan"})
        self.assertEqual(result["month_start"], "2026-11-01")
        self.assertEqual(result["month_end"], "2026-12-01")
        self.assertEqual(result["timezone"], "Asia/Seoul")
        self.assertEqual(len(cursor.queries), 4)
        for sql, values in cursor.queries:
            if "FROM ext_holidays" in sql:
                self.assertEqual(values, ("2026-01-01", "2027-01-01"))
            elif "FROM ext_users" in sql:
                self.assertIn("WHERE name = %s", sql)
                self.assertEqual(values, ("Morgan",))
            else:
                self.assertIn("AND user_name = %s", sql)
                self.assertEqual(values[-1], "Morgan")
        self.assertEqual(connection.commits, 0)
        self.assertTrue(connection.closed)
        self.assertEqual(cursor.attendance, originals)
        self.assertEqual(cursor.leaves, leaves)

    def test_admin_and_pm_can_read_all_but_approval_query_uses_final_status_and_overlap(self):
        for role in ("admin", "pm"):
            env, cursor, _, _ = self.environment([record(), record(2, "Emery")], [approved()])
            result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor(role)), "2026", "11"))
            self.assertEqual({row["user_name"] for row in result["records"]}, {"Morgan", "Emery"})
            sql, values = cursor.queries[1]
            self.assertIn("start_date < %s AND end_date >= %s", sql)
            self.assertNotIn("pm_status", sql)
            self.assertNotIn("AND user_name = %s", sql)
            self.assertNotIn("leave_type = ANY", sql)
            self.assertEqual(values, ("승인", "2026-12-01", "2026-11-01"))

    def test_legacy_preserves_self_attendance_scope_and_never_reads_leave_details(self):
        env, cursor, _, _ = self.environment([record(name="공용")], [approved(name="공용")])
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor("member", "공용", "legacy")), "2026", "11"))
        self.assertEqual(len(cursor.queries), 1)
        self.assertEqual(cursor.queries[0][1][-1], "공용")
        self.assertEqual(result["records"][0]["approved_half_days"], [])

    def test_annual_coverage_is_four_bulk_scoped_reads_and_preserves_original_summary_and_rows(self):
        attendance = [record(), {**record(2), "work_date": "2026-11-04",
                                "clock_in": "2026-11-04 09:20:00", "clock_out": None}, record(3, "Emery")]
        leaves = [approved(kind="연차", start_date="2026-11-03", end_date="2026-11-03"),
                  approved("other", "Emery", kind="공가")]
        roster = [employee(), employee("Emery", hire_date=None)]
        originals = deepcopy((attendance, leaves, roster))
        env, cursor, connection, _ = self.environment(attendance, leaves, roster)
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", "all"))
        self.assertEqual(result["month"], "all")
        self.assertEqual(result["period_type"], "year")
        self.assertEqual((result["month_start"], result["month_end"]), ("2026-01-01", "2027-01-01"))
        self.assertEqual((result["period_start"], result["period_end"], result["as_of"]),
                         ("2026-01-01", "2027-01-01", "2026-11-05"))
        self.assertEqual({row["user_name"] for row in result["records"]}, {"Morgan"})
        self.assertEqual(len(cursor.queries), 4)
        self.assertEqual(cursor.queries[0][1], ("2026-01-01", "2027-01-01", "Morgan"))
        self.assertEqual(cursor.queries[1][1], ("승인", "2027-01-01", "2026-01-01", "Morgan"))
        self.assertEqual(cursor.queries[2][1], ("2026-01-01", "2027-01-01"))
        self.assertEqual(cursor.queries[3][1], ("Morgan",))
        self.assertTrue(all(sql.startswith("SELECT") for sql, values in cursor.queries))
        baseline = policy.build_attendance_view(attendance[:2], leaves[:1], date(2026, 1, 1), date(2027, 1, 1))
        self.assertEqual(result["rows"], baseline["rows"])
        self.assertEqual([row["work_date"] for row in result["records"] if row["is_missing"]], ["2026-11-02", "2026-11-04"])
        self.assertEqual(next(row for row in result["records"] if row["id"] == 2)["clock_out"], None)
        self.assertTrue(next(row for row in result["records"] if row["id"] == 2)["missing_clock_out"])
        self.assertEqual(next(row for row in result["records"] if row["id"] == 2)["status"], "정상 · 퇴근 미기록")
        self.assertFalse(next(row for row in result["records"] if row["id"] == 1)["missing_clock_out"])
        self.assertEqual(result["coverage_warnings"], [])
        self.assertEqual((cursor.attendance, cursor.leaves, cursor.roster), originals)
        self.assertEqual(connection.commits, 0)
        self.assertTrue(connection.closed)

    def test_empty_month_stays_current_month_and_reads_full_year_holidays_once(self):
        env, cursor, connection, _ = self.environment(roster=[employee()])
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", ""))
        self.assertEqual((result["month"], result["period_type"]), ("11", "month"))
        self.assertEqual((result["period_start"], result["period_end"]), ("2026-11-01", "2026-12-01"))
        self.assertEqual(cursor.queries[2][1], ("2026-01-01", "2027-01-01"))
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["coverage_warnings"], [])
        self.assertEqual(connection.commits, 0)

    def test_roster_read_keeps_inactive_namesake_for_conflict_without_other_member_metadata_leak(self):
        roster = [employee(), employee(id="inactive-namesake", is_active=False, hire_date="2026-02-01"),
                  employee("다른 직원", hire_date=None)]
        env, cursor, connection, _ = self.environment([record()], roster=roster)
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", "11"))
        roster_sql, parameters = cursor.queries[3]
        self.assertIn("WHERE name = %s", roster_sql)
        self.assertNotIn("is_active = TRUE", roster_sql)
        self.assertNotIn("role IN", roster_sql)
        self.assertEqual(parameters, ("Morgan",))
        self.assertEqual(result["coverage_warnings"], [{"code": "conflicting_hire_dates", "user_name": "Morgan"}])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(connection.commits, 0)

    def test_annual_legacy_never_reads_holidays_roster_or_leave_and_has_no_new_private_metadata(self):
        env, cursor, connection, _ = self.environment([record(name="공용")], [approved(name="공용")],
                                                     [employee("공용")])
        result = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor("member", "공용", "legacy")), "2026", "all"))
        self.assertEqual(len(cursor.queries), 1)
        self.assertEqual(result["coverage_warnings"], [])
        self.assertEqual(result["records"][0]["approved_leaves"], [])
        self.assertFalse(result["records"][0]["is_missing"])
        self.assertFalse(result["records"][0]["missing_clock_in"])
        self.assertFalse(result["records"][0]["missing_clock_out"])
        self.assertEqual(connection.commits, 0)

    def test_coverage_query_failure_closes_connection_without_writes(self):
        for target in ("FROM ext_holidays", "FROM ext_users"):
            env, cursor, connection, _ = self.environment([record()], roster=[employee()])
            execute = cursor.execute
            def failing(sql, parameters=()):
                if target in sql:
                    raise RuntimeError("fake coverage read failed")
                return execute(sql, parameters)
            cursor.execute = failing
            with self.assertRaisesRegex(RuntimeError, "fake coverage read failed"):
                asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", "all"))
            self.assertTrue(connection.closed)
            self.assertEqual(connection.commits, 0)

    def test_invalid_month_or_date_returns_400_before_database_access(self):
        for year, month in (("2026", "13"), ("wrong", "1"), ("9999", "12"),
                            ("9999", "all"), ("9999", "11"), ("0000", "all"), ("wrong", "all"), ("2026", "ALL")):
            env, _, _, opened = self.environment()
            response = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), year, month))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(opened, [])
        env, _, _, opened = self.environment()
        response = asyncio.run(env["get_attendance"](FakeRequest(actor()), "2026-02-29", "Emery"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(opened, [])

    def test_daily_member_cannot_request_another_user_and_synthetic_rows_are_monthly_only(self):
        env, cursor, connection, _ = self.environment([record()], [approved(), approved("no-clock", "Emery")])
        result = asyncio.run(env["get_attendance"](FakeRequest(actor()), "2026-11-05", "Emery"))
        self.assertEqual(result["rows"][0]["user_name"], "Morgan")
        self.assertEqual(result["rows"][0]["status"], "오전반차")
        self.assertEqual(result["rows"][0]["raw_status"], "조퇴")
        self.assertTrue(all(row["has_attendance"] for row in result["rows"]))
        self.assertTrue(all(parameters[-1] == "Morgan" for _, parameters in cursor.queries))
        self.assertEqual(connection.commits, 0)
        env, _, _, _ = self.environment([], [approved()])
        result = asyncio.run(env["get_attendance"](FakeRequest(actor()), "2026-11-05"))
        self.assertEqual(result["rows"], [])

    def test_clock_in_uses_jwt_actor_and_approved_morning_without_changing_leave(self):
        leaves = [approved()]
        env, cursor, connection, _ = self.environment([], leaves)
        result = asyncio.run(env["clock_in"](FakeRequest(actor(), {"user": "Emery"})))
        insert = next((sql, values) for sql, values in cursor.queries if sql.startswith("INSERT"))
        self.assertEqual(insert[1][0], "Morgan")
        self.assertEqual(insert[1][-1], "지각")
        self.assertEqual(result["status"], "오전반차")
        self.assertEqual(result["raw_status"], "지각")
        self.assertEqual(connection.commits, 1)
        self.assertEqual(cursor.leaves, leaves)
        self.assertTrue(all("UPDATE ext_leave" not in sql for sql, _ in cursor.queries))

    def test_clock_in_raw_lateness_survives_approval_cancellation_in_daily_and_monthly_views(self):
        env, cursor, _, _ = self.environment([], [approved()])
        result = asyncio.run(env["clock_in"](FakeRequest(actor())))
        values = next(values for sql, values in cursor.queries if sql.startswith("INSERT"))
        stored = record(status=values[-1], clock_in=values[2].isoformat(" "))
        self.assertEqual(stored["status"], "지각")
        self.assertEqual(result["status"], "오전반차")
        for leaves in ([], [approved(status="반려")]):
            env, _, connection, _ = self.environment([stored], leaves)
            daily = asyncio.run(env["get_attendance"](FakeRequest(actor()), "2026-11-05"))
            self.assertEqual(daily["rows"][0]["status"], "지각")
            self.assertEqual(daily["rows"][0]["clock_in"], stored["clock_in"])
            monthly = asyncio.run(env["get_attendance_monthly"](FakeRequest(actor()), "2026", "11"))
            self.assertEqual(monthly["rows"][0]["late"], 1)
            self.assertEqual(monthly["records"][0]["raw_status"], stored["status"])
            self.assertEqual(connection.commits, 0)

    def test_clock_out_only_changes_the_actor_clock_out_never_status_or_approval(self):
        original = record(status="조퇴", clock_in="2026-11-05 09:31:00")
        env, cursor, connection, _ = self.environment([original])
        result = asyncio.run(env["clock_out"](FakeRequest(actor(), {"user": "Emery"})))
        updates = [(sql, values) for sql, values in cursor.queries if sql.startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "UPDATE ext_attendance SET clock_out = %s WHERE id = %s AND user_name = %s")
        self.assertEqual(updates[0][1][1:], (1, "Morgan"))
        self.assertEqual(result["status"], "지각")
        self.assertEqual(result["raw_status"], "조퇴")
        self.assertEqual(cursor.attendance, [original])
        self.assertEqual(connection.commits, 1)

    def test_legacy_clock_mutations_and_member_manual_edit_are_denied_before_database_access(self):
        for name in ("clock_in", "clock_out"):
            for identifier in ("legacy", "", None):
                env, _, _, opened = self.environment()
                response = asyncio.run(env[name](FakeRequest(actor(identifier=identifier))))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(opened, [])
        env, _, _, opened = self.environment()
        response = asyncio.run(env["update_attendance"](1, FakeRequest(actor(), {"status": "정상"})))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(opened, [])

    def test_manual_early_or_derived_half_day_status_is_rejected_and_raw_times_pass_unchanged(self):
        for status in ("조퇴", "오전반차", "무급오후반차", "invalid", None):
            env, _, _, opened = self.environment()
            response = asyncio.run(env["update_attendance"](1, FakeRequest(actor("pm"), {"status": status})))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(opened, [])
        env, cursor, connection, _ = self.environment()
        timestamp = "2026-11-05T09:29:45"
        result = asyncio.run(env["update_attendance"](1, FakeRequest(actor("pm"), {"clock_in": timestamp, "status": "정상"})))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(cursor.queries[0][1], [timestamp, "정상", 1])
        self.assertEqual(connection.commits, 1)

    def test_kpi_uses_the_same_normalized_late_count_without_writes(self):
        records = [record(), record(2, "Emery", "지각", "2026-11-05 13:00:00"),
                   record(3, "Drew", "지각", "2026-11-05 10:00:00")]
        env, cursor, connection, _ = self.environment(records, [approved(name="Emery")])
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        node = next(node for node in ast.walk(tree) if isinstance(node, ast.Try)
                    and any(isinstance(child, ast.Name) and child.id == "attendance_view" for child in ast.walk(node)))
        env.update(cur=cursor, attendance_issues=0, leave_used=0)
        exec(compile(ast.fix_missing_locations(ast.Module(body=node.body, type_ignores=[])), "<actual-kpi-query>", "exec"), env)
        self.assertEqual(env["attendance_issues"], 1)
        self.assertEqual(connection.commits, 0)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.queries))
        cursor.leaves[0]["status"] = "반려"
        exec(compile(ast.fix_missing_locations(ast.Module(body=node.body, type_ignores=[])), "<actual-kpi-query>", "exec"), env)
        self.assertEqual(env["attendance_issues"], 2)

    def test_half_day_query_failures_close_connections_without_partial_writes(self):
        for name, args, records in (
            ("get_attendance", ("2026-11-05",), [record()]),
            ("get_attendance_monthly", ("2026", "11"), [record()]),
            ("clock_in", (), []), ("clock_out", (), [record()]),
        ):
            env, cursor, connection, _ = self.environment(records)
            original_execute = cursor.execute
            def fail_half_days(sql, parameters=()):
                if "FROM ext_leave_requests" in sql:
                    raise RuntimeError("synthetic read failure")
                return original_execute(sql, parameters)
            cursor.execute = fail_half_days
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                asyncio.run(env[name](FakeRequest(actor()), *args))
            self.assertTrue(connection.closed, name)
            self.assertEqual(connection.commits, 0)
            self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.queries))


if __name__ == "__main__":
    unittest.main()
