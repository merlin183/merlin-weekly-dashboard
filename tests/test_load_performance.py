"""실제 함수 AST와 가짜 Google SDK만 실행한다. 앱·DB·네트워크 접근은 없다."""
import ast
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

from fastapi.concurrency import run_in_threadpool


SOURCE = Path(__file__).resolve().parent.parent / "src/main.py"
SOURCE_TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, content, status_code, media_type):
        self.status_code = status_code
        self.body = content.encode()
        self.media_type = media_type


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 5)


class FakeGoogleService:
    def __init__(self, items=(), release=None, error=None):
        self.items = items
        self.release = release
        self.error = error
        self.started = threading.Event()
        self.created_thread = threading.get_ident()
        self.execute_thread = None
        self.queries = []

    def events(self):
        return self

    def list(self, **query):
        self.queries.append(query)
        return self

    def execute(self):
        self.execute_thread = threading.get_ident()
        self.started.set()
        if self.release is not None and not self.release.wait(3):
            raise TimeoutError("fake Google request was not released")
        if self.error:
            raise self.error
        return {"items": self.items}


class FakeSheetClient:
    def __init__(self, rows, release=None, error=None):
        self.rows, self.release, self.error = rows, release, error
        self.created_thread = threading.get_ident()
        self.read_thread = None
        self.started = threading.Event()
        self.calls = []

    def open_by_key(self, key):
        self.calls.append(("open", key))
        return self

    def worksheet(self, name):
        self.calls.append(("worksheet", name))
        return self

    def get_all_values(self):
        self.read_thread = threading.get_ident()
        self.started.set()
        if self.release is not None and not self.release.wait(3):
            raise TimeoutError("fake sheet request was not released")
        if self.error:
            raise self.error
        return self.rows


class LoopFinished(Exception):
    """테스트에서 120초 반복 대기 직전 종료한다."""


def load_functions(factory):
    """실행할 함수와 공개 경로 상수만 추출해 import-time 부작용을 피한다."""
    namespace = {
        "Request": object, "Response": FakeResponse, "json": json,
        "dt_date": FixedDate, "timedelta": timedelta,
        "GOOGLE_CALENDAR_ID": "fake-calendar", "_get_calendar_service": factory,
        "run_in_threadpool": run_in_threadpool,
        "_jwt_decode": lambda token: None,
        "_is_user_active_cached": lambda identifier: False,
        "_check_session": lambda token: False,
    }
    targets = {"_read_calendar_events", "get_calendar_events", "auth_middleware",
               "_read_expense_sheet_rows", "_watch_expense_sheet"}
    for node in SOURCE_TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_PUBLIC_PATHS" for target in node.targets):
            namespace["_PUBLIC_PATHS"] = ast.literal_eval(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            node = deepcopy(node)
            node.decorator_list = []
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                         "<actual-calendar-function>", "exec"), namespace)
    return namespace


def poller_environment(rows, store, release=None, failures=()):
    env = load_functions(lambda: None)
    observed = {"clients": [], "sleep": [], "reads": [], "watermark": [], "alerts": [], "heartbeat": [],
                "effects": []}

    def factory():
        index = len(observed["clients"])
        service = FakeSheetClient(rows, release=release,
                                  error=failures[index] if index < len(failures) else None)
        observed["clients"].append(service)
        return service

    async def sleep(seconds):
        observed["sleep"].append(seconds)
        if seconds == 120:
            raise LoopFinished()
        await asyncio.sleep(0)

    def read_store(key):
        observed["reads"].append(key)
        return deepcopy(store)

    async def alert(*args):
        observed["alerts"].append(args)
        observed["effects"].append(("alert", args))

    def watermark(value):
        observed["watermark"].append(value)
        observed["effects"].append(("watermark", value))

    env.update({"_get_gspread_client": factory, "EXPENSE_SHEET_ID": "fake-sheet",
                "EXPENSE_SHEET_NAME": "fake-tab", "EXPENSE_MAX_BATCH": 10,
                "asyncio": SimpleNamespace(sleep=sleep), "_read_store": read_store,
                "_expense_set_last_no": watermark, "_send_expense_alert": alert,
                "_heartbeat": lambda *args: observed["heartbeat"].append(args),
                "logger": SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)})
    return env, observed


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(poll(), timeout=2)


class LoadPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_expense_poller_read_does_not_block_event_loop_or_change_watermark(self):
        release = threading.Event()
        rows = [["No"], ["7", "2026", "2026-11-05", "Morgan", "업체", "기존 비용", "", "", "1000"]]
        before = deepcopy(rows)
        env, observed = poller_environment(rows, {"seeded": True, "last_no": 7}, release)
        loop_thread = threading.get_ident()
        task = asyncio.create_task(env["_watch_expense_sheet"]())
        try:
            await wait_until(lambda: observed["clients"] and observed["clients"][0].started.is_set())
            ticks = []
            for _ in range(3):
                await asyncio.sleep(0)
                ticks.append(True)
            self.assertEqual(len(ticks), 3)
            self.assertFalse(task.done())
            self.assertEqual(observed["sleep"], [40])
            self.assertEqual(observed["reads"], [])
        finally:
            release.set()
            with self.assertRaises(LoopFinished):
                await task
        client = observed["clients"][0]
        self.assertNotEqual(client.created_thread, loop_thread)
        self.assertEqual(client.created_thread, client.read_thread)
        self.assertEqual(client.calls, [("open", "fake-sheet"), ("worksheet", "fake-tab")])
        self.assertEqual(observed["sleep"], [40, 120])
        self.assertEqual(observed["reads"], ["expense_notified"])
        self.assertEqual(observed["watermark"], [])
        self.assertEqual(observed["alerts"], [])
        self.assertEqual(observed["heartbeat"], [("watch_expense_sheet", True)])
        self.assertEqual(rows, before)

    async def test_expense_poller_retries_with_fresh_clients_and_keeps_existing_seed_policy(self):
        error = RuntimeError("fake transient")
        error.response = SimpleNamespace(status_code=503)
        rows = [["7", "2026", "2026-11-05", "Morgan", "업체", "기존 비용"]]
        env, observed = poller_environment(rows, {}, failures=(error, error))
        with self.assertRaises(LoopFinished):
            await env["_watch_expense_sheet"]()
        self.assertEqual(observed["sleep"], [40, 2, 4, 120])
        self.assertEqual(len({id(client) for client in observed["clients"]}), 3)
        self.assertEqual(observed["watermark"], [7])
        self.assertEqual(observed["alerts"], [])
        self.assertEqual(observed["heartbeat"], [("watch_expense_sheet", True)])

    async def test_expense_poller_new_rows_keep_alert_then_watermark_and_failure_heartbeat(self):
        rows = [["8", "2026", "2026-11-05", "Morgan", "업체", "신규 비용", "", "", "1000"]]
        env, observed = poller_environment(rows, {"seeded": True, "last_no": 7})
        with self.assertRaises(LoopFinished):
            await env["_watch_expense_sheet"]()
        self.assertEqual(observed["alerts"], [("Morgan", "신규 비용", "1000", "2026-11-05", "Morgan", "엑셀 직접 입력")])
        self.assertEqual(observed["watermark"], [8])
        self.assertEqual([kind for kind, value in observed["effects"]], ["alert", "watermark"])
        self.assertEqual(observed["sleep"], [40, 120])
        env, failed = poller_environment(rows, {"seeded": True, "last_no": 7},
                                         failures=(RuntimeError("fake nontransient"),))
        with self.assertRaises(LoopFinished):
            await env["_watch_expense_sheet"]()
        self.assertEqual(failed["sleep"], [40, 120])
        self.assertEqual(failed["watermark"], [])
        self.assertEqual(failed["alerts"], [])
        self.assertEqual(failed["heartbeat"], [("watch_expense_sheet", False, "fake nontransient")])

    async def test_blocking_calendar_sdk_does_not_block_event_loop(self):
        release = threading.Event()
        services = []

        def factory():
            service = FakeGoogleService(release=release)
            services.append(service)
            return service

        env = load_functions(factory)
        loop_thread = threading.get_ident()
        request = asyncio.create_task(env["get_calendar_events"]("2026-11-01", "2026-11-30"))
        try:
            await wait_until(lambda: services and services[0].started.is_set())
            heartbeat = []
            for _ in range(3):
                await asyncio.sleep(0)
                heartbeat.append(True)
            self.assertEqual(len(heartbeat), 3)
            self.assertFalse(request.done(), "heartbeat must run while SDK is still blocked")
            self.assertNotEqual(services[0].created_thread, loop_thread)
            self.assertEqual(services[0].created_thread, services[0].execute_thread)
        finally:
            release.set()
            result = await request
        self.assertEqual(result, {"events": []})

    async def test_date_ranges_and_event_response_preserve_original_values(self):
        items = [
            {"id": "timed", "summary": "정기회의 [Riley, Morgan]",
             "description": "참석자: Finley, Morgan\\n기존 내용",
             "start": {"dateTime": "2026-11-05T09:30:00+09:00"},
             "end": {"dateTime": "2026-11-05T10:15:00+09:00"},
             "attendees": [{"displayName": "Casey"}, {"email": "guest@example.test"}],
             "creator": {"email": "owner@example.test"}},
            {"id": "all-day", "start": {"date": "2026-11-06"}, "end": {"date": "2026-11-07"}},
        ]
        before = deepcopy(items)
        services = []

        def factory():
            service = FakeGoogleService(items)
            services.append(service)
            return service

        env = load_functions(factory)
        result = await env["get_calendar_events"]("2026-11-01", "2026-11-30")
        self.assertEqual(services[0].queries, [{
            "calendarId": "fake-calendar", "timeMin": "2026-11-01T00:00:00+09:00",
            "timeMax": "2026-11-30T23:59:59+09:00", "maxResults": 100,
            "singleEvents": True, "orderBy": "startTime",
        }])
        self.assertEqual(result, {"events": [
            {"id": "timed", "title": "정기회의 [Riley, Morgan]", "description": items[0]["description"],
             "startDate": "2026-11-05", "endDate": "2026-11-05", "startTime": "09:30",
             "endTime": "10:15", "allDay": False,
             "attendees": ["Casey", "guest", "Finley", "Morgan", "Riley"], "creator": "owner"},
            {"id": "all-day", "title": "", "description": "", "startDate": "2026-11-06",
             "endDate": "2026-11-07", "startTime": "", "endTime": "", "allDay": True,
             "attendees": [], "creator": ""},
        ]})
        self.assertEqual(items, before)
        await env["get_calendar_events"]()
        self.assertEqual(services[1].queries[0]["timeMin"], "2026-11-02T00:00:00+09:00")
        self.assertEqual(services[1].queries[0]["timeMax"], "2026-12-02T23:59:59+09:00")
        await env["get_calendar_events"]("2026-11-05T01:02:03Z", "2026-11-06T04:05:06Z")
        self.assertEqual(services[2].queries[0]["timeMin"], "2026-11-05T01:02:03Z")
        self.assertEqual(services[2].queries[0]["timeMax"], "2026-11-06T04:05:06Z")
        self.assertEqual(len({id(service) for service in services}), 3)

    async def test_google_failure_is_502_and_does_not_poison_next_request(self):
        attempts = []
        release = threading.Event()

        def factory():
            service = FakeGoogleService(release=release if not attempts else None,
                                        error=RuntimeError("fake unavailable") if not attempts else None)
            attempts.append(service)
            return service

        env = load_functions(factory)
        failing = asyncio.create_task(env["get_calendar_events"]("2026-11-01", "2026-11-30"))
        try:
            await wait_until(lambda: attempts and attempts[0].started.is_set())
            result = await asyncio.wait_for(env["get_calendar_events"]("2026-11-01", "2026-11-30"), 2)
            self.assertEqual(result, {"events": []})
            self.assertFalse(failing.done())
        finally:
            release.set()
            response = await failing
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.media_type, "application/json")
        self.assertEqual(json.loads(response.body), {"error": "캘린더 조회 실패: fake unavailable"})
        self.assertEqual(await env["get_calendar_events"]("2026-11-01", "2026-11-30"), {"events": []})
        self.assertIsNot(attempts[0], attempts[1])

    async def test_client_creation_and_invalid_date_keep_existing_502_contract(self):
        def broken_factory():
            raise RuntimeError("fake credentials failure")

        env = load_functions(broken_factory)
        response = await env["get_calendar_events"]()
        self.assertEqual(response.status_code, 502)
        self.assertIn("fake credentials failure", json.loads(response.body)["error"])
        service = FakeGoogleService()
        env["_get_calendar_service"] = lambda: service
        response = await env["get_calendar_events"]("invalid-date")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(service.queries, [])

    async def test_calendar_keeps_authentication_and_existing_role_access(self):
        services = []

        def factory():
            service = FakeGoogleService()
            services.append(service)
            return service

        env = load_functions(factory)
        self.assertNotIn("/api/calendar/events", env["_PUBLIC_PATHS"])

        def request(headers=None):
            return SimpleNamespace(url=SimpleNamespace(path="/api/calendar/events"),
                                   headers=headers or {}, state=SimpleNamespace())

        async def next_handler(current):
            return await env["get_calendar_events"]("2026-11-01", "2026-11-30")

        unauthenticated = await env["auth_middleware"](request(), next_handler)
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(services, [])
        for role in ("member", "pm", "admin"):
            with self.subTest(role=role):
                env["_jwt_decode"] = lambda token, role=role: {"user_id": "local-id", "name": "Morgan",
                                                             "email": "test@example.test", "role": role}
                env["_is_user_active_cached"] = lambda identifier: True
                current = request({"Authorization": "Bearer fake-jwt"})
                self.assertEqual(await env["auth_middleware"](current, next_handler), {"events": []})
                self.assertEqual(current.state.user["role"], role)
        env["_is_user_active_cached"] = lambda identifier: False
        inactive = await env["auth_middleware"](request({"Authorization": "Bearer fake-jwt"}), next_handler)
        self.assertEqual(inactive.status_code, 401)
        self.assertEqual(len(services), 3)
        env["_jwt_decode"] = lambda token: None
        env["_check_session"] = lambda token: True
        legacy = request({"Authorization": "Bearer fake-session"})
        self.assertEqual(await env["auth_middleware"](legacy, next_handler), {"events": []})
        self.assertEqual(legacy.state.user["user_id"], "legacy")


if __name__ == "__main__":
    unittest.main()
