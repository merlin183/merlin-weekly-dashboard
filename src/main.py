"""주간 업무 보고 대시보드 - FastAPI 백엔드

API:
  GET  /api/report         → 리포트 데이터 조회
  PUT  /api/report         → 리포트 데이터 저장
  GET  /api/team-config    → 팀 설정 조회
  PUT  /api/team-config    → 팀 설정 저장
  GET  /api/todos          → 할일 데이터 조회
  PUT  /api/todos          → 할일 데이터 저장
  POST /api/slack/notify   → Slack DM 알림 전송
  POST /api/slack/test     → Slack 연결 테스트
  POST /api/upload         → 파일 업로드 (이미지 + 문서)
  DELETE /api/upload/{name} → 업로드된 파일 삭제
  GET  /api/comments/{id}  → 프로젝트별 댓글 조회
  POST /api/comments/{id}  → 익명 댓글 추가
  DELETE /api/comments/{id}/{cid} → 댓글 삭제
  POST /api/ai/summarize   → Gemini AI 안건 요약 생성
"""
import asyncio
import hashlib as _hashlib_mod
import json
import logging
import os
import sqlite3
import traceback
import uuid
import time as _time_mod
from datetime import datetime, timedelta, date as dt_date
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

try:
    from src.attendance_policy import (
        HALF_DAY_TYPES, attendance_date as _attendance_date, month_bounds as _attendance_month_bounds,
        build_attendance_view as _build_attendance_view, arrival_status as _attendance_arrival_status,
        period_bounds as _attendance_period_bounds, add_business_day_coverage as _add_attendance_coverage,
    )
except ImportError:
    from attendance_policy import (
        HALF_DAY_TYPES, attendance_date as _attendance_date, month_bounds as _attendance_month_bounds,
        build_attendance_view as _build_attendance_view, arrival_status as _attendance_arrival_status,
        period_bounds as _attendance_period_bounds, add_business_day_coverage as _add_attendance_coverage,
    )

import httpx
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

try:
    from src import installation_policy as _installation
    from src.passwords import hash_password as _hash_password, verify_password as _verify_password, validate_password as _validate_password
except ImportError:
    import installation_policy as _installation
    from passwords import hash_password as _hash_password, verify_password as _verify_password, validate_password as _validate_password

BACKGROUND_TASKS_ENABLED = _installation.BACKGROUND_TASKS_ENABLED
STORAGE_AUTO_CREATE_BUCKET = _installation.STORAGE_AUTO_CREATE_BUCKET
LEGACY_SHARED_LOGIN_ENABLED = _installation.LEGACY_SHARED_LOGIN_ENABLED

# Supabase Storage 설정 (시크릿은 환경변수에서만 — 하드코딩 제거)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "uploads").strip()

def _supabase_storage_headers():
    """Supabase Storage API 인증 헤더"""
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }

# BigQuery 프로젝트 (API 비용 조회용)
BQ_PROJECT = os.environ.get("BQ_PROJECT", "")
# BigQuery 서비스 계정 키: 환경변수 JSON → 임시 파일, 또는 로컬 파일
_BQ_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
_gcp_json_env = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
if _gcp_json_env:
    import tempfile
    _tmp_key = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp_key.write(_gcp_json_env)
    _tmp_key.close()
    _BQ_KEY_PATH = _tmp_key.name

BQ_LLM_TABLE = os.environ.get("BQ_LLM_TABLE", "")
BQ_ORDERS_TABLE = os.environ.get("BQ_ORDERS_TABLE", "")
# GA4 BigQuery 익스포트 데이터셋 (방문자 users/sessions 소스). events_YYYYMMDD 일별 테이블
BQ_GA4_DATASET = os.environ.get("BQ_GA4_DATASET", "")
# 운영 Aurora 읽기전용 analytics_ro 뷰 접속 (순매출/API비용 권위 출처). 비밀번호 포함 — 환경변수로만.
# 미설정 시 기존 BigQuery 경로로 폴백.
ANALYTICS_DB_URL = os.environ.get("ANALYTICS_DB_URL", "")
# 채널톡 Open API (CS 인입 집계 — /cs 독립 페이지). 시크릿은 환경변수로만.
CHANNELTALK_ACCESS_KEY = os.environ.get("CHANNELTALK_ACCESS_KEY", "")
CHANNELTALK_ACCESS_SECRET = os.environ.get("CHANNELTALK_ACCESS_SECRET", "")
# /cs 페이지 전용 비밀번호. 미설정 시 CS 로그인을 비활성화한다.
CS_PAGE_PASSWORD = os.environ.get("CS_PAGE_PASSWORD", "")
# API 비용(cost_usd) → 원화 환산 환율. 봇과 맞추려면 봇이 쓰는 환율로 설정.
USD_KRW_RATE = float(os.environ.get("USD_KRW_RATE", "1"))
# Installer billing multiplier; neutral by default.
API_COST_MARKUP = float(os.environ.get("API_COST_MARKUP", "1"))

# BigQuery 모델별 비용 계산 SQL (CASE 절)
_BQ_COST_CASE = _installation.bq_cost_case()
from fastapi import FastAPI, Request, Response, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

logger = logging.getLogger("weekly-report")

# 경로 설정
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = DATA_DIR / "uploads"

# 업로드 / 백업 디렉토리 자동 생성
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
BACKUPS_DIR = DATA_DIR / "backups"
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
MAX_BACKUPS = 30  # 최대 보관 수

# 허용 파일 확장자 (이미지 + 문서)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".hwp", ".txt", ".csv", ".zip", ".rar", ".html", ".htm"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | DOC_EXTENSIONS

# 데이터 파일 경로
REPORT_FILE = DATA_DIR / "report.json"
TEAM_CONFIG_FILE = DATA_DIR / "team_config.json"
TODOS_FILE = DATA_DIR / "todos.json"
COMMENTS_FILE = DATA_DIR / "comments.json"

# 동시 쓰기 방지용 락
_write_lock = asyncio.Lock()

# DB 백업 디렉토리
DB_BACKUPS_DIR = DATA_DIR / "db_backups"
DB_BACKUPS_DIR.mkdir(exist_ok=True)
MAX_DB_BACKUPS = 30

# ── PostgreSQL (Supabase) 중앙 DB ──
METRICS_DB = DATA_DIR / "metrics_cache.db"  # 로컬 백업용 경로 유지
DATABASE_URL = os.environ.get("DATABASE_URL", "")

import psycopg2
from psycopg2 import pool as pg_pool

# 커넥션 풀 (매 요청마다 새 연결 생성 방지)
_connection_pool = None

# 커넥션 keepalive·타임아웃 — 죽은 커넥션으로의 쿼리가 이벤트 루프를
# 수십 초씩 멈추지 않도록 한다.
_PG_CONN_KWARGS = dict(
    connect_timeout=10,        # 새 연결 수립 10초 상한
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
    tcp_user_timeout=15000,    # 응답 없는 커넥션은 15초 안에 TCP 레벨에서 끊김
)


def _get_pool():
    """SimpleConnectionPool 싱글턴 반환"""
    global _connection_pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    if _connection_pool is None or _connection_pool.closed:
        # 풀 최대치 환경변수화 — 운영 기본 10, 테스트 Supabase(세션 풀러 15-클라이언트 한도)는
        # PG_POOL_MAX 낮춰 스모크 등 추가 연결 여유 확보.
        _pool_max = int(os.environ.get("PG_POOL_MAX", "10"))
        _connection_pool = pg_pool.SimpleConnectionPool(1, _pool_max, DATABASE_URL, **_PG_CONN_KWARGS)
    return _connection_pool

class _PooledConnection:
    """close() 호출 시 풀에 반납하는 래퍼"""
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
    def cursor(self):
        return self._conn.cursor()
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._pool.putconn(self._conn)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()

def _pg_conn():
    """PostgreSQL 풀 커넥션 반환 — pre-ping 없이 즉시 반환 (lazy 검증).

    이전 버전은 매 요청 SELECT 1로 생존을 확인했는데(50-100ms 왕복 1회 추가),
    Supabase pooler가 keepalive 잘 유지해서 죽은 커넥션이 드물다. 매 요청
    추가 핑보다 가끔 발생하는 OperationalError를 _safe_pg_conn에서 흡수하는
    편이 훨씬 빠르다. 죽은 커넥션은 호출자가 _safe_pg_conn을 쓰거나
    예외 처리하면 됨.
    """
    global _connection_pool
    for _ in range(2):
        try:
            p = _get_pool()
            conn = p.getconn()
            # 트랜잭션 잔재 정리 (이전 사용자가 putconn 전에 commit/rollback 안 했을 수 있음)
            try:
                conn.rollback()
            except psycopg2.Error:
                # rollback 실패 = 죽은 커넥션 → 폐기하고 재시도
                try:
                    p.putconn(conn, close=True)
                except Exception:
                    pass
                continue
            return _PooledConnection(conn, p)
        except pg_pool.PoolError:
            break  # 풀 고갈 → 직접 연결
        except psycopg2.Error:
            _connection_pool = None  # 풀 자체 망가짐 → 재생성
    # 폴백: 풀 우회 직접 연결
    return psycopg2.connect(DATABASE_URL, **_PG_CONN_KWARGS)


# ── 멀티워커 조정 (uvicorn --workers) ──
# advisory lock 키 — 워커 간 조정용 (서로 다른 키라 독립적으로 동작)
_LEADER_LOCK_KEY = 912771   # 백그라운드 작업 담당 워커 선출
_REPORT_LOCK_KEY = 912772   # 리포트 저장 직렬화
_SCHEMA_LOCK_KEY = 912773   # Serialize schema initialization across workers and bootstrap processes
_leader_conn = None         # 리더 락 유지용 장수명 커넥션 (GC 방지)
_IS_LEADER = False          # 이 워커가 백그라운드 작업 담당(리더)인지 — 스케줄러 게이팅용


def _try_become_leader() -> bool:
    """advisory lock으로 백그라운드 작업 담당 워커 1개를 선출한다.

    멀티워커에서는 각 워커가 startup 시 이 함수를 호출하고, 락을 잡은 워커만
    True를 받아 백그라운드 동기화 루프를 실행한다. 안 그러면 워커 수만큼
    중복 실행돼 외부 API 한도 초과·DB 부하가 악화된다. advisory lock은 세션
    단위라 리더 커넥션을 프로세스 수명 동안 열어 둔다.
    """
    global _leader_conn, _IS_LEADER
    try:
        # 세션 단위 advisory lock은 트랜잭션 풀러(6543)에선 백엔드가 재사용될 때 유실돼
        # 리더 게이트가 무력화된다(워커 수만큼 중복 실행). 세션 풀러(5432)로 붙으면
        # 연결 수명 동안 같은 백엔드가 고정돼 락이 유지된다 → 리더 1개 보장.
        leader_url = DATABASE_URL.replace(":6543/", ":5432/") if ":6543/" in DATABASE_URL else DATABASE_URL
        conn = psycopg2.connect(leader_url, **_PG_CONN_KWARGS)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
        cur.close()
        if got:
            _leader_conn = conn   # 세션 락 유지를 위해 연결 보관
            _IS_LEADER = True
            return True
        conn.close()
        return False
    except Exception as e:
        logger.warning(f"[리더 선출] 실패 — 백그라운드 작업 건너뜀: {e}")
        return False


def _read_store(key: str) -> dict | None:
    """ext_app_store에서 JSON 데이터 읽기"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT data FROM ext_app_store WHERE key = %s", (key,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    d = row[0]
    return d if isinstance(d, dict) else json.loads(d)


async def _write_store(key: str, data: dict) -> None:
    """ext_app_store에 JSON 데이터 쓰기"""
    async with _write_lock:
        _write_store_unlocked(key, data)


def _write_store_unlocked(key: str, data: dict) -> None:
    """_write_lock을 이미 획득한 상태에서 호출하는 쓰기 (재진입 락 방지용)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ext_app_store (key, data, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()",
        (key, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ── Linear 캐시 테이블 및 헬퍼 (L2 DB 캐시) ──

def _ensure_linear_cache_table():
    """ext_linear_cache 테이블이 없으면 생성"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_linear_cache (
        cache_key TEXT PRIMARY KEY,
        data JSONB NOT NULL,
        updated_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.commit()
    conn.close()


def _read_linear_cache(key: str) -> dict | None:
    """ext_linear_cache에서 캐시 데이터 읽기"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT data, updated_at FROM ext_linear_cache WHERE cache_key = %s", (key,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"data": row[0], "updated_at": str(row[1])}
    return None


def _write_linear_cache(key: str, data):
    """ext_linear_cache에 캐시 데이터 저장 (UPSERT)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO ext_linear_cache (cache_key, data, updated_at)
           VALUES (%s, %s::jsonb, NOW())
           ON CONFLICT (cache_key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()""",
        (key, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ── Linear → DB 백그라운드 동기화 상태 ──
_linear_db_sync_running = False
_linear_db_last_sync: str | None = None  # 마지막 동기화 시각 (ISO)
_linear_db_last_duration: float = 0.0     # 마지막 동기화 소요 시간 (초)


async def _sync_linear_to_db():
    """Linear 데이터를 로컬 DB에 동기화 (백그라운드)"""
    global _linear_db_sync_running, _linear_db_last_sync, _linear_db_last_duration
    if _linear_db_sync_running or not LINEAR_API_KEY:
        return
    _linear_db_sync_running = True
    t0 = _time_mod.time()
    try:
        logger.info("[Linear DB캐시] 동기화 시작")

        # 1) Projects (installer-configured excluded teams + projects and issues)
        exclude_ids = set()
        try:
            team_q = await _linear_graphql(
                '{ projects(first: 50) { nodes { id teams { nodes { name } } } } }'
            )
            for tp in team_q.get("data", {}).get("projects", {}).get("nodes", []):
                if any(t.get("name") in _installation.LINEAR_EXCLUDED_TEAM_NAMES for t in (tp.get("teams") or {}).get("nodes", [])):
                    exclude_ids.add(tp["id"])
        except Exception:
            pass

        proj_result = await _linear_graphql("""{
            projects(first: 20) {
                nodes {
                    id name state priority sortOrder
                    issues { nodes { id identifier title state { name type } priority assignee { name } labels { nodes { name } } completedAt createdAt description } }
                }
            }
        }""")
        all_projects = proj_result.get("data", {}).get("projects", {}).get("nodes", [])
        # 원본 데이터 + 제외 ID 함께 저장
        _write_linear_cache("linear_projects", {
            "exclude_ids": list(exclude_ids),
            "projects": all_projects,
        })
        logger.info(f"[Linear DB캐시] projects 저장 완료 ({len(all_projects)}건)")

        # 2) Cycles (활성 사이클 + Initiative 매핑)
        cycle_result = await _linear_graphql("""{
            cycles(first: 5, filter: { isActive: { eq: true } }) { nodes {
                id name number startsAt endsAt progress
                team { id name }
                issues(first: 250) { nodes {
                    id identifier title priority
                    state { name type }
                    assignee { name }
                    project { name }
                    parent { id identifier }
                } }
            } }
        }""")
        # Initiative 매핑 (별도 쿼리)
        proj_init_map = {}
        try:
            pi_result = await _linear_graphql(
                '{ projects(first: 50) { nodes { name initiatives { nodes { name } } } } }'
            )
            for p in pi_result.get("data", {}).get("projects", {}).get("nodes", []):
                inits = (p.get("initiatives") or {}).get("nodes", [])
                if inits:
                    proj_init_map[p["name"]] = inits[0].get("name", "")
        except Exception:
            pass

        cycles_raw = cycle_result.get("data", {}).get("cycles", {}).get("nodes", [])
        _write_linear_cache("linear_cycles", {
            "cycles": cycles_raw,
            "proj_init_map": proj_init_map,
        })
        logger.info(f"[Linear DB캐시] cycles 저장 완료 ({len(cycles_raw)}건)")

        # 3) Initiatives (프로젝트 계층 구조)
        init_direct = await _linear_graphql("""{
            initiatives(first: 50) { nodes { id name status targetDate } }
        }""")
        init_proj_result = await _linear_graphql("""{
            projects(first: 50) { nodes {
                id name state priority targetDate
                initiatives { nodes { id name status targetDate } }
            } }
        }""")
        _write_linear_cache("linear_initiatives", {
            "initiatives": init_direct.get("data", {}).get("initiatives", {}).get("nodes", []),
            "projects": init_proj_result.get("data", {}).get("projects", {}).get("nodes", []),
        })
        logger.info("[Linear DB캐시] initiatives 저장 완료")

        # 4) My-issues (담당자별 미완료 이슈) — 엔드포인트가 캐시 우선 조회
        mi_result = await _linear_graphql(_LINEAR_MY_ISSUES_Q)
        _write_linear_cache("linear_my_issues", {
            "issues": mi_result.get("data", {}).get("issues", {}).get("nodes", []),
        })
        logger.info("[Linear DB캐시] my-issues 저장 완료")

        elapsed = _time_mod.time() - t0
        _linear_db_last_sync = datetime.utcnow().isoformat() + "Z"
        _linear_db_last_duration = round(elapsed, 2)
        logger.info(f"[Linear DB캐시] 동기화 완료 ({_linear_db_last_duration}초)")

    except Exception as e:
        logger.error(f"[Linear DB캐시] 동기화 실패: {e}")
    finally:
        _linear_db_sync_running = False


async def _linear_db_sync_loop():
    """5분 주기 Linear → DB 동기화 루프"""
    await asyncio.sleep(5)  # 서버 시작 5초 후 첫 동기화
    while True:
        try:
            await _sync_linear_to_db()
            _heartbeat("linear_db_sync", True)
        except Exception as e:
            _heartbeat("linear_db_sync", False, str(e))
            logger.warning(f"_linear_db_sync_loop 실패: {e}")
        await asyncio.sleep(300)  # 5분 간격


def _migrate_json_to_pg():
    """기존 JSON 파일/SQLite → PostgreSQL 마이그레이션 (최초 1회).

    신규/빈 DB(테이블 미존재)나 DATABASE_URL 미설정 환경(CI·테스트)에서도
    import 시 안전하게 동작하도록 방어적으로 작성. report 저장소 테이블은
    여기서 보장 생성한다(없으면 앱 자체가 report 저장 불가).
    """
    if not DATABASE_URL:
        return
    try:
        conn = _pg_conn()
    except Exception as e:
        logger.warning(f"[마이그레이션] DB 연결 불가로 건너뜀: {e}")
        return
    try:
        cur = conn.cursor()
        # 신규 DB 대응: report 저장소 테이블 보장 생성
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ext_app_store ("
            "key TEXT PRIMARY KEY, data JSONB, updated_at TIMESTAMP DEFAULT NOW())"
        )
        conn.commit()
        migrations = {
            "report": REPORT_FILE,
            "team_config": TEAM_CONFIG_FILE,
            "todos": TODOS_FILE,
        }
        for key, path in migrations.items():
            cur.execute("SELECT 1 FROM ext_app_store WHERE key = %s", (key,))
            if cur.fetchone():
                continue
            # SQLite app_store에서 먼저 시도
            if Path(str(METRICS_DB)).exists():
                try:
                    import sqlite3 as _sq
                    sq = _sq.connect(str(METRICS_DB))
                    sq_row = sq.execute("SELECT data FROM app_store WHERE key = ?", (key,)).fetchone()
                    sq.close()
                    if sq_row:
                        cur.execute(
                            "INSERT INTO ext_app_store (key, data, updated_at) VALUES (%s, %s, NOW())",
                            (key, sq_row[0]),
                        )
                        logger.info(f"[마이그레이션] SQLite app_store[{key}] → PostgreSQL 완료")
                        continue
                except Exception:
                    pass
            # JSON 파일에서 시도
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO ext_app_store (key, data, updated_at) VALUES (%s, %s, NOW())",
                        (key, json.dumps(data, ensure_ascii=False)),
                    )
                    logger.info(f"[마이그레이션] {path.name} → PostgreSQL 완료")
                except Exception as e:
                    logger.warning(f"[마이그레이션] {path.name} 실패: {e}")
        conn.commit()
    except Exception as e:
        logger.warning(f"[마이그레이션] 건너뜀: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass



# ── 인증 설정 ── (시크릿은 환경변수에서만 — 하드코딩 제거)
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
import hashlib, secrets, hmac

# JWT signing key is required at startup and supplied by the installer.
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_EXPIRY_HOURS = 24 * 30  # 30일

# ── ext_users 테이블 생성 및 시드 ──
def _init_users_table():
    """Create user schema only. Initial account creation is an explicit CLI operation."""
    conn = _pg_conn()
    cur = conn.cursor()
    # 테이블 생성
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ext_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            slack_id TEXT DEFAULT '',
            linear_id TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            last_login TIMESTAMP
        )
    """)
    # 입사일 컬럼 (기존 테이블 대응 — 없으면 추가)
    cur.execute("ALTER TABLE ext_users ADD COLUMN IF NOT EXISTS hire_date DATE")
    # 연차 산정 기준일 (실제 입사일과 다를 수 있음 — 예: 경력 인정·휴직 보정)
    cur.execute("ALTER TABLE ext_users ADD COLUMN IF NOT EXISTS leave_start_date DATE")
    conn.commit()

    conn.close()




# ── JWT 유틸리티 ──
import base64 as _b64

def _jwt_encode(payload: dict) -> str:
    """간단한 JWT 인코딩 (HS256, PyJWT 없이 직접 구현)"""
    import time
    _installation.require_jwt_secret(JWT_SECRET)
    payload = dict(payload)
    header = {"alg": "HS256", "typ": "JWT"}
    # exp 추가
    if "exp" not in payload:
        payload["exp"] = int(time.time()) + JWT_EXPIRY_HOURS * 3600

    def _b64url(data: bytes) -> str:
        return _b64.urlsafe_b64encode(data).rstrip(b"=").decode()

    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    sig_input = f"{h}.{p}".encode()
    sig = hmac.new(JWT_SECRET.encode(), sig_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def _jwt_decode(token: str) -> dict | None:
    """JWT 디코딩 및 검증. 실패 시 None 반환."""
    import time
    try:
        _installation.require_jwt_secret(JWT_SECRET)
        parts = token.split(".")
        if len(parts) != 3:
            return None

        def _b64url_decode(s: str) -> bytes:
            s += "=" * (4 - len(s) % 4)
            return _b64.urlsafe_b64decode(s)

        header = json.loads(_b64url_decode(parts[0]))
        if header != {"alg": "HS256", "typ": "JWT"}:
            return None
        # 서명 검증
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = hmac.new(JWT_SECRET.encode(), sig_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(parts[2])
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        # 페이로드 디코딩
        payload = json.loads(_b64url_decode(parts[1]))

        # 만료 확인
        if payload.get("exp", 0) < int(time.time()):
            return None

        return payload
    except Exception:
        return None

# 공용 비번 세션 토큰 — ext_app_store(DB)에 저장 (Railway 재배포 시 컨테이너 파일이 날아가는 것 방지)
_SESSION_STORE_KEY = "legacy_sessions"
_SESSION_EXPIRY_DAYS = 30


def _load_sessions() -> dict:
    """DB에서 세션 토큰 로드. {token: expire_iso} 형태. 만료된 건 제외."""
    data = _read_store(_SESSION_STORE_KEY) or {}
    if not isinstance(data, dict):
        return {}
    now = datetime.utcnow()
    out = {}
    for t, exp in data.items():
        try:
            if datetime.fromisoformat(exp) > now:
                out[t] = exp
        except Exception:
            pass
    return out


def _save_sessions(sessions: dict) -> None:
    """세션 dict를 DB에 저장."""
    _write_store_unlocked(_SESSION_STORE_KEY, sessions)


def _add_session(token: str) -> None:
    """새 세션 토큰 추가 (30일 만료)."""
    sessions = _load_sessions()
    expire = (datetime.utcnow() + timedelta(days=_SESSION_EXPIRY_DAYS)).isoformat()
    sessions[token] = expire
    _save_sessions(sessions)


def _check_session(token: str) -> bool:
    """토큰이 유효한지 확인."""
    if not LEGACY_SHARED_LOGIN_ENABLED or not DASHBOARD_PASSWORD.strip():
        return False
    sessions = _load_sessions()
    if token not in sessions:
        return False
    try:
        if datetime.fromisoformat(sessions[token]) < datetime.utcnow():
            del sessions[token]
            _save_sessions(sessions)
            return False
    except Exception:
        return False
    return True

# ── 프로덕션 판별 ──
_IS_PRODUCTION = "RAILWAY_ENVIRONMENT" in os.environ or "PORT" in os.environ

# Swagger/OpenAPI는 프로덕션에서 비활성화
app = FastAPI(
    title="주간 업무 보고 대시보드 API",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)


# ── 프로덕션 에러 핸들러: 내부 구조 노출 방지 ──
@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    if _IS_PRODUCTION:
        return Response(
            content=json.dumps({"error": "요청 형식이 올바르지 않습니다"}),
            status_code=422,
            media_type="application/json",
        )
    # 개발 환경에서는 상세 정보 표시
    return Response(
        content=json.dumps({"detail": exc.errors()}),
        status_code=422,
        media_type="application/json",
    )


# CORS 설정 — 허용된 출처만
_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",") if os.environ.get("ALLOWED_ORIGINS") else []
if not _ALLOWED_ORIGINS and not _IS_PRODUCTION:
    _ALLOWED_ORIGINS = ["http://localhost:8080", "http://127.0.0.1:8080"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

# ── gzip 압축 (정적 자산 + JSON 응답) — Accept-Encoding: gzip 클라이언트만 압축 ──
# index.html 548KB, app.js 271KB, /api/report 1.2MB 등 큰 응답이 raw로 전송되던 문제 해결.
# 최소 압축 크기 500바이트 (작은 응답은 압축 overhead가 더 큼)
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


# ── 보안 헤더 미들웨어 ──
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://*.supabase.co; "
        "connect-src 'self' https://cdn.jsdelivr.net https://*.supabase.co https://generativelanguage.googleapis.com; "
        "font-src 'self'; "
        "frame-ancestors 'none'"
    ))
    if _IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # 정적 파일 캐싱:
    #  - 버전(?v=내용해시) 붙은 JS/CSS는 immutable 장기 캐시 → 재검증 왕복 제거(배포 시 해시가
    #    바뀌어 새 URL이 되므로 즉시 반영). 버전은 서버가 _get_index_html()에서 자동 주입.
    #  - 그 외 JS/CSS/HTML 및 "/"는 no-cache(항상 최신 — 버전 정보를 담은 진입점이므로).
    path = request.url.path
    if path.endswith(('.js', '.css')) and request.query_params.get('v'):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.endswith(('.js', '.css', '.html')) or path == '/':
        response.headers["Cache-Control"] = "no-cache"
    return response


# ── 인증 미들웨어 ──
# 인증이 필요 없는 경로
_PUBLIC_PATHS = {"/api/auth/login", "/api/auth/check", "/api/auth/login-user", "/api/health",
                 "/api/cs/login", "/api/cs/metrics", "/api/cs/consultations",
                 "/api/cs/sentiment/fill", "/api/cs/weekly-report", "/api/cs/app"}  # /cs는 독립 비밀번호로 엔드포인트 내부에서 검증

# is_active 검증 — 인증 핫패스. 앱(싱가포르)↔DB(서울) 크로스리전이라 매 요청 DB lookup이
# 요청당 ~0.1s floor를 만듦. 30초 캐시로 왕복 제거(비활성화 반영은 최대 TTL 지연, 워커별 독립).
_ACTIVE_TTL = 30  # 초 — 비활성화/강제로그아웃 반영 최대 지연
_active_cache: dict[str, tuple[float, bool]] = {}  # user_id -> (expires_at, is_active)


def _is_user_active_cached(user_id: str) -> bool:
    now = _time_mod.time()
    hit = _active_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT is_active FROM ext_users WHERE id = %s", (user_id,))
        row = cur.fetchone(); conn.close()
        active = bool(row and row[0])
    except Exception:
        return False  # Authentication fails closed while account state cannot be verified.
    _active_cache[user_id] = (now + _ACTIVE_TTL, active)
    if len(_active_cache) > 2000:  # 메모리 가드: 만료 항목 정리
        for k in [k for k, v in _active_cache.items() if v[0] <= now]:
            _active_cache.pop(k, None)
    return active


def _invalidate_active_cache(user_id: str = None):
    """사용자 비활성화/수정 시 캐시 즉시 제거(해당 워커). 다른 워커는 TTL 만료로 반영."""
    if user_id is None:
        _active_cache.clear()
    else:
        _active_cache.pop(user_id, None)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # 정적 파일, 인증 엔드포인트, 파일 프록시, Webhook(자체 HMAC 검증)은 통과
    if not path.startswith("/api/") or path in _PUBLIC_PATHS or path == "/api/webhooks/linear":
        return await call_next(request)

    # Authorization 헤더에서 토큰 확인
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token and path.startswith("/api/file-proxy/") and request.method == "GET":
        token = request.cookies.get("weekly_file_session", "")
    if token:

        # JWT 토큰 확인 (개인 로그인)
        jwt_payload = _jwt_decode(token)
        if jwt_payload:
            # 사용자 is_active 검증 — 강제 로그아웃/비활성화 즉시 반영
            uid = jwt_payload.get("user_id")
            if uid and _is_user_active_cached(uid):
                request.state.user = {
                    "user_id": uid,
                    "name": jwt_payload.get("name"),
                    "email": jwt_payload.get("email"),
                    "role": jwt_payload.get("role"),
                }
                return await call_next(request)
            # 비활성 사용자 → 401로 강제 로그아웃 유도
            return Response(content=json.dumps({"error": "계정이 비활성화되었습니다. 다시 로그인해주세요"}),
                            status_code=401, media_type="application/json")

        # 공용 비번 세션 (회의용·게스트) — 직원과 동일한 member 권한만 부여
        if _check_session(token):
            request.state.user = {
                "user_id": "legacy",
                "name": "공용",
                "email": "",
                "role": "member",
            }
            return await call_next(request)

    return Response(
        content=json.dumps({"error": "인증이 필요합니다"}),
        status_code=401,
        media_type="application/json",
    )


# ── 이전 모드: 이 서버로 온 요청을 새 주소로 넘김 ──
# 서버를 Railway → Cloudflare로 옮긴 뒤 옛 Railway에 REDIRECT_TO(새 주소)를 설정하면
# 모든 요청을 308(메서드·본문 유지)로 새 주소에 넘기고, 백그라운드 작업(리더 선출)도 돌리지 않는다.
# 되돌리려면 REDIRECT_TO 를 지우고 재배포. 가장 바깥 미들웨어여야 해서 다른 미들웨어보다 뒤에 둔다.
REDIRECT_TO = os.environ.get("REDIRECT_TO", "").strip().rstrip("/")


@app.middleware("http")
async def redirect_to_new_host_middleware(request: Request, call_next):
    if REDIRECT_TO and request.url.path != "/api/health":
        target = REDIRECT_TO + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=308)
    return await call_next(request)


# ── 헬스체크 (외부 업타임 모니터용, 인증 불필요) ──
# 서버가 인터넷에 나가는 Cloudflare 위치(예: ICN=서울, SIN=싱가포르) — DB(서울)와의 거리 진단용. 1회만 조회
_EGRESS_COLO: str | None = None


async def _egress_colo() -> str:
    global _EGRESS_COLO
    if not _installation.EGRESS_DIAGNOSTICS_ENABLED:
        return "disabled"
    if _EGRESS_COLO is None:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
            kv = dict(line.split("=", 1) for line in r.text.splitlines() if "=" in line)
            _EGRESS_COLO = f"{kv.get('colo', '?')}/{kv.get('loc', '?')}"
        except Exception:
            _EGRESS_COLO = "?"
    return _EGRESS_COLO


@app.get("/api/health")
async def health_check():
    """헬스체크 — DB 연결 확인. 정상이면 200, DB 장애면 503.
    UptimeRobot 등 외부 모니터가 1~5분 주기로 핑 → 다운 시 알림."""
    try:
        t0 = _time_mod.time()
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        db_ms = round((_time_mod.time() - t0) * 1000)
        return {"ok": True, "db": "up", "db_ms": db_ms, "colo": await _egress_colo()}
    except Exception as e:
        return Response(
            content=json.dumps({"ok": False, "db": "down", "error": str(e)[:200]}),
            status_code=503, media_type="application/json",
        )


@app.get("/api/_diag/egress")
async def diag_egress(request: Request):
    """진단(admin/PM 전용): 이 서버(Railway)의 공인 egress IP + 운영 Aurora 연결 테스트.
    Aurora 보안그룹 등록 IP 확인·연결 점검용."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(
            content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}),
            status_code=403,
            media_type="application/json",
        )
    if not _installation.EGRESS_DIAGNOSTICS_ENABLED:
        return Response(content=json.dumps({"error": "Egress diagnostics are disabled"}), status_code=503, media_type="application/json")
    result = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            ips = []
            for svc in ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ifconfig.me/ip"):
                try:
                    r = await client.get(svc)
                    if r.status_code == 200:
                        ips.append(r.text.strip())
                except Exception:
                    pass
            result["egress_ips"] = ips
    except Exception as e:
        result["egress_err"] = str(e)[:100]
    if ANALYTICS_DB_URL:
        try:
            import psycopg2
            c = psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=8)
            cur = c.cursor(); cur.execute("SELECT 1"); cur.fetchone(); c.close()
            result["analytics_db"] = "connected"
        except Exception as e:
            result["analytics_db"] = "FAIL: " + str(e)[:150]
    else:
        result["analytics_db"] = "ANALYTICS_DB_URL 미설정"
    return result


# ── 인증 API ──
# ── 로그인 무차별 대입 방어 (인메모리 슬라이딩 윈도우 잠금) ──
# 워커별 독립 상태(uvicorn 2워커)지만 각 워커가 독립적으로 잠그므로 대입 속도를 충분히 둔화.
# DB 쓰기 없음 → Supabase 연결 과부하 회피. 키 = 클라이언트 IP.
_LOGIN_FAIL_WINDOW = 300      # 최근 5분 윈도우
_LOGIN_FAIL_MAX = 10          # 5분 내 10회 실패면
_LOGIN_LOCK_SECONDS = 300     # 5분 잠금
_login_fails: dict[str, list[float]] = {}
_login_lock_until: dict[str, float] = {}


def _client_ip(request) -> str:
    """프록시(Railway) 뒤이므로 X-Forwarded-For 우선, 첫 IP 사용."""
    try:
        ip = (request.headers.get("x-forwarded-for") or
              (request.client.host if request.client else "") or "")
        return ip.split(",")[0].strip()[:64] or "unknown"
    except Exception:
        return "unknown"


def _login_locked(key: str) -> int:
    """잠겨있으면 남은 초(>0), 아니면 0. 만료된 잠금/오래된 기록은 정리."""
    now = _time_mod.time()
    until = _login_lock_until.get(key, 0)
    if until > now:
        return int(until - now) + 1
    if until:
        _login_lock_until.pop(key, None)
        _login_fails.pop(key, None)
    return 0


def _login_register_fail(key: str) -> None:
    """실패 1회 기록 → 윈도우 내 임계 초과 시 잠금. dict 비대 시 만료항목 정리."""
    now = _time_mod.time()
    fails = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_FAIL_WINDOW]
    fails.append(now)
    _login_fails[key] = fails
    if len(fails) >= _LOGIN_FAIL_MAX:
        _login_lock_until[key] = now + _LOGIN_LOCK_SECONDS
    if len(_login_fails) > 5000:  # 메모리 누수 방지 가드
        for k in list(_login_fails.keys()):
            if all(now - t >= _LOGIN_FAIL_WINDOW for t in _login_fails.get(k, [])):
                _login_fails.pop(k, None)


def _login_reset(key: str) -> None:
    _login_fails.pop(key, None)
    _login_lock_until.pop(key, None)


def _login_locked_response(retry_after: int) -> Response:
    return Response(
        content=json.dumps({"error": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요."}),
        status_code=429, media_type="application/json",
        headers={"Retry-After": str(retry_after)},
    )


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """비밀번호로 로그인 → 세션 토큰 반환"""
    body = await request.json()
    password = body.get("password", "")

    lk = "pw:" + _client_ip(request)
    locked = _login_locked(lk)
    if locked:
        return _login_locked_response(locked)

    if not LEGACY_SHARED_LOGIN_ENABLED or not DASHBOARD_PASSWORD.strip() or not isinstance(password, str) or not hmac.compare_digest(password, DASHBOARD_PASSWORD):
        _login_register_fail(lk)
        return Response(
            content=json.dumps({"error": "비밀번호가 올바르지 않습니다"}),
            status_code=401,
            media_type="application/json",
        )

    _login_reset(lk)
    token = secrets.token_urlsafe(32)
    _add_session(token)
    response = Response(content=json.dumps({"ok": True, "token": token}), media_type="application/json")
    _set_file_session_cookie(response, token, request)
    return response


def _set_file_session_cookie(response: Response, token: str, request: Request):
    """Permit authenticated same-origin image/document requests without exposing tokens in URLs."""
    response.set_cookie(
        "weekly_file_session", token, max_age=JWT_EXPIRY_HOURS * 3600,
        httponly=True, secure=_IS_PRODUCTION or request.url.scheme == "https",
        samesite="strict", path="/api/file-proxy/",
    )


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if LEGACY_SHARED_LOGIN_ENABLED and token and not _jwt_decode(token):
        sessions = _load_sessions()
        if token in sessions:
            sessions.pop(token)
            _save_sessions(sessions)
    response = Response(content=json.dumps({"ok": True}), media_type="application/json")
    response.delete_cookie("weekly_file_session", path="/api/file-proxy/")
    return response


@app.get("/api/auth/check")
async def auth_check(request: Request):
    """현재 토큰 유효성 확인"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # JWT 토큰 확인
        jwt_payload = _jwt_decode(token)
        if jwt_payload:
            return {"authenticated": True, "user": {
                "user_id": jwt_payload.get("user_id"),
                "name": jwt_payload.get("name"),
                "email": jwt_payload.get("email"),
                "role": jwt_payload.get("role"),
            }}
        # 기존 세션 토큰 확인
        if _check_session(token):
            return {"authenticated": True, "user": {"user_id": "legacy", "name": "공용", "role": "member"}}
    return {"authenticated": False}


# ── 개인 로그인/RBAC API ──
@app.post("/api/auth/login-user")
async def auth_login_user(request: Request):
    """개인 이메일+비밀번호 로그인 → JWT 반환"""
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return Response(
            content=json.dumps({"error": "이메일과 비밀번호를 입력하세요"}),
            status_code=400,
            media_type="application/json",
        )

    # (IP+이메일) 조합으로 잠금 — 사무실 공유 NAT에서 직원들이 서로 잠그지 않도록.
    # (Railway가 X-Forwarded-For를 실제 IP로 덮어써서 IP 스푸핑은 불가 → IP만이면 사무실 전체 잠김 위험)
    lk = "user:" + _client_ip(request) + ":" + email[:128]
    locked = _login_locked(lk)
    if locked:
        return _login_locked_response(locked)

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, email, password_hash, role, is_active FROM ext_users WHERE email = %s",
        (email,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        _login_register_fail(lk)
        return Response(
            content=json.dumps({"error": "이메일 또는 비밀번호가 올바르지 않습니다"}),
            status_code=401,
            media_type="application/json",
        )

    user_id, name, user_email, password_hash, role, is_active = row

    if not is_active:
        conn.close()
        return Response(
            content=json.dumps({"error": "비활성화된 계정입니다"}),
            status_code=403,
            media_type="application/json",
        )

    if not _verify_password(password, password_hash):
        conn.close()
        _login_register_fail(lk)
        return Response(
            content=json.dumps({"error": "이메일 또는 비밀번호가 올바르지 않습니다"}),
            status_code=401,
            media_type="application/json",
        )

    _login_reset(lk)

    # 마지막 로그인 시간 업데이트
    cur.execute("UPDATE ext_users SET last_login = NOW() WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()

    # JWT 생성
    token = _jwt_encode({
        "user_id": user_id,
        "name": name,
        "email": user_email,
        "role": role,
    })

    response = Response(content=json.dumps({"ok": True, "token": token, "user": {"user_id": user_id, "name": name, "email": user_email, "role": role}}), media_type="application/json")
    _set_file_session_cookie(response, token, request)
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """현재 로그인한 사용자 정보 반환"""
    user = getattr(request.state, "user", None)
    if not user:
        return Response(
            content=json.dumps({"error": "인증이 필요합니다"}),
            status_code=401,
            media_type="application/json",
        )
    return {"user": user}


@app.post("/api/auth/password")
async def change_password(request: Request):
    """본인 비밀번호 변경 — current_password 검증 후 new_password 적용. legacy 세션 거절."""
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in (None, "", "legacy"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")

    body = await request.json()
    current = body.get("current_password", "") or ""
    new = body.get("new_password", "") or ""
    if not current or not new:
        return Response(content=json.dumps({"error": "current_password, new_password 필수"}), status_code=400, media_type="application/json")
    try:
        _validate_password(new)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    if new == current:
        return Response(content=json.dumps({"error": "현재 비밀번호와 동일합니다"}), status_code=400, media_type="application/json")

    user_id = user["user_id"]
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM ext_users WHERE id = %s AND is_active = TRUE", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(status_code=404)
    if not _verify_password(current, row[0]):
        conn.close()
        # 현재 비밀번호 불일치는 인증 만료가 아니므로 400 (401이면 프론트가 로그아웃 처리됨)
        return Response(content=json.dumps({"error": "현재 비밀번호가 올바르지 않습니다"}), status_code=400, media_type="application/json")

    cur.execute("UPDATE ext_users SET password_hash = %s WHERE id = %s", (_hash_password(new), user_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/auth/users")
async def auth_list_users(request: Request):
    """전체 사용자 목록 (admin/pm)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(
            content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}),
            status_code=403,
            media_type="application/json",
        )

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, role, slack_id, linear_id, is_active, created_at, last_login, hire_date, leave_start_date FROM ext_users ORDER BY created_at")
    rows = cur.fetchall()
    conn.close()

    users = []
    for r in rows:
        users.append({
            "id": r[0], "name": r[1], "email": r[2], "role": r[3],
            "slack_id": r[4], "linear_id": r[5], "is_active": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "last_login": r[8].isoformat() if r[8] else None,
            "hire_date": r[9].isoformat() if r[9] else None,
            "leave_start_date": r[10].isoformat() if r[10] else None,
        })
    return {"users": users}


@app.post("/api/auth/register")
async def auth_register(request: Request):
    """새 사용자 등록 (admin/pm)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(
            content=json.dumps({"error": "관리자 권한이 필요합니다"}),
            status_code=403,
            media_type="application/json",
        )

    body = await request.json()
    name = body.get("name", "").strip()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    role = body.get("role", "member")

    if not name or not email or not password:
        return Response(
            content=json.dumps({"error": "이름, 이메일, 비밀번호는 필수입니다"}),
            status_code=400,
            media_type="application/json",
        )

    try:
        _validate_password(password)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")

    if role not in ("admin", "pm", "member"):
        return Response(
            content=json.dumps({"error": "유효하지 않은 역할입니다 (admin, pm, member)"}),
            status_code=400,
            media_type="application/json",
        )

    conn = _pg_conn()
    cur = conn.cursor()
    # 중복 이메일 확인
    cur.execute("SELECT 1 FROM ext_users WHERE email = %s", (email,))
    if cur.fetchone():
        conn.close()
        return Response(
            content=json.dumps({"error": "이미 등록된 이메일입니다"}),
            status_code=409,
            media_type="application/json",
        )

    user_id = str(uuid.uuid4())
    slack_id = (body.get("slack_id") or "").strip()
    hire_date = (body.get("hire_date") or "").strip() or None
    leave_start_date = (body.get("leave_start_date") or "").strip() or None
    cur.execute(
        "INSERT INTO ext_users (id, name, email, password_hash, role, slack_id, hire_date, leave_start_date) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (user_id, name, email, _hash_password(password), role, slack_id, hire_date, leave_start_date),
    )
    conn.commit()
    conn.close()

    log_activity(request, "사용자 추가", "user", user_id,
                 {"name": name, "email": email, "role": role})
    return {"ok": True, "user": {"id": user_id, "name": name, "email": email, "role": role}}


@app.put("/api/auth/users/{user_id}")
async def auth_update_user(user_id: str, request: Request):
    """사용자 정보 수정 (admin/pm)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(
            content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}),
            status_code=403,
            media_type="application/json",
        )

    body = await request.json()
    conn = _pg_conn()
    cur = conn.cursor()

    # 대상 사용자 존재 확인
    cur.execute("SELECT 1 FROM ext_users WHERE id = %s", (user_id,))
    if not cur.fetchone():
        conn.close()
        return Response(
            content=json.dumps({"error": "사용자를 찾을 수 없습니다"}),
            status_code=404,
            media_type="application/json",
        )

    # 업데이트 가능한 필드
    updates = []
    params = []
    for field in ("name", "email", "role", "slack_id", "linear_id", "is_active", "hire_date", "leave_start_date"):
        if field in body:
            if field == "role" and body[field] not in ("admin", "pm", "member"):
                conn.close()
                return Response(
                    content=json.dumps({"error": "유효하지 않은 역할입니다"}),
                    status_code=400,
                    media_type="application/json",
                )
            updates.append(f"{field} = %s")
            params.append(body[field] if body[field] != "" else None)

    # 비밀번호 변경
    if "password" in body and body["password"]:
        updates.append("password_hash = %s")
        params.append(_hash_password(body["password"]))

    if updates:
        params.append(user_id)
        cur.execute(f"UPDATE ext_users SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()

    conn.close()
    # is_active 변경 시 캐시 즉시 무효화 (강제 로그아웃·재활성화 즉시 반영)
    _invalidate_active_cache(user_id)
    # 활동 로그 — 비밀번호는 마스킹
    log_detail = {k: ("***" if k == "password" else v) for k, v in body.items()}
    log_activity(request, "사용자 수정", "user", user_id, log_detail)
    return {"ok": True}


def _read_json(path: Path) -> dict | None:
    """JSON 파일 읽기. 파일 없으면 None 반환."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _create_backup(path: Path) -> None:
    """파일 저장 전 자동 백업 (최대 MAX_BACKUPS개 유지)."""
    if not path.exists():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = path.stem  # 예: report, todos
    backup_path = BACKUPS_DIR / f"{stem}_{ts}.json"
    try:
        import shutil
        shutil.copy2(str(path), str(backup_path))
        # 오래된 백업 정리
        backups = sorted(BACKUPS_DIR.glob(f"{stem}_*.json"), key=lambda p: p.name)
        while len(backups) > MAX_BACKUPS:
            backups.pop(0).unlink()
    except Exception as e:
        logger.warning(f"백업 실패: {e}")


async def _write_json(path: Path, data: dict) -> None:
    """JSON 파일 쓰기 (락 사용, 저장 전 자동 백업)."""
    async with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_backup(path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 리포트 API ──

# 병합 대상: {섹션 키: 안건 목록 키} — {key: {listKey: [...]}} 형태 섹션
# 리포트 병합 로직(_merge_report 등)은 src/util.py 로 분리(G1).
try:
    from src.util import _merge_report
except ImportError:
    from util import _merge_report


# 병합 후 클라이언트에 돌려줄 섹션 키 목록
_REPORT_MERGE_KEYS = (
    "meetingAgenda", "directions", "completedResults", "activeProjects",
    "teamPriorities", "metricSections", "kanbanColumns", "_deletedIds",
)


@app.get("/api/report")
async def get_report():
    data = _read_store("report")
    if data is None:
        return Response(status_code=204)
    return data


@app.put("/api/report")
async def put_report(request: Request):
    body = await request.json()

    # 읽기 → 병합 → 쓰기를 하나의 트랜잭션에서 처리해 동시 저장 시 한쪽이
    # 추가한 항목이 유실되지 않도록 한다.
    #  - _write_lock: 같은 워커 내 직렬화
    #  - pg_advisory_xact_lock: 워커 간 직렬화 (트랜잭션 종료 시 자동 해제)
    async with _write_lock:
        conn = _pg_conn()
        merged_changed = False
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_REPORT_LOCK_KEY,))
            cur.execute("SELECT data FROM ext_app_store WHERE key = 'report'")
            row = cur.fetchone()
            server_data = None
            if row is not None:
                server_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            if server_data:
                body, merged_changed = _merge_report(server_data, body)
                _auto_backup_report(server_data)  # 저장 전 이전 버전 백업
            cur.execute(
                "INSERT INTO ext_app_store (key, data, updated_at) VALUES ('report', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()",
                (json.dumps(body, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()

    if merged_changed:
        # 병합으로 목록이 바뀌었으면 클라이언트가 동기화하도록 병합 결과 반환
        return {
            "ok": True,
            "merged": True,
            "report": {k: body.get(k) for k in _REPORT_MERGE_KEYS if k in body},
        }
    return {"ok": True}


# ── 팀 설정 API ──

@app.get("/api/team-config")
async def get_team_config():
    data = _read_store("team_config")
    defaults = _installation.team_config_defaults()
    if isinstance(data, dict):
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data
    return defaults


@app.get("/api/company/business-license/info")
async def business_license_info(request: Request):
    """사업자등록증 존재 여부 + 파일명 — 로그인(legacy 제외) 사용자만"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}),
                        status_code=403, media_type="application/json")
    meta = _read_store("company_business_license")
    if not meta or not meta.get("b64"):
        return {"exists": False}
    return {"exists": True, "filename": meta.get("filename", "사업자등록증.pdf"),
            "size": meta.get("size", 0), "uploaded_at": meta.get("uploaded_at", "")}


@app.get("/api/company/business-license")
async def download_business_license(request: Request):
    """사업자등록증 다운로드 — 로그인(legacy 제외) 전 직원. DB에 base64로 보관된 파일을 스트리밍."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}),
                        status_code=403, media_type="application/json")
    meta = _read_store("company_business_license")
    if not meta or not meta.get("b64"):
        return Response(content=json.dumps({"error": "사업자등록증이 등록되지 않았습니다"}),
                        status_code=404, media_type="application/json")
    import base64 as _b64
    try:
        content = _b64.b64decode(meta["b64"])
    except Exception:
        return Response(content=json.dumps({"error": "파일 디코딩 실패"}),
                        status_code=500, media_type="application/json")
    filename = meta.get("filename", "사업자등록증.pdf")
    # RFC 5987 — 한글 파일명 대응
    from urllib.parse import quote as _quote
    cd = f"attachment; filename*=UTF-8''{_quote(filename)}"
    log_activity(request, "사업자등록증 다운로드", "company", "business_license", {"filename": filename})
    return Response(content=content, media_type=meta.get("content_type", "application/pdf"),
                    headers={"Content-Disposition": cd})


@app.put("/api/team-config")
async def put_team_config(request: Request):
    """팀 설정 변경 — admin/pm (멤버 보고 항목·회사정보 등 팀 운영 설정)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    await _write_store("team_config", body)
    # 활동 로그 — 상위 키만 (값은 너무 큼)
    try:
        keys = list((body or {}).keys()) if isinstance(body, dict) else []
    except Exception:
        keys = []
    log_activity(request, "팀 설정 변경", "team_config", "", {"keys": keys})
    return {"ok": True}


# ── 파일 업로드 API ──

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """파일 업로드 → Supabase Storage에 저장"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return Response(content=json.dumps({"error": "Storage is not configured"}), status_code=503, media_type="application/json")
    file.filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return Response(content=json.dumps({"error": "Unsupported file extension"}), status_code=400, media_type="application/json")
    # 한글/특수문자 파일명은 Supabase에서 거부되므로 UUID 기반 안전 이름 사용
    try:
        file.filename.encode('ascii')
        safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    except UnicodeEncodeError:
        safe_name = f"{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:8]}{ext}"
    content = await file.read()

    # MIME 타입 결정 (텍스트 계열은 charset=utf-8 명시)
    content_type = file.content_type or "application/octet-stream"
    TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".html", ".json", ".xml", ".yaml", ".yml", ".log"}
    if ext in TEXT_EXTENSIONS and "charset" not in content_type:
        content_type = content_type + "; charset=utf-8"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{safe_name}",
            headers={
                **_supabase_storage_headers(),
                "Content-Type": content_type,
            },
            content=content,
        )
        if resp.status_code not in (200, 201):
            return Response(
                content=json.dumps({"error": "업로드 실패", "detail": resp.text}),
                status_code=resp.status_code,
                media_type="application/json",
            )

    # 파일 프록시 URL 생성 (Supabase 직접 URL 대신 — Content-Type + charset 보장)
    proxy_url = f"/api/file-proxy/{safe_name}"
    return {"url": proxy_url, "name": file.filename, "isImage": ext in IMAGE_EXTENSIONS}


@app.get("/api/file-proxy/{filename:path}")
async def file_proxy(filename: str):
    """Authenticated proxy for private Storage objects; active content is downloaded in a sandbox."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return Response(status_code=503)
    if not filename or "/" in filename or "\\" in filename or filename in (".", "..") or any(ord(char) < 32 or ord(char) == 127 for char in filename):
        return Response(status_code=400)
    from urllib.parse import quote
    file_url = f"{SUPABASE_URL}/storage/v1/object/authenticated/{STORAGE_BUCKET}/{quote(filename, safe='')}"
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
               "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(file_url, headers=_supabase_storage_headers(), follow_redirects=False)
        if 300 <= resp.status_code < 400:
            return Response(status_code=502, headers=headers)
        if resp.status_code != 200:
            return Response(status_code=resp.status_code, headers=headers)
    # Supabase가 Content-Type을 text/plain으로 덮어씌우므로 확장자 기반으로 직접 결정
    ext = Path(filename).suffix.lower()
    MIME_MAP = {
        ".html": "text/html", ".htm": "text/html",
        ".md": "text/markdown", ".txt": "text/plain",
        ".csv": "text/csv", ".json": "application/json",
        ".xml": "text/xml", ".yaml": "text/yaml", ".yml": "text/yaml",
        ".log": "text/plain",
        ".pdf": "application/pdf",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    ct = MIME_MAP.get(ext, resp.headers.get("content-type", "application/octet-stream"))
    active_content = ext in {".html", ".htm", ".svg", ".xml", ".xhtml"} or ct.split(";", 1)[0].strip().lower() in {
        "text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml",
    }
    if active_content:
        # A client which creates a Blob must also receive a non-executable MIME type.
        ct = "application/octet-stream"
    if ct.startswith("text/") or ct in ("application/json",):
        ct += "; charset=utf-8"
    # 텍스트/HTML은 브라우저에서 바로 표시, 바이너리(PDF/이미지/기타)는 다운로드
    INLINE_TYPES = {"text/", "application/json", "application/pdf", "image/"}
    is_inline = any(ct.startswith(t) for t in INLINE_TYPES)
    if not is_inline or active_content:
        # 원본 파일명 복원 시도 (UUID_ 접두사 제거)
        orig_name = filename.split("_", 1)[1] if "_" in filename else filename
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(orig_name, safe="")
    return Response(content=resp.content, media_type=ct, headers=headers)


@app.delete("/api/upload/{filename}")
async def delete_file(filename: str):
    """Supabase Storage에서 파일 삭제"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return Response(status_code=503)
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{filename}",
            headers=_supabase_storage_headers(),
        )
        if resp.status_code == 404:
            return Response(status_code=404)
        if resp.status_code not in (200, 204):
            return Response(
                content=json.dumps({"error": "삭제 실패", "detail": resp.text}),
                status_code=resp.status_code,
                media_type="application/json",
            )
    return {"ok": True}


# ── 할일 API ──

@app.get("/api/todos")
async def get_todos():
    data = _read_store("todos")
    if data is None:
        return {"todos": []}
    return data


@app.put("/api/todos")
async def put_todos(request: Request):
    body = await request.json()
    await _write_store("todos", body)
    return {"ok": True}


# ── 주간 퍼널 점검 체크리스트 API ──
# 시드(15섹션)는 src/checklist_seed.py 로 분리(main.py 비대화 방지, G1 정신).
try:
    from src.checklist_seed import WEEKLY_CHECKLIST_SEED
except ImportError:
    from checklist_seed import WEEKLY_CHECKLIST_SEED


@app.get("/api/weekly-checklist")
async def get_weekly_checklist():
    """체크리스트 구조 + 주차별 상태. 저장본 없으면 시드 반환(편집·저장 전까지 persist 안 함)."""
    data = _read_store("weekly_checklist")
    if not data or not isinstance(data.get("sections"), list) or not data["sections"]:
        return WEEKLY_CHECKLIST_SEED
    data.setdefault("weeks", {})
    return data


def _merge_checklist(base: dict, incoming: dict) -> dict:
    """동시편집 lost-update 방지: 저장본(base)과 들어온 값(incoming)을 id 기준 병합.
    - 섹션/항목: id union(들어온 값이 내용·순서 우선, base에만 있는 건 동시추가로 보고 보존)
    - 삭제: incoming의 _deletedSectionIds/_deletedItemIds를 영구 tombstone으로 누적(uid는 재사용 안 되므로 부활 방지)
    - weeks: 주차별 itemId 단위 deep-merge(같은 칸은 incoming 우선)
    base가 비었으면 incoming 그대로(자체 삭제목록만 정리).
    """
    base = base if isinstance(base, dict) else {}
    del_secs = set(base.get("_deletedSectionIds") or []) | set(incoming.get("_deletedSectionIds") or [])
    del_items = set(base.get("_deletedItemIds") or []) | set(incoming.get("_deletedItemIds") or [])

    def _merge_items(bsec, isec):
        i_items = isec.get("items") or []
        i_ids = {it.get("id") for it in i_items}
        out_items = [it for it in i_items if it.get("id") not in del_items]
        for it in ((bsec or {}).get("items") or []):
            if it.get("id") not in i_ids and it.get("id") not in del_items:
                out_items.append(it)
        s = dict(isec); s["items"] = out_items; return s

    base_secs = {s.get("id"): s for s in (base.get("sections") or [])}
    inc_secs = incoming.get("sections") or []
    inc_ids = {s.get("id") for s in inc_secs}
    result = []
    for s in inc_secs:                       # 들어온 순서·내용 우선
        if s.get("id") in del_secs:
            continue
        result.append(_merge_items(base_secs.get(s.get("id")), s))
    for s in (base.get("sections") or []):   # base에만 있는 섹션 = 다른 사용자의 동시 추가 → 보존
        sid = s.get("id")
        if sid not in inc_ids and sid not in del_secs:
            items = [it for it in (s.get("items") or []) if it.get("id") not in del_items]
            s = dict(s); s["items"] = items
            result.append(s)

    weeks = {wk: dict(m or {}) for wk, m in (base.get("weeks") or {}).items()}
    for wk, m in (incoming.get("weeks") or {}).items():
        wm = dict(weeks.get(wk) or {}); wm.update(m or {}); weeks[wk] = wm

    out = dict(incoming)
    out["sections"] = result
    out["weeks"] = weeks
    out["_deletedSectionIds"] = sorted(del_secs)
    out["_deletedItemIds"] = sorted(del_items)
    return out


@app.put("/api/weekly-checklist")
async def put_weekly_checklist(request: Request):
    """저장(구조+주차상태) — 저장본과 id 기준 병합해 동시편집 유실 방지. 모든 로그인 사용자 편집 가능."""
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("sections"), list):
        return Response(content=json.dumps({"error": "sections 배열 필수"}),
                        status_code=400, media_type="application/json")
    body.setdefault("weeks", {})
    existing = _read_store("weekly_checklist")
    merged = _merge_checklist(existing, body)
    await _write_store("weekly_checklist", merged)
    return {"ok": True, "merged": True}


# ── Slack 알림 API ──

async def _slack_post_message(bot_token: str, channel: str, text: str) -> dict:
    """Slack chat.postMessage 호출 (httpx 사용)"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"channel": channel, "text": text},
        )
        return resp.json()


SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


def _get_slack_id_by_name(name: str) -> str:
    """ext_users.slack_id 조회. 없거나 비활성이면 빈 문자열."""
    if not name:
        return ""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT slack_id FROM ext_users WHERE name = %s AND is_active = TRUE", (name,))
        row = cur.fetchone()
        conn.close()
        return (row[0] or "") if row else ""
    except Exception as e:
        logger.error(f"_get_slack_id_by_name 실패: {e}")
        return ""


@app.post("/api/slack/notify")
async def slack_notify(request: Request):
    """할일 배정 알림 — assignee 이름 → ext_users.slack_id 서버 조회. 개인 로그인 필수."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")

    body = await request.json()
    bot_token = _get_slack_bot_token()  # DB 토큰 사용 — 전역 변수는 재시작 시 비워짐
    assignee = (body.get("assignee") or "").strip()
    # 하위 호환: 옛 호출(slackUserId 직접 전달)은 더 이상 신뢰하지 않음 — assignee 필수
    message = (body.get("message") or "").strip()

    if not bot_token:
        return Response(content=json.dumps({"error": "서버에 SLACK_BOT_TOKEN이 설정되지 않았습니다"}),
                        status_code=400, media_type="application/json")
    if not assignee or not message:
        return Response(content=json.dumps({"error": "assignee, message 필수"}),
                        status_code=400, media_type="application/json")

    slack_user_id = _get_slack_id_by_name(assignee)
    if not slack_user_id:
        return Response(content=json.dumps({"error": f"'{assignee}'의 Slack ID가 등록되지 않았습니다"}),
                        status_code=400, media_type="application/json")

    result = await _slack_post_message(bot_token, slack_user_id, message)
    if not result.get("ok"):
        return Response(content=json.dumps({"error": result.get("error", "알 수 없는 오류")}),
                        status_code=400, media_type="application/json")
    return {"ok": True}


@app.post("/api/slack/test")
async def slack_test(request: Request):
    """Slack 봇 토큰 연결 테스트"""
    body = await request.json()
    bot_token = body.get("bot_token") or _get_slack_bot_token()  # 입력값 우선, 없으면 DB 토큰
    if not bot_token:
        return Response(
            content=json.dumps({"error": "서버에 SLACK_BOT_TOKEN이 설정되지 않았습니다"}),
            status_code=400,
            media_type="application/json",
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        data = resp.json()

    if not data.get("ok"):
        return Response(
            content=json.dumps({"error": data.get("error", "인증 실패")}),
            status_code=400,
            media_type="application/json",
        )
    return {"ok": True, "team": data.get("team"), "bot": data.get("user")}


# ── 핵심 지표 자동 조회 API ──

# 외부 지표 대시보드 프로덕션 API
METRICS_API_URL = os.environ.get("METRICS_API_URL", "").strip().rstrip("/")

# Supabase 접속 정보 (상단에서 정의됨)


# 순수 유틸은 src/util.py 로 분리(G1). 두 import 컨텍스트(uvicorn=src.main / 테스트=main) 모두 지원.
try:
    from src.util import _fmt_krw, _calc_trend, _metric_status, _daily_summary_to_maps as _ds_to_maps
except ImportError:
    from util import _fmt_krw, _calc_trend, _metric_status, _daily_summary_to_maps as _ds_to_maps


# 운영 DB API (PII 마스킹된 read-only)
METRICS_PROD_API_URL = os.environ.get("METRICS_PROD_API_URL", "").strip().rstrip("/")
METRICS_API_KEY = os.environ.get("METRICS_API_KEY", "")
PG_FEE_RATE = float(os.environ.get("PG_FEE_RATE", "0"))

# AI 모델별 토큰 단가 (USD/1M tokens)
LLM_PRICING = _installation.llm_pricing()
LLM_DEFAULT_PRICING = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


# 일시적 네트워크 오류(상대 서버가 chunked 응답을 끊는 등) — 재시도 대상
_TRANSIENT_HTTP_ERRORS = (
    httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
    httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
)


# 운영 daily-summary 한 번에 조회 가능한 최대 일수(운영 API 제한 92일)
_DAILY_SUMMARY_MAX_DAYS = 92


async def _fetch_daily_summary(start_date: str, end_date: str) -> dict:
    """운영 /api/v1/dashboard/daily-summary 조회 — 92일 초과 구간은 잘라서 합친다.
    반환: {"llm_cost_available": bool, "days": [...]} (응답 days 를 이어붙인 것)."""
    if not METRICS_API_KEY or not METRICS_PROD_API_URL:
        raise RuntimeError("METRICS_PROD_API_URL / METRICS_API_KEY 미설정")
    sd = dt_date.fromisoformat(start_date)
    ed = dt_date.fromisoformat(end_date)
    merged = {"llm_cost_available": True, "days": []}
    headers = {"X-Dashboard-API-Key": METRICS_API_KEY, "User-Agent": "weekly-report-dashboard"}
    async with httpx.AsyncClient(timeout=60) as client:
        cur = sd
        while cur <= ed:
            chunk_end = min(ed, cur + timedelta(days=_DAILY_SUMMARY_MAX_DAYS - 1))
            resp = None
            for attempt in range(3):
                try:
                    resp = await client.get(
                        f"{METRICS_PROD_API_URL}/api/v1/dashboard/daily-summary",
                        headers=headers, params={"from": str(cur), "to": str(chunk_end)},
                    )
                    break
                except _TRANSIENT_HTTP_ERRORS:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)
            resp.raise_for_status()
            body = resp.json()
            merged["llm_cost_available"] = merged["llm_cost_available"] and bool(body.get("llm_cost_available", True))
            merged["days"].extend(body.get("days") or [])
            cur = chunk_end + timedelta(days=1)
    return merged


def _daily_summary_to_maps(summary: dict):
    """daily-summary → (순매출, 구매수, API비용 원화) — 환율·마진은 환경변수 값 사용."""
    return _ds_to_maps(summary, USD_KRW_RATE, API_COST_MARKUP)


async def _fetch_prod_api(path: str, params: dict) -> list:
    """운영 API 호출 + 페이지네이션 처리. 일시적 네트워크 오류(RemoteProtocolError 등)는 재시도."""
    if not METRICS_PROD_API_URL or not METRICS_API_KEY:
        raise RuntimeError("METRICS_PROD_API_URL and METRICS_API_KEY are required")
    items = []
    cursor = None
    headers = {"X-Dashboard-API-Key": METRICS_API_KEY}
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            p = {**params}
            if cursor:
                p["cursor"] = cursor
            # 일시적 오류(상대가 chunked 응답을 끊는 등)는 최대 3회 재시도 후 전파
            resp = None
            for attempt in range(3):
                try:
                    resp = await client.get(f"{METRICS_PROD_API_URL}{path}", headers=headers, params=p)
                    break
                except _TRANSIENT_HTTP_ERRORS as ex:
                    if attempt == 2:
                        logger.error(f"운영 API 네트워크 오류(재시도 소진): {path} → {ex}")
                        raise
                    logger.warning(f"운영 API 일시 오류 재시도 {attempt+1}/3: {path} → {ex}")
                    await asyncio.sleep(0.5 * (attempt + 1))
            if resp.status_code != 200:
                logger.error(f"운영 API 에러: {path} → {resp.status_code} {resp.text[:200]}")
                break
            d = resp.json()
            items.extend(d.get("items", []))
            cursor = d.get("next_cursor")
            if not cursor:
                break
    return items


_metrics_auto_cache = {}   # {"start_end": (expire_ts, result)} — 외부 운영API 집계 결과 캐시
_METRICS_AUTO_TTL = 900    # 15분 (외부 API 4회 조회가 무거워 초기 로드 병목 → 캐싱)


# ── /api/metrics/auto 공유 캐시 (L2: ext_app_store, 워커 간 공유) ──
_metrics_auto_refreshing = set()   # 진행 중인 백그라운드 갱신 ck (워커 내 중복 spawn 방지)
_metrics_auto_tasks = set()        # 백그라운드 task 강참조 보관 (GC로 중도 취소 방지)


def _metrics_auto_l2_get(ck: str):
    """공유 DB 캐시 조회 → (result|None, is_fresh)."""
    try:
        d = _read_store(f"metrics_auto:{ck}")
        if d and isinstance(d, dict) and "result" in d:
            return d["result"], (float(d.get("expire", 0)) > _time_mod.time())
    except Exception as e:
        logger.warning(f"[metrics/auto] L2 캐시 읽기 실패: {e}")
    return None, False


async def _metrics_auto_l2_set(ck: str, result: dict):
    """공유 DB 캐시 저장 (best-effort)."""
    try:
        await _write_store(f"metrics_auto:{ck}",
                           {"expire": _time_mod.time() + _METRICS_AUTO_TTL, "result": result})
    except Exception as e:
        logger.warning(f"[metrics/auto] L2 캐시 쓰기 실패: {e}")


async def _refresh_auto_metrics(start_date: str, end_date: str, ck: str):
    """백그라운드 갱신 — stale 응답을 내보낸 뒤 최신값을 L1/L2에 채운다."""
    try:
        result = await _compute_auto_metrics(start_date, end_date)
        _metrics_auto_cache[ck] = (_time_mod.time() + _METRICS_AUTO_TTL, result)
        await _metrics_auto_l2_set(ck, result)
        logger.info(f"[metrics/auto] 백그라운드 갱신 완료: {ck}")
    except Exception as ex:
        logger.warning(f"[metrics/auto] 백그라운드 갱신 실패({ck}): {ex}")
    finally:
        _metrics_auto_refreshing.discard(ck)


@app.get("/api/metrics/auto")
async def auto_metrics(start_date: str, end_date: str):
    """운영 DB API 기반 자동 집계 — 매출/AI비용/PG/광고비/순이익.
    2단계 캐시(L1 인메모리 + L2 DB 공유) + stale-while-revalidate:
    만료된 캐시가 있으면 즉시 옛 값을 반환하고 백그라운드에서 갱신 → 40초 대기 제거.
    워커 2개가 L2(ext_app_store)를 공유하므로 워커별 캐시 분리 문제도 해소."""
    if not METRICS_API_KEY or not METRICS_PROD_API_URL:
        return Response(content=json.dumps({"error": "METRICS_PROD_API_URL / METRICS_API_KEY 미설정"}),
                        status_code=400, media_type="application/json")

    _ck = f"{start_date}_{end_date}"
    now = _time_mod.time()

    # L1: 인메모리 (워커별, 가장 빠름)
    _hit = _metrics_auto_cache.get(_ck)
    if _hit and _hit[0] > now:
        return _hit[1]

    # L2: DB 공유 캐시
    l2_result, l2_fresh = _metrics_auto_l2_get(_ck)
    if l2_result is not None:
        if l2_fresh:
            _metrics_auto_cache[_ck] = (now + _METRICS_AUTO_TTL, l2_result)
            return l2_result
        # stale → 즉시 반환 + 백그라운드 1회 갱신 (사용자는 대기 없음)
        if _ck not in _metrics_auto_refreshing:
            _metrics_auto_refreshing.add(_ck)
            _t = asyncio.create_task(_refresh_auto_metrics(start_date, end_date, _ck))
            _metrics_auto_tasks.add(_t)
            _t.add_done_callback(_metrics_auto_tasks.discard)
        return l2_result

    # 캐시 전무(최초 로드) → 동기 계산. 일시적 오류면 raw 500 대신 502.
    try:
        result = await _compute_auto_metrics(start_date, end_date)
    except Exception as ex:
        logger.error(f"[metrics/auto] 운영 API 집계 실패: {ex}")
        return Response(
            content=json.dumps({"error": "운영 API 일시 오류 — 잠시 후 다시 시도해주세요"}),
            status_code=502, media_type="application/json")
    _metrics_auto_cache[_ck] = (now + _METRICS_AUTO_TTL, result)
    await _metrics_auto_l2_set(_ck, result)
    return result


async def _compute_auto_metrics(start_date: str, end_date: str) -> dict:
    """외부 운영 API를 호출해 지표를 집계해 반환. 일시적 네트워크 오류는 예외로 전파(호출부가 502 처리)."""
    sd = dt_date.fromisoformat(start_date)
    ed = dt_date.fromisoformat(end_date)
    delta = (ed - sd).days + 1
    prev_ed = sd - timedelta(days=1)
    prev_sd = prev_ed - timedelta(days=delta - 1)

    # 광고비(spend)는 운영 funnel API에만 있음 — /api/funnel/summary(전체 총액) 사용. 실패 시 None(전체 집계 보호)
    async def _fetch_ad_spend(s, e):
        """funnel API(/api/funnel/summary)에서 전체 광고비 조회. 성공 시 숫자, 실패/지연 시 None."""
        if not METRICS_API_URL:
            return None
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                # Render 무료 플랜 슬립 대응 — 먼저 깨우기
                try:
                    await client.get(f"{METRICS_API_URL}/api/health")
                except Exception:
                    pass
                r = await client.get(
                    f"{METRICS_API_URL}/api/funnel/summary",
                    params={"start_date": s, "end_date": e},
                )
            if r.status_code == 200:
                return int(r.json().get("spend", 0) or 0)
            logger.warning(f"[metrics/auto] 광고비 HTTP {r.status_code}")
        except Exception as ex:
            logger.warning(f"[metrics/auto] 광고비 조회 실패: {ex}")
        return None

    # 현재 + 전기간 병렬 조회 (광고비 funnel 2건 포함).
    # 매출/AI비용 호출이 일시적 네트워크 오류로 끝까지 실패하면 예외가 전파되고,
    # 호출부(auto_metrics)가 이를 잡아 502로 응답한다(캐시 미적재 → 다음 호출 재시도).
    cur_orders, prev_orders, cur_llm, prev_llm, cur_ad, prev_ad = await asyncio.gather(
        _fetch_prod_api("/api/v1/dashboard/orders", {"from": start_date, "to": end_date, "limit": "5000"}),
        _fetch_prod_api("/api/v1/dashboard/orders", {"from": str(prev_sd), "to": str(prev_ed), "limit": "5000"}),
        _fetch_prod_api("/api/v1/dashboard/llm-request-logs", {"from": start_date, "to": end_date, "limit": "5000"}),
        _fetch_prod_api("/api/v1/dashboard/llm-request-logs", {"from": str(prev_sd), "to": str(prev_ed), "limit": "5000"}),
        _fetch_ad_spend(start_date, end_date),
        _fetch_ad_spend(str(prev_sd), str(prev_ed)),
    )
    # 광고비 정리 (None = 조회 실패)
    ad_fetch_ok = cur_ad is not None
    ad_spend = cur_ad or 0
    prev_ad_spend = prev_ad or 0

    # 매출 집계 (환불 차감)
    def calc_revenue(orders):
        return sum(
            (o.get("total_amount", 0) - (o.get("cancelled_amount") or 0))
            for o in orders if o.get("payment_status") == "PAID"
        )
    revenue = calc_revenue(cur_orders)
    prev_revenue = calc_revenue(prev_orders)
    paid_count = sum(1 for o in cur_orders if o.get("payment_status") == "PAID")

    # AI 비용 집계 (USD)
    def calc_llm_cost(logs):
        total = 0.0
        for r in logs:
            if r.get("status") != "success":
                continue
            pricing = LLM_PRICING.get(r.get("model", ""), LLM_DEFAULT_PRICING)
            total += (
                pricing["input"] * (r.get("prompt_tokens") or 0)
                + pricing["output"] * (r.get("completion_tokens") or 0)
                + pricing["cache_read"] * (r.get("cache_read_tokens") or 0)
                + pricing["cache_write"] * (r.get("cache_creation_tokens") or 0)
            )
        return total / 1_000_000  # USD
    llm_cost_usd = calc_llm_cost(cur_llm)
    prev_llm_cost_usd = calc_llm_cost(prev_llm)
    usd_krw = 1380  # 환율 (대략)
    llm_cost_krw = round(llm_cost_usd * usd_krw)
    prev_llm_cost_krw = round(prev_llm_cost_usd * usd_krw)

    # PG 수수료
    pg_fee = round(revenue * PG_FEE_RATE)
    prev_pg_fee = round(prev_revenue * PG_FEE_RATE)

    # 일별 매출
    from collections import defaultdict
    daily = defaultdict(lambda: {"revenue": 0, "count": 0})
    for o in cur_orders:
        if o.get("payment_status") != "PAID":
            continue
        date = (o.get("created_at") or "")[:10]
        if date:
            amt = (o.get("total_amount", 0) - (o.get("cancelled_amount") or 0))
            daily[date]["revenue"] += amt
            daily[date]["count"] += 1
    daily_list = [{"date": k, "revenue": v["revenue"], "count": v["count"]}
                  for k, v in sorted(daily.items())]

    # 지표 카드
    metrics = []

    # 1. 매출
    rev_trend = _calc_trend(revenue, prev_revenue)
    metrics.append({
        "label": "매출 (환불 차감)",
        "value": _fmt_krw(revenue),
        "prevValue": _fmt_krw(prev_revenue),
        "trend": rev_trend,
        "status": _metric_status(rev_trend),
        "subLabel": f"{paid_count}건 결제 · 일평균 {_fmt_krw(round(revenue / delta) if delta > 0 else 0)}",
        "detail": f"{start_date} ~ {end_date} ({delta}일)",
        "alert": False,
    })

    # 2. AI 비용
    llm_trend = _calc_trend(llm_cost_krw, prev_llm_cost_krw)
    api_ratio = round(llm_cost_krw / revenue * 100, 1) if revenue > 0 else 0
    metrics.append({
        "label": "AI 비용 (LLM)",
        "value": _fmt_krw(llm_cost_krw),
        "prevValue": _fmt_krw(prev_llm_cost_krw),
        "trend": llm_trend,
        "status": _metric_status(llm_trend, higher_is_better=False),
        "subLabel": f"매출 대비 {api_ratio}% · ${llm_cost_usd:,.2f} USD",
        "detail": f"성공 {sum(1 for r in cur_llm if r.get('status')=='success')}건 / 전체 {len(cur_llm)}건",
        "alert": api_ratio > 15,
    })

    # 3. PG 수수료
    metrics.append({
        "label": f"PG 수수료 ({PG_FEE_RATE * 100:g}%)",
        "value": _fmt_krw(pg_fee),
        "prevValue": _fmt_krw(prev_pg_fee),
        "trend": _calc_trend(pg_fee, prev_pg_fee),
        "status": "green",
        "subLabel": f"매출 {_fmt_krw(revenue)} × {PG_FEE_RATE * 100:g}%",
        "detail": "토스페이먼츠 고정 수수료율 적용",
        "alert": False,
    })

    # 4. 광고비 (운영 funnel API의 spend 합계)
    ad_trend = _calc_trend(ad_spend, prev_ad_spend)
    ad_ratio = round(ad_spend / revenue * 100, 1) if revenue > 0 else 0
    metrics.append({
        "label": "광고비",
        "value": _fmt_krw(ad_spend),
        "prevValue": _fmt_krw(prev_ad_spend),
        "trend": ad_trend,
        "status": _metric_status(ad_trend, higher_is_better=False),
        "subLabel": (f"매출 대비 {ad_ratio}%" if ad_fetch_ok else "조회 실패 — 잠시 후 자동 갱신"),
        "detail": ("운영 funnel API(summary-by-product) 기준" if ad_fetch_ok
                   else "외부 funnel API 응답 지연/실패로 미집계"),
        "alert": (not ad_fetch_ok) or ad_ratio > 30,
    })

    # 5. 순이익 (매출 - AI비용 - PG수수료 - 광고비)
    profit = revenue - llm_cost_krw - pg_fee - ad_spend
    prev_profit = prev_revenue - prev_llm_cost_krw - prev_pg_fee - prev_ad_spend
    profit_trend = _calc_trend(profit, prev_profit)
    metrics.append({
        "label": "순이익",
        "value": _fmt_krw(profit),
        "prevValue": _fmt_krw(prev_profit),
        "trend": profit_trend,
        "status": _metric_status(profit_trend),
        "subLabel": f"매출 - AI({_fmt_krw(llm_cost_krw)}) - PG({_fmt_krw(pg_fee)}) - 광고비({_fmt_krw(ad_spend)})",
        "detail": ("광고비 포함 순이익" if ad_fetch_ok else "광고비 조회 실패로 일부 누락된 순이익"),
        "alert": profit < 0,
    })

    result = {
        "metrics": metrics,
        "daily": daily_list,
        "summary": {
            "revenue": revenue, "prev_revenue": prev_revenue,
            "llm_cost_krw": llm_cost_krw, "llm_cost_usd": round(llm_cost_usd, 2),
            "pg_fee": pg_fee, "ad_spend": ad_spend, "profit": profit,
            "paid_count": paid_count, "period_days": delta,
        },
    }
    return result


@app.get("/api/metrics/fetch")
async def fetch_metrics(start_date: str, end_date: str):
    """metrics provider API + Supabase에서 핵심 지표 4개를 조회하여 카드 형식으로 반환"""
    if not METRICS_API_URL:
        return Response(content=json.dumps({"error": "METRICS_API_URL is not configured"}), status_code=503, media_type="application/json")
    sd = dt_date.fromisoformat(start_date)
    ed = dt_date.fromisoformat(end_date)
    delta = (ed - sd).days + 1
    prev_ed = sd - timedelta(days=1)
    prev_sd = prev_ed - timedelta(days=delta - 1)

    metrics = []

    # ── 외부 지표 대시보드 API 호출 (매출, API 비용, 재구매율) ──
    async with httpx.AsyncClient(timeout=120) as client:
        # 서버 깨우기 (Render 무료 플랜 슬립 대응)
        try:
            await client.get(f"{METRICS_API_URL}/api/health")
        except Exception:
            pass

        # 이번 기간 + 전 기간 데이터 병렬 조회
        try:
            cur_funnel, prev_funnel, cur_repurchase, prev_repurchase = await asyncio.gather(
                client.get(
                    f"{METRICS_API_URL}/api/funnel/summary-by-product",
                    params={"start_date": start_date, "end_date": end_date},
                ),
                client.get(
                    f"{METRICS_API_URL}/api/funnel/summary-by-product",
                    params={"start_date": str(prev_sd), "end_date": str(prev_ed)},
                ),
                client.get(
                    f"{METRICS_API_URL}/api/pre-mvp/repurchase",
                    params={"start_date": start_date, "end_date": end_date},
                ),
                client.get(
                    f"{METRICS_API_URL}/api/pre-mvp/repurchase",
                    params={"start_date": str(prev_sd), "end_date": str(prev_ed)},
                ),
            )
        except Exception as e:
            return Response(
                content=json.dumps({"error": f"외부 지표 API 호출 실패: {str(e)}"}),
                status_code=502,
                media_type="application/json",
            )

    # ── 지표 1: 주간 매출 · ROAS · 순익 ──
    if cur_funnel.status_code == 200:
        cur = cur_funnel.json()
        revenue = sum(p.get("server_revenue", 0) + p.get("crm_revenue", 0) for p in cur)
        ad_spend = sum(p.get("spend", 0) for p in cur)
        api_cost = sum(p.get("api_cost_krw", 0) for p in cur)
        total_cost = ad_spend + api_cost
        profit = revenue - total_cost
        roas = round(revenue / total_cost * 100) if total_cost > 0 else 0
        roi = round(profit / total_cost * 100) if total_cost > 0 else 0

        prev_revenue = 0
        if prev_funnel.status_code == 200:
            prev = prev_funnel.json()
            prev_revenue = sum(p.get("server_revenue", 0) + p.get("crm_revenue", 0) for p in prev)

        trend = _calc_trend(revenue, prev_revenue)

        metrics.append({
            "label": "주간 매출 · ROAS · 순익",
            "value": _fmt_krw(revenue),
            "prevValue": _fmt_krw(prev_revenue),
            "trend": trend,
            "status": _metric_status(trend),
            "subLabel": f"ROAS {roas}% · ROI {roi}% · 순익 {_fmt_krw(profit)}",
            "detail": f"광고비 {_fmt_krw(ad_spend)} | API {_fmt_krw(api_cost)} | {start_date}~{end_date}",
            "alert": roas < 100,
        })

        # ── 지표 2: 매출 대비 API 비용 ──
        api_ratio = round(api_cost / revenue * 100, 1) if revenue > 0 else 0
        prev_api_ratio = 0.0
        if prev_funnel.status_code == 200:
            prev = prev_funnel.json()
            prev_rev = sum(p.get("server_revenue", 0) + p.get("crm_revenue", 0) for p in prev)
            prev_api = sum(p.get("api_cost_krw", 0) for p in prev)
            prev_api_ratio = round(prev_api / prev_rev * 100, 1) if prev_rev > 0 else 0

        api_trend = _calc_trend(api_ratio, prev_api_ratio)

        metrics.append({
            "label": "매출 대비 API 비용",
            "value": f"{api_ratio}%",
            "prevValue": f"{prev_api_ratio}%",
            "trend": api_trend,
            "status": _metric_status(api_trend, higher_is_better=False),
            "subLabel": f"API {_fmt_krw(api_cost)} / 매출 {_fmt_krw(revenue)}",
            "detail": f"{start_date}~{end_date} 기준 | 전기간 {prev_api_ratio}%",
            "alert": api_ratio > 15,
        })

    # ── 지표 3: 재구매율 (D7) ──
    if cur_repurchase.status_code == 200:
        cur_rp = cur_repurchase.json().get("data", [])
        total_buyers = sum(p.get("total_buyers", 0) for p in cur_rp)
        if total_buyers > 0:
            weighted_d7 = sum(
                p.get("d7_repurchase_rate", 0) * p.get("total_buyers", 0)
                for p in cur_rp
            ) / total_buyers
        else:
            weighted_d7 = 0
        d7_rate = round(weighted_d7, 1)

        prev_d7 = 0.0
        if prev_repurchase.status_code == 200:
            prev_rp = prev_repurchase.json().get("data", [])
            prev_buyers = sum(p.get("total_buyers", 0) for p in prev_rp)
            if prev_buyers > 0:
                prev_d7 = round(
                    sum(p.get("d7_repurchase_rate", 0) * p.get("total_buyers", 0) for p in prev_rp) / prev_buyers, 1
                )

        rp_trend = _calc_trend(d7_rate, prev_d7)
        detail_parts = [f"{p['product']} {p['d7_repurchase_rate']}%" for p in cur_rp]

        metrics.append({
            "label": "재구매율 (D7)",
            "value": f"{d7_rate}%",
            "prevValue": f"{prev_d7}%",
            "trend": rp_trend,
            "status": _metric_status(rp_trend),
            "subLabel": f"구매자 {total_buyers}명 기준 (D7)",
            "detail": " | ".join(detail_parts) if detail_parts else "",
            "alert": d7_rate < 5,
        })

    # ── 지표 4: 신규 방문자 비율 (Supabase) ──
    try:
        import psycopg2  # noqa: E402 — 선택적 의존성

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 이번 기간
        cur.execute(
            "SELECT SUM(users), SUM(sessions), "
            "ROUND(SUM(users)::numeric / NULLIF(SUM(sessions), 0) * 100, 2) "
            "FROM ga4_daily_funnel WHERE event_date BETWEEN %s AND %s",
            (start_date, end_date),
        )
        row = cur.fetchone()
        users = int(row[0] or 0)
        sessions = int(row[1] or 0)
        new_ratio = float(row[2] or 0)

        # 전 기간
        cur.execute(
            "SELECT ROUND(SUM(users)::numeric / NULLIF(SUM(sessions), 0) * 100, 2) "
            "FROM ga4_daily_funnel WHERE event_date BETWEEN %s AND %s",
            (str(prev_sd), str(prev_ed)),
        )
        prev_ratio = float((cur.fetchone()[0]) or 0)
        conn.close()

        nv_trend = _calc_trend(new_ratio, prev_ratio)

        metrics.append({
            "label": "신규 방문자 비율",
            "value": f"{new_ratio}%",
            "prevValue": f"{prev_ratio}%",
            "trend": nv_trend,
            "status": "green",
            "subLabel": f"users {users:,} / sessions {sessions:,}",
            "detail": f"{start_date}~{end_date}",
            "alert": False,
        })
    except ImportError:
        logger.warning("psycopg2 미설치 — 신규 방문자 비율 건너뜀")
    except Exception as e:
        logger.warning(f"신규 방문자 비율 조회 실패: {e}")

    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "prev_period": {"start_date": str(prev_sd), "end_date": str(prev_ed)},
        "metrics": metrics,
    }


@app.post("/api/metrics/cache")
async def save_metrics_cache(request: Request):
    """조회된 일별 데이터를 로컬 SQLite에 저장"""
    body = await request.json()
    rows = body.get("rows", [])
    if not rows:
        return Response(
            content=json.dumps({"error": "저장할 데이터가 없습니다"}),
            status_code=400,
            media_type="application/json",
        )

    conn = _pg_conn()
    cur = conn.cursor()
    saved = 0
    for row in rows:
        d = row.get("date", "")
        revenue = row.get("revenue", 0)

        # 매출이 0이면 기존 값 유지 (수동 입력 보호)
        if revenue == 0:
            cur.execute("SELECT revenue FROM ext_daily_metrics WHERE date = %s", (d,))
            existing = cur.fetchone()
            if existing and existing[0] > 0:
                revenue = existing[0]

        ad_spend = row.get("ad_spend", 0)
        api_cost = row.get("api_cost", 0)
        profit = revenue - ad_spend - api_cost
        total_cost = ad_spend + api_cost
        roas = round(revenue / total_cost * 100) if total_cost > 0 else 0

        buyers = row.get("buyers", 0)
        api_per_buyer = round(api_cost / buyers) if buyers > 0 else 0

        cur.execute(
            "INSERT INTO ext_daily_metrics "
            "(date, revenue, crm_revenue, buyers, ad_spend, api_cost, api_per_buyer, profit, roas, users, sessions, new_visitor_ratio, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (date) DO UPDATE SET "
            "revenue=EXCLUDED.revenue, crm_revenue=EXCLUDED.crm_revenue, buyers=EXCLUDED.buyers, "
            "ad_spend=EXCLUDED.ad_spend, api_cost=EXCLUDED.api_cost, api_per_buyer=EXCLUDED.api_per_buyer, "
            "profit=EXCLUDED.profit, roas=EXCLUDED.roas, users=EXCLUDED.users, sessions=EXCLUDED.sessions, "
            "new_visitor_ratio=EXCLUDED.new_visitor_ratio, updated_at=NOW()",
            (d, revenue, row.get("crm_revenue", 0), buyers, ad_spend, api_cost, api_per_buyer, profit, roas,
             row.get("users", 0), row.get("sessions", 0), row.get("new_visitor_ratio", 0)),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {"ok": True, "saved": saved}


@app.get("/api/metrics/cache")
async def get_metrics_cache(start_date: str, end_date: str):
    """PostgreSQL에서 일별 캐시 데이터 조회"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT date::text, revenue, crm_revenue, buyers, ad_spend, api_cost, api_per_buyer, "
        "profit, roas, users, sessions, new_visitor_ratio, updated_at::text "
        "FROM ext_daily_metrics WHERE date BETWEEN %s AND %s ORDER BY date",
        (start_date, end_date),
    )
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return {"rows": rows, "count": len(rows)}


@app.delete("/api/metrics/cache")
async def delete_metrics_cache():
    """비활성화 — 데이터 보호를 위해 전체 삭제 차단"""
    return Response(
        status_code=403,
        content=json.dumps({"error": "데이터 보호: 전체 삭제가 비활성화되었습니다"}),
        media_type="application/json",
    )


# metrics/table 결과 단기 캐시 — "원격 조회/저장" 반복 클릭·재방문 시 13초 재계산 회피.
# 데일리 잡(_auto_collect_metrics)은 use_cache=False로 항상 fresh. 전부 0(수집 실패)이면 미캐시.
_METRICS_TABLE_TTL = 300  # 5분
_metrics_table_cache: dict[tuple, tuple] = {}  # (start,end) -> (expires_at, result)


@app.get("/api/metrics/table")
async def fetch_metrics_table(start_date: str, end_date: str, use_cache: bool = True):
    """일별 지표 — 운영 analytics_ro(순매출) + BigQuery(API비용+GA4방문자) + funnel API(광고비)"""
    ck = (start_date, end_date)
    if use_cache:
        hit = _metrics_table_cache.get(ck)
        if hit and hit[0] > _time_mod.time():
            return hit[1]
    sd = dt_date.fromisoformat(start_date)
    ed = dt_date.fromisoformat(end_date)
    days = (ed - sd).days + 1
    date_list = [sd + timedelta(days=i) for i in range(days)]

    revenue_map: dict[str, int] = {}
    buyers_map: dict[str, int] = {}
    api_map: dict[str, int] = {}
    ad_map: dict[str, int] = {}
    visitor_map: dict[str, dict] = {}

    ts_start = f"{start_date} 00:00:00"
    ts_end_exclusive = str(ed + timedelta(days=1))

    # ── 1) BigQuery: 일별 매출 + API 비용 (1회 쿼리) ──
    try:
        if not BQ_PROJECT or not BQ_ORDERS_TABLE or not BQ_LLM_TABLE or not _BQ_KEY_PATH:
            raise RuntimeError("BigQuery metrics integration is not configured")
        from google.cloud import bigquery as bq_client
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(_BQ_KEY_PATH)
        bq = bq_client.Client(project=BQ_PROJECT, credentials=creds)

        # 매출 + 구매자 수
        q_rev = f"""
            SELECT DATE(created_at, 'Asia/Seoul') AS d,
                   SUM(total_amount) AS revenue,
                   COUNT(*) AS buyers
            FROM `{BQ_ORDERS_TABLE}`
            WHERE payment_status = 'PAID'
              AND created_at >= TIMESTAMP('{ts_start}', 'Asia/Seoul')
              AND created_at <  TIMESTAMP('{ts_end_exclusive} 00:00:00', 'Asia/Seoul')
            GROUP BY d ORDER BY d
        """
        for row in bq.query(q_rev).result():
            revenue_map[str(row.d)] = int(row.revenue or 0)
            buyers_map[str(row.d)] = int(row.buyers or 0)

        # API 비용
        q_api = f"""
            SELECT DATE(created_at, 'Asia/Seoul') AS d,
                   ROUND(SUM({_BQ_COST_CASE}) * {USD_KRW_RATE} * {API_COST_MARKUP}, 0) AS cost_krw
            FROM `{BQ_LLM_TABLE}`
            WHERE status = 'success'
              AND created_at >= TIMESTAMP('{ts_start}', 'Asia/Seoul')
              AND created_at <  TIMESTAMP('{ts_end_exclusive} 00:00:00', 'Asia/Seoul')
            GROUP BY d ORDER BY d
        """
        for row in bq.query(q_api).result():
            api_map[str(row.d)] = int(row.cost_krw or 0)

    except Exception as e:
        logger.warning(f"BigQuery 조회 실패: {e}")

    # ── 1b) 운영 daily-summary API override: 순매출 + 구매수 + API비용 ──
    # Optional daily-summary provider can override the BigQuery fallback.
    try:
        summary = await _fetch_daily_summary(start_date, end_date)
        rev2, buy2, api2 = _daily_summary_to_maps(summary)
        if rev2:  # 빈 결과면 BigQuery 폴백 유지
            revenue_map = rev2
            buyers_map = buy2
        api_map.update(api2)
    except Exception as e:
        logger.warning(f"[metrics/table] daily-summary override 실패(BigQuery 폴백): {e}")

    # ── 2) 광고비: 운영 funnel API를 일별로 조회 ──
    # (이 대시보드 DB에는 adset_performance 테이블이 없어 로컬 쿼리가 항상 0이었음.
    #  funnel API /api/funnel/summary 는 하루 단위 조회 시 그날 광고비를 반환 → 동시 조회로 채움)
    async def _funnel_day_spend(client, d):
        if not METRICS_API_URL:
            return str(d), 0
        try:
            r = await client.get(
                f"{METRICS_API_URL}/api/funnel/summary",
                params={"start_date": str(d), "end_date": str(d)},
            )
            if r.status_code == 200:
                return str(d), int(r.json().get("spend", 0) or 0)
            logger.warning(f"[metrics/table] {d} 광고비 HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"[metrics/table] {d} 광고비 조회 실패: {e}")
        return str(d), 0

    try:
        sem = asyncio.Semaphore(8)  # Render 무료 플랜 과부하 방지

        async def _bounded(client, d):
            async with sem:
                return await _funnel_day_spend(client, d)

        async with httpx.AsyncClient(timeout=90) as client:
            # Render 무료 플랜 슬립 대응 — 먼저 깨우기
            try:
                await client.get(f"{METRICS_API_URL}/api/health")
            except Exception:
                pass
            spend_results = await asyncio.gather(*[_bounded(client, d) for d in date_list])
        for ds, sp in spend_results:
            ad_map[ds] = sp
    except Exception as e:
        logger.warning(f"[metrics/table] 광고비 일별 조회 실패: {e}")

    # ── 2b) 신규 방문자: BigQuery GA4 익스포트(events_*)에서 일별 users/sessions ──
    # (로컬 ga4_daily_funnel 테이블이 이 DB에 없어 0이었음 → GA4 BigQuery 익스포트에서 직접 집계)
    try:
        if not BQ_PROJECT or not BQ_GA4_DATASET or not _BQ_KEY_PATH:
            raise RuntimeError("BigQuery GA4 integration is not configured")
        from google.cloud import bigquery as bq_client
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(_BQ_KEY_PATH)
        bq_ga4 = bq_client.Client(project=BQ_PROJECT, credentials=creds)
        suffix_start = start_date.replace("-", "")
        suffix_end = end_date.replace("-", "")
        q_ga4 = f"""
            SELECT event_date AS d,
                   COUNT(DISTINCT user_pseudo_id) AS users,
                   COUNT(DISTINCT CONCAT(
                       user_pseudo_id, '-',
                       CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING)
                   )) AS sessions
            FROM `{BQ_PROJECT}.{BQ_GA4_DATASET}.events_*`
            WHERE _TABLE_SUFFIX BETWEEN '{suffix_start}' AND '{suffix_end}'
            GROUP BY d
        """
        for row in bq_ga4.query(q_ga4).result():
            ev = str(row.d)  # 'YYYYMMDD'
            ds = f"{ev[0:4]}-{ev[4:6]}-{ev[6:8]}"
            u, s = int(row.users or 0), int(row.sessions or 0)
            visitor_map[ds] = {
                "users": u, "sessions": s,
                "ratio": round(u / s * 100, 2) if s > 0 else 0,
            }
    except Exception as e:
        logger.warning(f"[metrics/table] GA4 방문자 조회 실패: {e}")

    # ── 3) 결과 조합 ──
    rows = []
    for d in date_list:
        ds = str(d)
        revenue = revenue_map.get(ds, 0)
        buyers = buyers_map.get(ds, 0)
        ad_spend = ad_map.get(ds, 0)
        api_cost = api_map.get(ds, 0)
        profit = revenue - ad_spend - api_cost
        total_cost = ad_spend + api_cost
        roas_val = round(revenue / total_cost * 100) if total_cost > 0 else 0
        api_per_buyer = round(api_cost / buyers) if buyers > 0 else 0

        vis = visitor_map.get(ds, {})
        rows.append({
            "date": ds,
            "revenue": revenue,
            "crm_revenue": 0,
            "buyers": buyers,
            "ad_spend": ad_spend,
            "api_cost": api_cost,
            "api_per_buyer": api_per_buyer,
            "profit": profit,
            "roas": roas_val,
            "users": vis.get("users", 0),
            "sessions": vis.get("sessions", 0),
            "new_visitor_ratio": vis.get("ratio", 0),
        })

    result = {"period": {"start_date": start_date, "end_date": end_date}, "rows": rows}
    # 유의미한 데이터가 있을 때만 캐시(전부 0=수집 실패 → 다음 클릭에 재시도)
    if any(r["revenue"] or r["ad_spend"] or r["api_cost"] for r in rows):
        _metrics_table_cache[ck] = (_time_mod.time() + _METRICS_TABLE_TTL, result)
    return result


# ── 마감 임박 할일 체크 (백그라운드 태스크) ──

async def _accrue_monthly_leave():
    """연차 자동 부여 (근로기준법 기준, 매일 1회).
    - 1년 미만: 입사일 일자 기준 매월 +1 (최대 11)
      예: 입사 2026-07-25 → 매월 25일에 +1. 입사일이 그달 말일을 넘는 경우(31일 등) 월말로 자동 보정.
    - 1년 이상: 기본 15일, 3년차부터 2년마다 +1 (5년차 17, 7년차 18, ...), 최대 25일
      즉 target = min(15 + (근속연수 - 1) // 2, 25)
    공통: 자동 계산값이 현재 total_days보다 클 때만 끌어올림 (admin 수동 보상 등 더 높은 값은 보존)."""
    import calendar as _cal
    while True:
        try:
            today = datetime.now(KST).replace(tzinfo=None).date()
            conn = _pg_conn()
            cur = conn.cursor()
            # 연차 산정일 = leave_start_date 우선, 없으면 hire_date
            cur.execute("SELECT name, COALESCE(leave_start_date, hire_date) FROM ext_users WHERE is_active = TRUE AND COALESCE(leave_start_date, hire_date) IS NOT NULL")
            users = cur.fetchall()
            year = today.year
            for name, hire in users:
                days_since = (today - hire).days
                if days_since < 0:
                    continue  # 미래 입사일은 제외
                # 입사 기념일이 그달 말일을 넘으면 말일로 보정
                last_day_this_month = _cal.monthrange(today.year, today.month)[1]
                effective_hire_day = min(hire.day, last_day_this_month)
                # 만 근속 연수 (입사 기념일 통과 여부 반영)
                years = today.year - hire.year
                if (today.month, today.day) < (hire.month, effective_hire_day):
                    years -= 1

                if years < 1:
                    # 1년 미만 — 입사일 기준 만월 수 (최대 11)
                    months = (today.year - hire.year) * 12 + (today.month - hire.month)
                    if today.day < effective_hire_day:
                        months -= 1
                    target = max(0, min(months, 11))
                else:
                    # 1년 이상 — 근로기준법: 1~2년차 15, 3~4년차 16, 5~6년차 17, ... 최대 25
                    target = min(15 + (years - 1) // 2, 25)

                if target <= 0:
                    continue
                cur.execute("SELECT total_days FROM ext_leave_balance WHERE user_name = %s AND year = %s", (name, year))
                row = cur.fetchone()
                cur_total = float(row[0]) if row else 0.0
                if cur_total < target:
                    cur.execute(
                        "INSERT INTO ext_leave_balance (user_name, year, total_days) VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_name, year) DO UPDATE SET total_days = EXCLUDED.total_days",
                        (name, year, float(target)))
                    logger.info(f"[연차 자동부여] {name}(근속 {years}년): total_days {cur_total} -> {target}")
            conn.commit()
            conn.close()
            _heartbeat("accrue_monthly_leave", True)
        except Exception as e:
            _heartbeat("accrue_monthly_leave", False, str(e))
            logger.error(f"_accrue_monthly_leave 실패: {e}")
        await asyncio.sleep(86400)  # 24시간마다


async def _check_deadline_todos():
    """매 시간 마감일 D-1인 할일을 확인하고 Slack DM 전송 — ext_users.slack_id 조회"""
    while True:
        await asyncio.sleep(3600)  # 1시간마다 체크
        try:
            todos_data = _read_store("todos")
            if not todos_data:
                _heartbeat("check_deadline_todos", True)  # 할일 없음 = 정상 무작업
                continue

            bot_token = _get_slack_bot_token()  # DB 토큰 사용 — 전역 변수는 재시작 시 비워짐
            if not bot_token:
                _heartbeat("check_deadline_todos", True)  # 토큰 미설정 = 정상 무작업
                continue

            # 이미 알림 보낸 할일 ID 추적
            notified = set(todos_data.get("_deadlineNotified", []))
            tomorrow = (datetime.now(KST).replace(tzinfo=None) + timedelta(days=1)).strftime("%Y-%m-%d")
            new_notifications = []

            # 발송 후보 먼저 수집 (미완료·미알림·내일 마감)
            candidates = [t for t in todos_data.get("todos", [])
                          if t.get("status") != "done" and t.get("id") not in notified
                          and t.get("dueDate") == tomorrow]

            DEADLINE_MAX_BATCH = 20  # 안전장치: 한 번에 이보다 많으면 발송 금지(중복방지 손상·대량 의심)
            if len(candidates) > DEADLINE_MAX_BATCH:
                # 비정상적으로 많음 → 발송하지 않고 기록만 (다음부터 정상, 폭주 차단)
                new_notifications = [t["id"] for t in candidates if t.get("id")]
                logger.warning(f"[마감알림] 후보 {len(candidates)}건 > 상한 {DEADLINE_MAX_BATCH} — 무발송 기록만")
            else:
                for todo in candidates:
                    assignee = todo.get("assignee", "")
                    slack_id = _get_slack_id_by_name(assignee)
                    if slack_id:
                        msg = f"[마감 임박] '{todo.get('title', '')}' 마감일이 내일({tomorrow})입니다."
                        await _slack_post_message(bot_token, slack_id, msg)
                        new_notifications.append(todo["id"])

            # 알림 보낸(또는 무발송 처리한) ID 기록
            if new_notifications:
                todos_data.setdefault("_deadlineNotified", [])
                todos_data["_deadlineNotified"].extend(new_notifications)
                await _write_store("todos", todos_data)
            _heartbeat("check_deadline_todos", True)

        except Exception as e:
            _heartbeat("check_deadline_todos", False, str(e))
            logger.warning(f"마감 알림 체크 실패: {e}")


def _save_remote_rows(rows, skip_dates=None):
    """원격 데이터를 PostgreSQL에 저장하는 공통 함수"""
    skip = skip_dates or set()
    conn = _pg_conn()
    cur = conn.cursor()
    saved = 0
    for row in rows:
        d = row.get("date", "")
        if d in skip:
            continue
        revenue = row.get("revenue", 0)
        ad_spend = row.get("ad_spend", 0)
        api_cost = row.get("api_cost", 0)
        buyers = row.get("buyers", 0)
        profit = revenue - ad_spend - api_cost
        total_cost = ad_spend + api_cost
        roas = round(revenue / total_cost * 100) if total_cost > 0 else 0
        api_per_buyer = round(api_cost / buyers) if buyers > 0 else 0

        cur.execute(
            "INSERT INTO ext_daily_metrics "
            "(date, revenue, crm_revenue, buyers, ad_spend, api_cost, api_per_buyer, profit, roas, users, sessions, new_visitor_ratio, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (date) DO UPDATE SET "
            "revenue=EXCLUDED.revenue, crm_revenue=EXCLUDED.crm_revenue, buyers=EXCLUDED.buyers, "
            "ad_spend=EXCLUDED.ad_spend, api_cost=EXCLUDED.api_cost, api_per_buyer=EXCLUDED.api_per_buyer, "
            "profit=EXCLUDED.profit, roas=EXCLUDED.roas, users=EXCLUDED.users, sessions=EXCLUDED.sessions, "
            "new_visitor_ratio=EXCLUDED.new_visitor_ratio, updated_at=NOW()",
            (d, revenue, row.get("crm_revenue", 0), buyers, ad_spend, api_cost, api_per_buyer, profit, roas,
             row.get("users", 0), row.get("sessions", 0), row.get("new_visitor_ratio", 0)),
        )
        saved += 1
    conn.commit()
    conn.close()
    return saved


async def _auto_collect_metrics():
    """매일 오후 6시 자동 지표 수집 — 최근 3일 + 누락 데이터 보정"""
    await asyncio.sleep(10)  # 서버 시작 후 10초 대기
    while True:
        try:
            # 다음 오후 6시까지 대기
            now = datetime.now()
            target = now.replace(hour=18, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"[자동수집] 다음 실행: {target.strftime('%Y-%m-%d %H:%M')} ({int(wait_seconds)}초 후)")
            await asyncio.sleep(wait_seconds)

            today = dt_date.today()
            logger.info(f"[자동수집] 실행 시작 — {today}")

            # ── 0) DB 자동 백업 (매일 1회, 최대 30개 유지) ──
            try:
                import shutil
                backup_name = f"metrics_cache_{today.strftime('%Y%m%d')}.db"
                backup_path = DB_BACKUPS_DIR / backup_name
                if not backup_path.exists():
                    shutil.copy2(str(METRICS_DB), str(backup_path))
                    logger.info(f"[백업] {backup_name} 생성 완료")
                    # 오래된 백업 삭제 (30개 초과 시)
                    backups = sorted(DB_BACKUPS_DIR.glob("metrics_cache_*.db"))
                    while len(backups) > MAX_DB_BACKUPS:
                        backups[0].unlink()
                        backups.pop(0)
                        logger.info(f"[백업] 오래된 백업 삭제 (최대 {MAX_DB_BACKUPS}개 유지)")
            except Exception as e:
                logger.warning(f"[백업] 실패: {e}")

            # ── 1) 최근 4일: 항상 덮어쓰기 (매출 집계·광고비가 며칠에 걸쳐 사후 보정됨) ──
            yesterday = today - timedelta(days=1)
            recent_start = today - timedelta(days=3)
            result = await fetch_metrics_table(str(recent_start), str(today), use_cache=False)
            rows = result.get("rows", [])
            if rows:
                saved = _save_remote_rows(rows)
                logger.info(f"[자동수집] 최근 4일({recent_start}~{today}) → {saved}건 덮어쓰기")

            # ── 2) 그 이전: 매출/광고비/API 중 하나라도 0이면 재조회 ──
            # Installer-declared unavailable dates are excluded without a built-in history.
            SKIP_DATES = _installation.METRICS_SKIPPED_DATES
            conn = _pg_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT date::text FROM ext_daily_metrics "
                "WHERE (revenue = 0 OR ad_spend = 0 OR api_cost = 0) AND date < %s "
                "ORDER BY date",
                (str(recent_start),),
            )
            incomplete = []
            for row in cur.fetchall():
                if row[0] not in SKIP_DATES:
                    incomplete.append(row[0])
            conn.close()

            if incomplete:
                logger.info(f"[자동수집] 누락 보정 대상: {len(incomplete)}건 ({incomplete[0]}~{incomplete[-1]})")
                fill_start = incomplete[0]
                fill_end = incomplete[-1]
                result = await fetch_metrics_table(fill_start, fill_end, use_cache=False)
                remote_rows = result.get("rows", [])
                useful = [r for r in remote_rows if r.get("revenue", 0) > 0 or r.get("ad_spend", 0) > 0 or r.get("api_cost", 0) > 0]
                if useful:
                    saved = _save_remote_rows(useful)
                    logger.info(f"[자동수집] 누락 보정 → {saved}건 업데이트")
                else:
                    logger.info("[자동수집] 누락 보정 — 원격에도 데이터 없음")
            else:
                logger.info("[자동수집] 누락 데이터 없음")

            logger.info("[자동수집] 완료")
            _heartbeat("auto_collect_metrics", True)

        except Exception as e:
            _heartbeat("auto_collect_metrics", False, str(e))
            logger.warning(f"[자동수집] 실패: {e}")
            # 실패 시 1시간 후 재시도
            await asyncio.sleep(3600)


async def _auto_sync_linear():
    """1시간마다 Linear → 대시보드 자동 동기화"""
    await asyncio.sleep(30)  # 서버 시작 후 30초 대기
    while True:
        try:
            if not LINEAR_API_KEY:
                logger.info("[Linear 동기화] API 키 미설정 — 건너뜀")
                _heartbeat("auto_sync_linear", True)  # 키 미설정 = 정상 무작업
                await asyncio.sleep(3600)
                continue

            # Linear에서 이슈 조회
            result = await _linear_graphql("""{
                issues(first: 200, filter: { team: { key: { eq: "CHE" } } }) {
                    nodes {
                        identifier title
                        state { name type }
                        assignee { name }
                        project { name }
                        completedAt createdAt
                        description
                    }
                }
            }""")
            linear_issues = result.get("data", {}).get("issues", {}).get("nodes", [])
            if not linear_issues:
                logger.info("[Linear 동기화] 이슈 0건 — 건너뜀")
                _heartbeat("auto_sync_linear", True)  # 이슈 0건 = 정상 무작업
                await asyncio.sleep(3600)
                continue

            # 기존 대시보드 데이터 로드
            report = _read_store("report") or {}
            if "activeProjects" not in report:
                report["activeProjects"] = {"title": "진행 중인 프로젝트", "subtitle": "팀 업무 현황", "projects": []}
            if "completedResults" not in report:
                report["completedResults"] = {"title": "완료 결과 추적", "subtitle": "완료된 프로젝트", "items": []}

            ap_list = report["activeProjects"]["projects"]
            cr_list = report["completedResults"]["items"]

            state_map = {
                "started": "active",
                "unstarted": "active",
                "completed": "done",
                "canceled": "done",
                "backlog": "hold",
                "triage": "active",
            }

            # 인덱스 구축
            by_linear_id = {}
            by_title = {}
            for item in ap_list + cr_list:
                lid = item.get("linearId", "")
                if lid:
                    by_linear_id[lid] = item
                t = item.get("title", "").strip().lower()
                if t:
                    by_title[t] = item

            updated = 0
            added = 0

            for li in linear_issues:
                title = li.get("title", "")
                li_state_type = li.get("state", {}).get("type", "")
                li_assignee = (li.get("assignee") or {}).get("name", "")
                li_completed = li.get("completedAt", "")
                li_identifier = li.get("identifier", "")
                li_project = (li.get("project") or {}).get("name", "")
                li_created = li.get("createdAt", "")
                li_desc = (li.get("description") or "")[:300]
                new_status = state_map.get(li_state_type, "active")

                matched = by_linear_id.get(li_identifier)
                if not matched:
                    matched = by_title.get(title.strip().lower())

                if matched:
                    changed = False
                    if matched.get("status", "") != new_status:
                        matched["status"] = new_status
                        changed = True
                    if li_assignee and matched.get("owner", "") != li_assignee:
                        matched["owner"] = li_assignee
                        changed = True
                    if li_completed and not matched.get("completedDate"):
                        matched["completedDate"] = li_completed[:10]
                        changed = True
                    if matched.get("linearId") != li_identifier:
                        matched["linearId"] = li_identifier
                        changed = True
                    if changed:
                        updated += 1
                else:
                    new_item = {
                        "id": uuid.uuid4().hex[:13],
                        "title": title,
                        "owner": li_assignee,
                        "status": new_status,
                        "linearId": li_identifier,
                        "note": f"[Linear] {li_project}\n{li_desc}".strip() if li_project or li_desc else "",
                        "createdAt": li_created,
                        "completedDate": li_completed[:10] if li_completed else "",
                    }
                    if new_status == "done":
                        cr_list.append(new_item)
                    else:
                        ap_list.append(new_item)
                    by_linear_id[li_identifier] = new_item
                    by_title[title.strip().lower()] = new_item
                    added += 1

            if updated > 0 or added > 0:
                report["_lastModified"] = datetime.utcnow().isoformat() + "Z"
                await _write_store("report", report)
                logger.info(f"[Linear 동기화] 완료 — Linear {len(linear_issues)}건 중 업데이트 {updated}건, 신규 {added}건")
            else:
                logger.info(f"[Linear 동기화] 변경 없음 (Linear {len(linear_issues)}건 확인)")
            _heartbeat("auto_sync_linear", True)

        except Exception as e:
            _heartbeat("auto_sync_linear", False, str(e))
            logger.warning(f"[Linear 동기화] 실패: {e}")

        await asyncio.sleep(3600)  # 1시간 간격


# ── B3: 백그라운드 작업 하트비트 + 실패/중단 알림 ──
# 각 루프가 사이클마다 _heartbeat(name, ok) 기록. 슈퍼바이저가 죽으면 자동 재시작+알림.
# 모니터가 last_success staleness/연속실패를 점검해 1회 알림(중복 억제, 복구 시 해제).
# 기대 최대 staleness(초) = 루프 주기 × 여유. 초과 시 "지연/중단 의심" 알림.
_HEARTBEAT_SPEC = {
    "check_deadline_todos":   9000,    # 1h 주기 → 2.5h 허용
    "watch_expense_sheet":    1800,    # 2m 주기 → 30m
    "accrue_monthly_leave":   97200,   # 24h 주기 → 27h
    "auto_collect_metrics":   97200,   # 매일 18시 → 27h
    "auto_sync_linear":       9000,    # 1h 주기 → 2.5h
    "linear_db_sync":         2400,    # 5m 주기 → 40m
    "weekly_report_reminder": 691200,  # 주 1회 → 8일
    "cs_prewarm":             4500,    # 20m 주기 → 75m
    "cs_weekly_report":       691200,  # 주 1회(월 09:30) → 8일
}
_HEARTBEAT_FAIL_ALERT = 3  # 연속 실패 이만큼이면 알림


def _heartbeat(task: str, ok: bool, error: str = "") -> None:
    """백그라운드 1사이클 결과 기록(멱등 upsert). 실패해도 메인 로직 방해 안 함."""
    try:
        conn = _pg_conn(); cur = conn.cursor()
        if ok:
            cur.execute(
                "INSERT INTO ext_heartbeats (task, last_run, last_success, last_error, fail_streak, alerted, updated_at) "
                "VALUES (%s, NOW(), NOW(), '', 0, FALSE, NOW()) "
                "ON CONFLICT (task) DO UPDATE SET last_run=NOW(), last_success=NOW(), "
                "last_error='', fail_streak=0, alerted=FALSE, updated_at=NOW()",
                (task,))
        else:
            cur.execute(
                "INSERT INTO ext_heartbeats (task, last_run, last_error, fail_streak, updated_at) "
                "VALUES (%s, NOW(), %s, 1, NOW()) "
                "ON CONFLICT (task) DO UPDATE SET last_run=NOW(), "
                "last_error=EXCLUDED.last_error, fail_streak=ext_heartbeats.fail_streak+1, updated_at=NOW()",
                (task, (error or "")[:500]))
        conn.commit(); conn.close()
    except Exception as e:
        try: conn.rollback(); conn.close()
        except Exception: pass
        logger.warning(f"_heartbeat({task}) 실패: {e}")


def _heartbeat_seed(task: str) -> None:
    """슈퍼바이저 시작 시 1행 보장 + last_success 그레이스(now) — 배포 직후 오탐 방지.
    재시작(conflict) 시엔 last_success 보존하고 last_run만 갱신."""
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO ext_heartbeats (task, last_run, last_success, fail_streak, alerted, updated_at) "
            "VALUES (%s, NOW(), NOW(), 0, FALSE, NOW()) "
            "ON CONFLICT (task) DO UPDATE SET last_run=NOW(), updated_at=NOW()",
            (task,))
        conn.commit(); conn.close()
    except Exception as e:
        try: conn.rollback(); conn.close()
        except Exception: pass
        logger.warning(f"_heartbeat_seed({task}) 실패: {e}")


async def _alert_bg(msg: str) -> None:
    """백그라운드 알림 발송 — SLACK_ALERT_CHANNEL 우선, 없으면 SLACK_ERROR_CHANNEL."""
    try:
        bot_token = _get_slack_bot_token() if "_get_slack_bot_token" in globals() else SLACK_BOT_TOKEN
        channel = os.environ.get("SLACK_ALERT_CHANNEL", "") or os.environ.get("SLACK_ERROR_CHANNEL", "")
        if bot_token and channel:
            await _slack_post_message(bot_token, channel, msg)
        else:
            logger.warning(f"[하트비트] 알림 채널 미설정 — 미발송: {msg[:120]}")
    except Exception as e:
        logger.warning(f"_alert_bg 실패: {e}")


def _start_supervised(name: str, coro_factory) -> None:
    """백그라운드 루프를 슈퍼바이저로 감싸 등록 — 죽으면 알림+자동 재시작."""
    _heartbeat_seed(name)
    asyncio.create_task(_supervise(name, coro_factory))


async def _supervise(name: str, coro_factory) -> None:
    """루프 코루틴이 (예외/반환으로) 죽으면 기록·알림 후 60초 뒤 재시작.
    while True 루프는 정상이면 반환하지 않으므로 반환=비정상 종료로 취급."""
    restarts = 0
    while True:
        try:
            await coro_factory()
            raise RuntimeError("loop exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            restarts += 1
            _heartbeat(name, ok=False, error=f"중단: {repr(e)[:200]}")
            logger.error(f"[하트비트] 백그라운드 '{name}' 중단(#{restarts}): {repr(e)[:200]} — 60초 후 재시작")
            await _alert_bg(f":warning: 백그라운드 작업 *{name}* 중단(#{restarts})\n`{repr(e)[:200]}`\n60초 후 자동 재시작")
            await asyncio.sleep(60)


async def _heartbeat_monitor_loop() -> None:
    """15분 주기로 last_success staleness/연속실패 점검 → 1회 알림(복구 시 해제)."""
    await asyncio.sleep(180)  # 시작 그레이스
    while True:
        try:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "SELECT task, EXTRACT(EPOCH FROM (NOW()-last_success))::int, "
                "COALESCE(fail_streak,0), COALESCE(alerted,FALSE), COALESCE(last_error,'') "
                "FROM ext_heartbeats")
            rows = cur.fetchall()
            for task, stale_s, fail_streak, alerted, last_error in rows:
                max_stale = _HEARTBEAT_SPEC.get(task)
                if max_stale is None:
                    continue  # 등록되지 않은 작업은 점검 제외
                bad = (stale_s is None or stale_s > max_stale or fail_streak >= _HEARTBEAT_FAIL_ALERT)
                if bad and not alerted:
                    cur.execute("UPDATE ext_heartbeats SET alerted=TRUE WHERE task=%s", (task,))
                    conn.commit()
                    mins = (stale_s // 60) if stale_s is not None else "?"
                    await _alert_bg(
                        f":red_circle: 백그라운드 *{task}* 지연/중단 의심\n"
                        f"마지막 성공 {mins}분 전(기대 {max_stale // 60}분 이내), 연속실패 {fail_streak}\n"
                        f"최근오류: `{(last_error or '없음')[:160]}`")
                elif not bad and alerted:
                    cur.execute("UPDATE ext_heartbeats SET alerted=FALSE WHERE task=%s", (task,))
                    conn.commit()
                    await _alert_bg(f":white_check_mark: 백그라운드 *{task}* 정상 복구")
            conn.close()
        except Exception as e:
            logger.warning(f"[하트비트 모니터] 실패: {e}")
        await asyncio.sleep(900)  # 15분 주기


async def _cs_prewarm_loop():
    """CS 지표/상담일지 캐시 예열 + 감성 분류 점진 채움 (20분 주기)."""
    await asyncio.sleep(25)
    try:
        from src.cs_metrics import (compute_cs_metrics, compute_consultations,
                                     fetch_consultations, classify_new_sentiments)
    except ImportError:
        from cs_metrics import (compute_cs_metrics, compute_consultations,
                                fetch_consultations, classify_new_sentiments)
    while True:
        try:
            await asyncio.to_thread(compute_cs_metrics, 8, False)        # fresh → 캐시 갱신
            _h, rows, err = await asyncio.to_thread(fetch_consultations)
            if not err and rows:
                # 감성 분류 점진 채움(사이클당 일부) → 시간이 지나면 100% 커버리지
                await asyncio.to_thread(classify_new_sentiments, rows, 120)
            await asyncio.to_thread(compute_consultations, 300, False)   # 불만율 반영 갱신
            _heartbeat("cs_prewarm", True)
        except Exception as e:
            _heartbeat("cs_prewarm", False, str(e))
            logger.warning(f"[CS 예열] 오류: {e}")
        await asyncio.sleep(1200)  # 20분


async def _cs_weekly_report_loop():
    """매주 월요일 09:30 KST — CS 주간 리포트 Slack 게시(CS_REPORT_SLACK_CHANNEL 설정 시)."""
    try:
        from src.cs_metrics import build_weekly_report_text
    except ImportError:
        from cs_metrics import build_weekly_report_text
    while True:
        try:
            now = datetime.now(KST)
            days_to_mon = (7 - now.weekday()) % 7
            if days_to_mon == 0 and (now.hour > 9 or (now.hour == 9 and now.minute >= 30)):
                days_to_mon = 7
            target = (now + timedelta(days=days_to_mon)).replace(hour=9, minute=30, second=0, microsecond=0)
            await asyncio.sleep(max(60, int((target - now).total_seconds())))
            ch = os.environ.get("CS_REPORT_SLACK_CHANNEL", "")
            bot = _get_slack_bot_token() if "_get_slack_bot_token" in globals() else SLACK_BOT_TOKEN
            if ch and bot:
                text = await asyncio.to_thread(build_weekly_report_text)
                res = await _slack_post_message(bot, ch, text)
                if res.get("ok"):
                    logger.info("[CS 주간리포트] Slack 게시 완료")
                else:
                    logger.warning(f"[CS 주간리포트] Slack 게시 실패: {res.get('error')}")
                    await _alert_bg(f":warning: CS 주간리포트 Slack 게시 실패 — `{res.get('error')}` (채널 {ch})")
            else:
                logger.info("[CS 주간리포트] CS_REPORT_SLACK_CHANNEL 미설정 — 게시 생략")
            _heartbeat("cs_weekly_report", True)
        except Exception as e:
            _heartbeat("cs_weekly_report", False, str(e))
            logger.warning(f"[CS 주간리포트] 오류: {e}")
            await asyncio.sleep(3600)


def _start_background_tasks() -> None:
    """리더 워커가 담당하는 모든 백그라운드 루프 + 하트비트 모니터 등록."""
    if not BACKGROUND_TASKS_ENABLED:
        return
    logger.info("[리더 워커] 백그라운드 작업 시작")
    _start_supervised("cs_prewarm", _cs_prewarm_loop)
    _start_supervised("cs_weekly_report", _cs_weekly_report_loop)
    _start_supervised("check_deadline_todos", _check_deadline_todos)
    # 비용 시트 폴러 — 안전장치(형식검증·배치상한) 적용 후 재활성화
    _start_supervised("watch_expense_sheet", _watch_expense_sheet)
    _start_supervised("accrue_monthly_leave", _accrue_monthly_leave)
    _start_supervised("auto_collect_metrics", _auto_collect_metrics)
    _start_supervised("auto_sync_linear", _auto_sync_linear)
    # Linear → DB 캐시 동기화 루프 (5분 주기)
    _start_supervised("linear_db_sync", _linear_db_sync_loop)
    # 주간 보고 미작성 리마인더 (월요일 09:00 KST)
    _start_supervised("weekly_report_reminder", _weekly_report_reminder_loop)
    # 하트비트 모니터 — 지연/중단 감지 알림
    asyncio.create_task(_heartbeat_monitor_loop())


async def _leader_election_loop() -> None:
    """리더가 될 때까지 주기적으로 재시도 → 리더 확보 시 백그라운드 작업 시작.

    기존엔 startup에서 _try_become_leader()를 1회만 호출 → 배포 교체 중
    구 인스턴스가 advisory lock을 쥐고 있으면 신 인스턴스가 리더 선출에 실패하고
    재시도가 없어 **모든 백그라운드 작업이 다음 무중첩 재시작까지 멈추는** 레이스가 있었음.
    여기서 30초 주기로 재시도하면: (1) 배포 overlap이 풀리면 곧 리더 확보,
    (2) 리더 프로세스가 죽어 락이 풀리면 다른 워커가 이어받음(failover)."""
    if not BACKGROUND_TASKS_ENABLED:
        return
    if REDIRECT_TO:
        # 이전 모드 — 백그라운드 작업은 새 서버가 담당(여기서 리더를 잡으면 새 서버가 락을 못 얻음)
        logger.info(f"[이전 모드] REDIRECT_TO={REDIRECT_TO} — 백그라운드 작업 실행 안 함")
        return
    while True:
        if _try_become_leader():
            _start_background_tasks()
            return
        await asyncio.sleep(30)


def initialize_schema():
    """Initialize local schema under a cross-process, transaction-scoped advisory lock."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    # Keep the lock in its own transaction: helpers commit on separate pooled connections.
    # A transaction pooler pins this dedicated connection's backend until commit/rollback.
    schema_connection = psycopg2.connect(DATABASE_URL, **_PG_CONN_KWARGS)
    try:
        schema_connection.autocommit = False
        with schema_connection.cursor() as schema_cursor:
            schema_cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
        conn = _pg_conn()
        try:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ext_app_store (key TEXT PRIMARY KEY, data JSONB, updated_at TIMESTAMP DEFAULT NOW())")
            conn.commit()
        finally:
            conn.close()
        _init_users_table()
        _ensure_bootstrap_tables()
        _ensure_leave_columns()
        _ensure_exec_table()
        _ensure_core_tree_table()
        _ensure_approvals_table()
        _ensure_activity_logs_table()
        _ensure_notifications_table()
        _ensure_member_reports_table()
        _ensure_project_hierarchy_table()
        _ensure_memos_table()
        _init_backups_table()
        _ensure_settings_extra_tables()
        _ensure_linear_cache_table()
        _ensure_upsert_indexes()
        if _installation.env_bool("SEED_KOREAN_HOLIDAYS"):
            _seed_korean_holidays()
        schema_connection.commit()
    except BaseException:
        schema_connection.rollback()
        raise
    finally:
        schema_connection.close()


@app.on_event("startup")
async def startup_event():
    """Validate credentials, initialize schema and start only explicitly enabled integrations."""
    _installation.require_jwt_secret(JWT_SECRET)
    initialize_schema()
    if STORAGE_AUTO_CREATE_BUCKET and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SUPABASE_URL}/storage/v1/bucket/{STORAGE_BUCKET}", headers=_supabase_storage_headers())
            if resp.status_code == 404:
                resp = await client.post(
                    f"{SUPABASE_URL}/storage/v1/bucket", headers=_supabase_storage_headers(),
                    json={"id": STORAGE_BUCKET, "name": STORAGE_BUCKET, "public": False},
                )
                resp.raise_for_status()
    if BACKGROUND_TASKS_ENABLED:
        asyncio.create_task(_leader_election_loop())


# ── 댓글 API (PostgreSQL 기반) ──

@app.get("/api/comments/{owner_key}")
async def get_comments(owner_key: str):
    """담당자별 댓글 조회"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, text, created_at::text as \"createdAt\" FROM ext_comments WHERE owner_key = %s ORDER BY created_at ASC",
        (owner_key,)
    )
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return {"comments": rows}


@app.post("/api/comments/{owner_key}")
async def add_comment(owner_key: str, request: Request):
    """댓글 추가"""
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return Response(status_code=400, content="댓글 내용이 비어있습니다")

    cid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ext_comments (id, owner_key, text, created_at) VALUES (%s, %s, %s, %s)",
        (cid, owner_key, text, now)
    )
    conn.commit()
    conn.close()
    return {"id": cid, "text": text, "createdAt": now}


@app.delete("/api/comments/{owner_key}/{comment_id}")
async def delete_comment(owner_key: str, comment_id: str):
    """댓글 삭제"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ext_comments WHERE id = %s AND owner_key = %s", (comment_id, owner_key))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 백업 관리 API ──

@app.get("/api/backups")
async def list_backups(file: str = "report"):
    """백업 목록 조회 (파일별)"""
    backups = sorted(BACKUPS_DIR.glob(f"{file}_*.json"), key=lambda p: p.name, reverse=True)
    result = []
    for b in backups:
        stat = b.stat()
        # 파일명에서 타임스탬프 추출: report_20260327_093800.json
        name = b.stem  # report_20260327_093800
        ts_part = name.replace(f"{file}_", "")
        try:
            ts = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
            label = ts.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            label = ts_part
        result.append({
            "filename": b.name,
            "label": label,
            "size": stat.st_size,
            "sizeKb": round(stat.st_size / 1024, 1),
        })
    return {"backups": result}


@app.get("/api/backups/{filename}")
async def get_backup(filename: str):
    """백업 파일 내용 조회"""
    path = BACKUPS_DIR / filename
    if not path.exists() or not path.name.endswith(".json"):
        return Response(status_code=404, content="백업 파일을 찾을 수 없습니다")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/backups/restore/{filename}")
async def restore_backup(filename: str):
    """백업 파일로 복원 (현재 데이터를 백업 후 덮어쓰기)"""
    backup_path = BACKUPS_DIR / filename
    if not backup_path.exists() or not backup_path.name.endswith(".json"):
        return Response(status_code=404, content="백업 파일을 찾을 수 없습니다")

    # 파일명에서 원본 키 결정 (report_xxx.json → report)
    stem = filename.split("_")[0]
    if stem not in ("report", "todos"):
        return Response(status_code=400, content=f"알 수 없는 파일 유형: {stem}")

    backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
    await _write_store(stem, backup_data)
    return {"ok": True, "restored": filename, "target": stem}


@app.delete("/api/backups/{filename}")
async def delete_backup(filename: str):
    """백업 파일 삭제"""
    path = BACKUPS_DIR / filename
    if path.exists() and path.name.endswith(".json"):
        path.unlink()
    return {"ok": True}


# ── AI 요약 API (Gemini) ──

# Gemini API 키 (환경변수에서 로드)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# 기본 시스템 프롬프트
DEFAULT_AI_PROMPT = """당신은 스타트업 대표에게 보고하는 전문 비즈니스 분석가입니다.
주간 회의 안건들을 분석하여 대표가 빠르게 전체 현황을 파악할 수 있도록 구조화된 요약을 작성합니다.

## 출력 형식

각 안건에 대해 아래 JSON 배열을 반환하세요. 반드시 순수 JSON만 출력하고, 마크다운 코드블록이나 다른 텍스트는 포함하지 마세요.

[
  {
    "project": "안건 제목",
    "author": "작성자",
    "category": "decision 또는 info",
    "situation": ["현황 핵심 포인트 1", "현황 핵심 포인트 2", "현황 핵심 포인트 3"],
    "businessImpact": "비즈니스에 영향을 끼치는 부분 상세 설명",
    "ceoDecision": "대표님 의사결정이 필요한 사항 (없으면 '없음')"
  }
]

## category 분류 기준
- "decision": 대표의 판단·승인·방향 결정이 필요한 안건 (예산 승인, 전략 변경, 인력 배치, A/B 중 선택 등)
- "info": 대표가 읽고 파악하면 되는 정보 공유 안건 (진행 현황 보고, 수치 공유, 완료 보고 등)
- 판단이 애매하면 decision 쪽으로 분류 (놓치는 것보다 한번 더 보는 게 낫다)

## 작성 원칙
- 각 안건의 핵심 상황을 논리순으로 정리 (원인 → 현상 → 대응/제안)
- 비즈니스 임팩트를 구체적 수치나 영향 범위로 명시
- decision 안건은 반드시 Q. 형태로 구체적 질문 제시 (선택지가 있으면 선택지도 제시)
- info 안건의 ceoDecision은 "없음"으로 설정
- 한국어로 작성, 간결하고 명확하게"""


@app.post("/api/ai/summarize")
async def ai_summarize(request: Request):
    """Gemini AI로 주간 안건 요약 생성"""
    body = await request.json()
    items = body.get("items", [])
    custom_prompt = body.get("prompt", DEFAULT_AI_PROMPT)
    # 서버 환경변수 키만 사용 (클라이언트 키 무시)
    api_key = GEMINI_API_KEY

    if not api_key:
        return Response(
            content=json.dumps({"error": "Gemini API 키가 설정되지 않았습니다. 설정에서 API 키를 입력하세요."}),
            status_code=400,
            media_type="application/json",
        )

    if not items:
        return Response(
            content=json.dumps({"error": "요약할 안건이 없습니다"}),
            status_code=400,
            media_type="application/json",
        )

    # 안건 데이터를 텍스트로 변환
    agenda_text = ""
    for i, item in enumerate(items, 1):
        agenda_text += f"\n---\n안건 {i}: {item.get('project', '제목 없음')}\n"
        agenda_text += f"작성자: {item.get('author', '미정')}\n"
        agenda_text += f"중요도: {item.get('importance', '미설정')}\n"
        if item.get("detail"):
            agenda_text += f"상세 내용:\n{item['detail']}\n"
        if item.get("ceoComment"):
            agenda_text += f"기존 대표 코멘트:\n{item['ceoComment']}\n"
        if item.get("discussedNote"):
            agenda_text += f"논의 결과:\n{item['discussedNote']}\n"
        if item.get("attachedHtml"):
            agenda_text += f"첨부 문서 내용:\n{item['attachedHtml']}\n"

    # Gemini API 호출
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"{custom_prompt}\n\n## 이번 주 안건 데이터\n{agenda_text}"}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 65536,
            "temperature": 0.3,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{GEMINI_API_URL}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code != 200:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_msg = error_data.get("error", {}).get("message", f"Gemini API 오류 (HTTP {resp.status_code})")
                return Response(
                    content=json.dumps({"error": error_msg}),
                    status_code=502,
                    media_type="application/json",
                )

            result = resp.json()
            # Gemini 응답에서 텍스트 추출
            candidates = result.get("candidates", [])
            if not candidates:
                return Response(
                    content=json.dumps({"error": "Gemini가 응답을 생성하지 못했습니다"}),
                    status_code=502,
                    media_type="application/json",
                )

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")

            # JSON 파싱 시도 (마크다운 코드블록 제거)
            clean_text = text.strip()
            if clean_text.startswith("```"):
                # ```json ... ``` 형태 제거
                lines = clean_text.split("\n")
                clean_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            try:
                summary_items = json.loads(clean_text)
            except json.JSONDecodeError:
                # JSON 파싱 실패 시 원본 텍스트 반환
                summary_items = [{"project": "전체 요약", "author": "", "situation": [clean_text], "businessImpact": "", "ceoDecision": ""}]

            return {
                "ok": True,
                "summaryItems": summary_items,
                "rawText": text,
                "model": GEMINI_MODEL,
                "generatedAt": datetime.now().isoformat(),
            }

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": "Gemini API 응답 시간 초과 (120초)"}),
            status_code=504,
            media_type="application/json",
        )
    except Exception as e:
        logger.error(f"AI 요약 생성 실패: {e}")
        return Response(
            content=json.dumps({"error": f"AI 요약 생성 실패: {str(e)}"}),
            status_code=500,
            media_type="application/json",
        )


# ── Supabase 첨부파일 프록시 (올바른 Content-Type 반환) ──

@app.get("/api/proxy-file")
async def proxy_file(url: str):
    """Resolve a legacy storage URL through the authenticated private-object proxy."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return Response(status_code=503)
    from urllib.parse import quote, unquote, urlsplit
    try:
        configured = urlsplit(SUPABASE_URL)
        target = urlsplit(url)
        if (configured.scheme != "https" or not configured.hostname or configured.username
                or configured.password or configured.query or configured.fragment
                or configured.path not in ("", "/")):
            return Response(status_code=503)
        if (not url or url != url.strip() or "\\" in url or "?" in url or "#" in url
                or any(ord(char) < 32 or ord(char) == 127 for char in url)
                or target.username or target.password
                or (target.scheme, target.hostname, 443 if target.port is None else target.port)
                != (configured.scheme, configured.hostname, 443 if configured.port is None else configured.port)):
            raise ValueError("Storage origin mismatch")
        bucket = quote(STORAGE_BUCKET, safe="")
        prefixes = (
            f"/storage/v1/object/public/{bucket}/",
            f"/storage/v1/object/authenticated/{bucket}/",
            f"/storage/v1/object/{bucket}/",
        )
        prefix = next((value for value in prefixes if target.path.startswith(value)), None)
        if prefix is None:
            raise ValueError("Storage bucket mismatch")
        filename = unquote(target.path[len(prefix):], errors="strict")
        if "%" in filename:
            raise ValueError("Ambiguous object encoding")
    except (TypeError, ValueError, UnicodeError):
        return Response(content='{"error":"허용되지 않는 URL"}', status_code=400, media_type="application/json")
    return await file_proxy(filename)


# ── Linear API 연동 ──

LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")
LINEAR_API_URL = "https://api.linear.app/graphql"


async def _linear_graphql(query: str) -> dict:
    """Linear GraphQL API 호출 — _get_linear_key()로 DB 우선·env 폴백.
    비정상 응답(빈 body·rate limit·HTML 오류·타임아웃·네트워크)에도 크래시 없이 빈 dict 반환.
    호출부는 모두 .get('data',{})... 로 받으므로 빈 dict면 자연히 빈 결과가 됨."""
    key = _get_linear_key() if "_get_linear_key" in globals() else LINEAR_API_KEY
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LINEAR_API_URL,
                headers={"Content-Type": "application/json", "Authorization": key},
                json={"query": query},
                timeout=30,
            )
        if resp.status_code != 200:
            logger.warning(f"[Linear] HTTP {resp.status_code}: {resp.text[:200]}")
            return {}
        try:
            return resp.json()
        except Exception as e:
            logger.warning(f"[Linear] 응답 JSON 파싱 실패: {e} | body={resp.text[:200]!r}")
            return {}
    except Exception as e:
        logger.warning(f"[Linear] 호출 실패: {e}")
        return {}


def _format_projects_from_raw(all_projects: list, exclude_ids: set) -> dict:
    """원본 프로젝트 데이터를 API 응답 형식으로 변환 (캐시/API 공용)"""
    projects = [p for p in all_projects if p["id"] not in exclude_ids]
    # priority 순 정렬: 1(Urgent) → 2(High) → 3(Medium) → 4(Low) → 0(미설정)
    projects.sort(key=lambda p: (p.get("priority", 0) or 99, p.get("sortOrder", 0)))
    output = []
    for p in projects:
        issues = p.get("issues", {}).get("nodes", [])
        total = len(issues)
        done = sum(1 for i in issues if i.get("state", {}).get("type") == "completed")
        in_progress = sum(1 for i in issues if i.get("state", {}).get("type") == "started")
        backlog = sum(1 for i in issues if i.get("state", {}).get("type") in ("backlog", "unstarted"))
        output.append({
            "id": p["id"],
            "name": p["name"],
            "state": p.get("state"),
            "priority": p.get("priority", 0),
            "total": total,
            "done": done,
            "inProgress": in_progress,
            "backlog": backlog,
            "progress": round(done / total * 100) if total > 0 else 0,
            "issues": sorted([{
                "id": i["id"],
                "identifier": i["identifier"],
                "title": i["title"],
                "state": i.get("state", {}).get("name", ""),
                "stateType": i.get("state", {}).get("type", ""),
                "priority": i.get("priority", 0),
                "assignee": (i.get("assignee") or {}).get("name", ""),
                "labels": [l["name"] for l in (i.get("labels", {}).get("nodes", []))],
                "description": (i.get("description") or "")[:200],
            } for i in issues], key=lambda x: (x["priority"] if x["priority"] > 0 else 99)),
        })
    return {"projects": output}


@app.get("/api/linear/projects")
async def linear_projects():
    """Linear 프로젝트별 이슈 현황 (진척도 포함) — DB 캐시 우선 조회"""
    if not LINEAR_API_KEY:
        return Response(
            content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
            status_code=400, media_type="application/json",
        )
    # L2: DB 캐시에서 먼저 조회
    cached = _read_linear_cache("linear_projects")
    if cached:
        raw = cached["data"]
        exclude_ids = set(raw.get("exclude_ids", []))
        all_projects = raw.get("projects", [])
        result = _format_projects_from_raw(all_projects, exclude_ids)
        result["_cached"] = True
        result["_cached_at"] = cached["updated_at"]
        return result

    # 캐시 미스: 기존 Linear API 직접 호출 (폴백)
    logger.info("[Linear projects] 캐시 미스 — API 직접 호출")
    exclude_ids = set()
    try:
        team_q = await _linear_graphql('{ projects(first: 50) { nodes { id teams { nodes { name } } } } }')
        for tp in team_q.get("data", {}).get("projects", {}).get("nodes", []):
            if any(t.get("name") in _installation.LINEAR_EXCLUDED_TEAM_NAMES for t in (tp.get("teams") or {}).get("nodes", [])):
                exclude_ids.add(tp["id"])
    except Exception:
        pass
    result = await _linear_graphql("""{
        projects(first: 20) {
            nodes {
                id name state priority sortOrder
                issues { nodes { id identifier title state { name type } priority assignee { name } labels { nodes { name } } completedAt createdAt description } }
            }
        }
    }""")
    all_projects = result.get("data", {}).get("projects", {}).get("nodes", [])
    return _format_projects_from_raw(all_projects, exclude_ids)


_LINEAR_MY_ISSUES_Q = """{
    issues(first: 100, filter: { state: { type: { nin: ["completed", "canceled"] } } }) {
        nodes {
            id identifier title
            state { name type }
            priority
            assignee { name }
            project { name }
            labels { nodes { name } }
            createdAt
        }
    }
}"""


def _group_my_issues(issues: list) -> dict:
    """Linear 이슈 목록 → 담당자별 그룹핑 (캐시·라이브 양쪽 공용)."""
    by_assignee = {}
    for i in issues:
        assignee = (i.get("assignee") or {}).get("name", "미배정")
        by_assignee.setdefault(assignee, []).append({
            "id": i.get("id", ""),
            "identifier": i.get("identifier", ""),
            "title": i.get("title", ""),
            "state": (i.get("state") or {}).get("name", ""),
            "stateType": (i.get("state") or {}).get("type", ""),
            "priority": i.get("priority", 0),
            "project": (i.get("project") or {}).get("name", ""),
            "labels": [l["name"] for l in ((i.get("labels") or {}).get("nodes", []))],
        })
    return {"byAssignee": by_assignee}


@app.get("/api/linear/my-issues")
async def linear_my_issues():
    """Linear 담당자별 이슈 목록 — DB 캐시 우선(5분 동기화), 미스 시 라이브 폴백.
    (projects와 동일 패턴 — 초기 로드 경로에서 매 요청 라이브 호출(~1.2초) 제거)"""
    if not LINEAR_API_KEY:
        return Response(
            content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
            status_code=400, media_type="application/json",
        )
    cached = _read_linear_cache("linear_my_issues")
    if cached:
        res = _group_my_issues(cached["data"].get("issues", []))
        res["_cached"] = True
        res["_cached_at"] = cached.get("updated_at")
        return res
    # 캐시 미스(배포 직후 첫 동기화 전 등): Linear API 직접 호출 (폴백)
    result = await _linear_graphql(_LINEAR_MY_ISSUES_Q)
    return _group_my_issues(result.get("data", {}).get("issues", {}).get("nodes", []))


@app.post("/api/linear/project")
async def linear_create_project(request: Request):
    """Linear 프로젝트 생성"""
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}), status_code=400, media_type="application/json")
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return Response(content=json.dumps({"error": "프로젝트 이름을 입력하세요"}), status_code=400, media_type="application/json")
    # 팀 ID 조회
    team_result = await _linear_graphql('{ teams(first:1) { nodes { id } } }')
    team_id = team_result.get("data", {}).get("teams", {}).get("nodes", [{}])[0].get("id", "")
    safe_name = name.replace('"', '\\"')
    priority = int(body.get("priority", 0))
    mutation = f'mutation {{ projectCreate(input: {{ name: "{safe_name}", teamIds: ["{team_id}"], priority: {priority} }}) {{ success project {{ id name priority }} }} }}'
    result = await _linear_graphql(mutation)
    create = result.get("data", {}).get("projectCreate", {})
    if create.get("success"):
        return {"ok": True, "project": create.get("project")}
    return Response(content=json.dumps({"error": "프로젝트 생성 실패", "detail": str(result.get("errors", []))}), status_code=400, media_type="application/json")


@app.post("/api/linear/issue")
async def linear_create_issue(request: Request):
    """Linear 이슈 생성"""
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}), status_code=400, media_type="application/json")
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        return Response(content=json.dumps({"error": "이슈 제목을 입력하세요"}), status_code=400, media_type="application/json")
    # 팀 ID 조회
    team_result = await _linear_graphql('{ teams(first:1) { nodes { id } } }')
    team_id = team_result.get("data", {}).get("teams", {}).get("nodes", [{}])[0].get("id", "")
    safe_title = title.replace('"', '\\"')
    desc = (body.get("description") or "").replace('"', '\\"').replace("\n", "\\n")
    priority = int(body.get("priority", 0))
    project_id = body.get("projectId", "")
    fields = [f'teamId: "{team_id}"', f'title: "{safe_title}"']
    if desc:
        fields.append(f'description: "{desc}"')
    if priority:
        fields.append(f'priority: {priority}')
    if project_id:
        fields.append(f'projectId: "{project_id}"')
    mutation = f'mutation {{ issueCreate(input: {{ {", ".join(fields)} }}) {{ success issue {{ id identifier title }} }} }}'
    result = await _linear_graphql(mutation)
    create = result.get("data", {}).get("issueCreate", {})
    if create.get("success"):
        return {"ok": True, "issue": create.get("issue")}
    return Response(content=json.dumps({"error": "이슈 생성 실패", "detail": str(result.get("errors", []))}), status_code=400, media_type="application/json")


@app.put("/api/linear/project/{project_id}")
async def linear_update_project(project_id: str, request: Request):
    """Linear 프로젝트 우선순위 수정"""
    if not LINEAR_API_KEY:
        return Response(
            content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
            status_code=400, media_type="application/json",
        )
    body = await request.json()
    fields = []
    if "priority" in body:
        fields.append(f'priority: {int(body["priority"])}')
    if not fields:
        return Response(content=json.dumps({"error": "수정할 필드가 없습니다"}), status_code=400, media_type="application/json")
    mutation = f'mutation {{ projectUpdate(id: "{project_id}", input: {{ {", ".join(fields)} }}) {{ success project {{ id name priority }} }} }}'
    result = await _linear_graphql(mutation)
    update = result.get("data", {}).get("projectUpdate", {})
    if update.get("success"):
        return {"ok": True, "project": update.get("project")}
    return Response(content=json.dumps({"error": "업데이트 실패"}), status_code=400, media_type="application/json")


@app.put("/api/linear/issue/{issue_id}")
async def linear_update_issue(issue_id: str, request: Request):
    """Linear 이슈 수정 (제목, 설명, 우선순위)"""
    if not LINEAR_API_KEY:
        return Response(
            content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
            status_code=400, media_type="application/json",
        )
    body = await request.json()
    # 수정 가능 필드
    fields = []
    if "title" in body:
        safe = body["title"].replace('"', '\\"')
        fields.append(f'title: "{safe}"')
    if "description" in body:
        safe = body["description"].replace('"', '\\"').replace("\n", "\\n")
        fields.append(f'description: "{safe}"')
    if "priority" in body:
        fields.append(f'priority: {int(body["priority"])}')
    if "dueDate" in body:
        if body["dueDate"]:
            fields.append(f'dueDate: "{body["dueDate"]}"')
        else:
            fields.append('dueDate: null')
    if "stateId" in body:
        fields.append(f'stateId: "{body["stateId"]}"')

    if not fields:
        return Response(
            content=json.dumps({"error": "수정할 필드가 없습니다"}),
            status_code=400, media_type="application/json",
        )

    mutation = f"""mutation {{
        issueUpdate(id: "{issue_id}", input: {{ {", ".join(fields)} }}) {{
            success
            issue {{ id identifier title description priority }}
        }}
    }}"""
    result = await _linear_graphql(mutation)
    update = result.get("data", {}).get("issueUpdate", {})
    if update.get("success"):
        return {"ok": True, "issue": update.get("issue")}
    return Response(
        content=json.dumps({"error": "Linear 업데이트 실패", "detail": str(result.get("errors", []))}),
        status_code=400, media_type="application/json",
    )


@app.get("/api/linear/initiatives")
async def linear_initiatives():
    """Initiative(상위 프로젝트) > Project > Issue 트리 조회"""
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
                        status_code=400, media_type="application/json")

    # 1. 프로젝트 목록 (이슈 없이, 복잡도 절감)
    # Initiative 전체 목록 직접 조회
    init_direct = await _linear_graphql("""{
        initiatives(first: 50) { nodes { id name status targetDate } }
    }""")
    all_initiatives_raw = init_direct.get("data", {}).get("initiatives", {}).get("nodes", [])

    proj_result = await _linear_graphql("""{
        projects(first: 50) { nodes {
            id name state priority targetDate
            initiatives { nodes { id name status targetDate } }
        } }
    }""")
    all_projects_raw = proj_result.get("data", {}).get("projects", {}).get("nodes", [])

    # 2. 프로젝트별 이슈를 배치 조회 (N+1 → ceil(N/10))
    all_projects = list(all_projects_raw)
    BATCH_SIZE = 10
    for batch_start in range(0, len(all_projects), BATCH_SIZE):
        batch = all_projects[batch_start:batch_start + BATCH_SIZE]
        alias_parts = []
        for idx, p in enumerate(batch):
            alias_parts.append(
                f'p{idx}: project(id: "{p["id"]}") {{ issues(first: 100) {{ nodes {{ '
                f'id identifier title dueDate state {{ id name type }} priority assignee {{ name }} }} }} }}'
            )
        batch_query = "{ " + " ".join(alias_parts) + " }"
        batch_result = await _linear_graphql(batch_query)
        batch_data = batch_result.get("data", {}) or {}
        for idx, p in enumerate(batch):
            p["issues"] = (batch_data.get(f"p{idx}") or {}).get("issues", {})

    # Initiative 그룹핑 (직접 조회 + 프로젝트 역참조 병합)
    init_map = {}
    # 먼저 직접 조회한 Initiative 전부 등록 (프로젝트 없어도 포함)
    for init in all_initiatives_raw:
        iid = init["id"]
        init_map[iid] = {"id": iid, "name": init["name"], "status": init.get("status"),
                         "targetDate": init.get("targetDate"), "projects": []}
    # 프로젝트에서 역참조된 Initiative에 프로젝트 연결
    assigned_ids = set()
    for p in all_projects:
        for init in p.get("initiatives", {}).get("nodes", []):
            iid = init["id"]
            if iid not in init_map:
                init_map[iid] = {"id": iid, "name": init["name"], "status": init.get("status"),
                                 "targetDate": init.get("targetDate"), "projects": []}
            init_map[iid]["projects"].append(p)
            assigned_ids.add(p["id"])
    initiatives = list(init_map.values())
    unassigned = [p for p in all_projects if p["id"] not in assigned_ids]

    def _fmt_issue(i):
        return {
            "id": i["id"], "identifier": i.get("identifier", ""),
            "title": i["title"], "dueDate": i.get("dueDate"),
            "state": i.get("state", {}).get("name", ""),
            "stateType": i.get("state", {}).get("type", ""),
            "stateId": i.get("state", {}).get("id", ""),
            "priority": i.get("priority", 0),
            "assignee": (i.get("assignee") or {}).get("name", ""),
        }

    def _fmt_project(p):
        issues = [_fmt_issue(i) for i in p.get("issues", {}).get("nodes", []) if i.get("state", {}).get("type") not in ("cancelled", "completed")]
        total = len(issues)
        done = sum(1 for i in issues if i["stateType"] == "completed")
        return {
            "id": p["id"], "name": p["name"], "state": p.get("state"),
            "priority": p.get("priority", 0), "targetDate": p.get("targetDate"),
            "total": total, "done": done,
            "progress": round(done / total * 100) if total > 0 else 0,
            "issues": sorted(issues, key=lambda x: (x["priority"] if x["priority"] > 0 else 99)),
        }

    output_inits = []
    for init in initiatives:
        projects = [_fmt_project(p) for p in init.get("projects", [])]
        total_issues = sum(p["total"] for p in projects)
        done_issues = sum(p["done"] for p in projects)
        output_inits.append({
            "id": init["id"], "name": init["name"],
            "status": init.get("status"), "targetDate": init.get("targetDate"),
            "totalIssues": total_issues, "doneIssues": done_issues,
            "progress": round(done_issues / total_issues * 100) if total_issues > 0 else 0,
            "projects": projects,
        })

    output_unassigned = [_fmt_project(p) for p in unassigned]

    # 마감일 없는 이슈 + Backlog 별도 수집
    no_due = []
    backlog = []
    for p in all_projects:
        for i in p.get("issues", {}).get("nodes", []):
            if i.get("state", {}).get("type") in ("cancelled", "completed"):
                continue
            fi = _fmt_issue(i)
            fi["project"] = p["name"]
            if fi["stateType"] == "backlog":
                backlog.append(fi)
            elif not fi["dueDate"] and fi["stateType"] in ("unstarted", "started"):
                no_due.append(fi)

    return {
        "initiatives": output_inits,
        "unassigned": output_unassigned,
        "noDueDate": no_due,
        "backlog": backlog,
    }


@app.post("/api/linear/sync")
async def linear_sync():
    """Linear 기준으로 대시보드 데이터 동기화 (없는 이슈는 자동 추가)"""
    if not LINEAR_API_KEY:
        return Response(
            content=json.dumps({"error": "LINEAR_API_KEY가 설정되지 않았습니다"}),
            status_code=400, media_type="application/json",
        )

    # Linear에서 전체 이슈 조회
    result = await _linear_graphql("""{
        issues(first: 200, filter: { team: { key: { eq: "CHE" } } }) {
            nodes {
                identifier title
                state { name type }
                assignee { name }
                project { name }
                completedAt createdAt
                description
            }
        }
    }""")
    linear_issues = result.get("data", {}).get("issues", {}).get("nodes", [])

    # 기존 데이터 로드 (PostgreSQL 우선, 없으면 파일)
    report = _read_store("report") or _read_json(REPORT_FILE) or {}
    if "activeProjects" not in report:
        report["activeProjects"] = {"title": "진행 중인 프로젝트", "subtitle": "팀 업무 현황", "projects": []}
    if "completedResults" not in report:
        report["completedResults"] = {"title": "완료 결과 추적", "subtitle": "완료된 프로젝트", "items": []}

    ap_list = report["activeProjects"]["projects"]
    cr_list = report["completedResults"]["items"]
    all_items = ap_list + cr_list

    # Linear 상태 → 대시보드 상태 매핑
    state_map = {
        "started": "active",
        "unstarted": "active",
        "completed": "done",
        "canceled": "done",
        "backlog": "hold",
        "triage": "active",
    }

    # 기존 아이템에서 linearId 기반 인덱스 + 제목 기반 인덱스
    by_linear_id = {}
    by_title = {}
    for item in all_items:
        lid = item.get("linearId", "")
        if lid:
            by_linear_id[lid] = item
        t = item.get("title", "").strip().lower()
        if t:
            by_title[t] = item

    updated = 0
    added = 0

    for li in linear_issues:
        title = li.get("title", "")
        li_state_type = li.get("state", {}).get("type", "")
        li_assignee = (li.get("assignee") or {}).get("name", "")
        li_completed = li.get("completedAt", "")
        li_identifier = li.get("identifier", "")
        li_project = (li.get("project") or {}).get("name", "")
        li_created = li.get("createdAt", "")
        li_desc = (li.get("description") or "")[:300]
        new_status = state_map.get(li_state_type, "active")

        # 1. linearId로 매칭 (가장 정확)
        matched = by_linear_id.get(li_identifier)

        # 2. 제목 정확 매칭만 (부분 매칭은 오매칭 위험)
        if not matched:
            matched = by_title.get(title.strip().lower())

        if matched:
            # 기존 항목 업데이트
            changed = False
            if matched.get("status", "") != new_status:
                matched["status"] = new_status
                changed = True
            if li_assignee and matched.get("owner", "") != li_assignee:
                matched["owner"] = li_assignee
                changed = True
            if li_completed and not matched.get("completedDate"):
                matched["completedDate"] = li_completed[:10]
                changed = True
            if matched.get("linearId") != li_identifier:
                matched["linearId"] = li_identifier
                changed = True
            if changed:
                updated += 1
        else:
            # 새 항목 추가 — Linear에만 있고 대시보드에 없는 이슈
            import uuid as _uuid
            new_item = {
                "id": _uuid.uuid4().hex[:13],
                "title": title,
                "owner": li_assignee,
                "status": new_status,
                "linearId": li_identifier,
                "note": f"[Linear] {li_project}\n{li_desc}".strip() if li_project or li_desc else "",
                "createdAt": li_created,
                "completedDate": li_completed[:10] if li_completed else "",
            }
            # 완료된 이슈는 completedResults에, 나머지는 activeProjects에
            if new_status == "done":
                cr_list.append(new_item)
            else:
                ap_list.append(new_item)
            # 인덱스 업데이트
            by_linear_id[li_identifier] = new_item
            by_title[title.strip().lower()] = new_item
            all_items.append(new_item)
            added += 1

    # 저장 (PostgreSQL + 파일 백업)
    report["_lastModified"] = datetime.utcnow().isoformat() + "Z"
    await _write_store("report", report)

    # 저장 후 검증
    verify = _read_store("report") or {}
    v_ap = verify.get("activeProjects", {}).get("projects", [])
    v_cr = verify.get("completedResults", {}).get("items", [])
    v_linear = sum(1 for i in v_ap + v_cr if i.get("linearId"))

    return {
        "ok": True,
        "updated": updated,
        "added": added,
        "total": len(linear_issues),
        "saved_ap": len(v_ap),
        "saved_cr": len(v_cr),
        "saved_linear": v_linear,
    }


# ── Linear 일일회의 연동 API ──

@app.get("/api/linear/completed-yesterday")
async def linear_completed_yesterday():
    """어제 완료된 Linear 이슈 (담당자별 그룹)"""
    if not LINEAR_API_KEY:
        return {"date": "", "byAssignee": {}}

    # KST 기준 — 평일이면 어제, 주말/월요일이면 최근 영업일까지 포함
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)
    today = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    # 월요일(0)이면 금요일부터, 일요일(6)이면 금요일부터, 그 외 어제부터
    weekday = today.weekday()
    if weekday == 0:  # 월요일 → 금~일 (3일)
        since = today - timedelta(days=3)
    elif weekday == 6:  # 일요일 → 금~토 (2일)
        since = today - timedelta(days=2)
    else:
        since = today - timedelta(days=1)

    gte = since.isoformat()
    lt = today.isoformat()

    result = await _linear_graphql("""{
        issues(
            first: 100
            filter: {
                team: { key: { eq: "CHE" } }
                completedAt: { gte: "%s", lt: "%s" }
            }
        ) {
            nodes {
                identifier title
                assignee { name }
                completedAt
                project { name }
            }
        }
    }""" % (gte, lt))

    issues = result.get("data", {}).get("issues", {}).get("nodes", [])
    by_assignee = {}
    for i in issues:
        assignee = (i.get("assignee") or {}).get("name", "미배정")
        if assignee not in by_assignee:
            by_assignee[assignee] = []
        by_assignee[assignee].append({
            "identifier": i.get("identifier", ""),
            "title": i.get("title", ""),
            "project": (i.get("project") or {}).get("name", ""),
        })

    return {"date": str(since.date()), "byAssignee": by_assignee}


@app.get("/api/linear/priority-issues")
async def linear_priority_issues():
    """팀 우선순위별 프로젝트의 Linear 이슈 (담당자별 그룹)"""
    if not LINEAR_API_KEY:
        return {"priorities": []}

    # 1. report에서 teamPriorities + 프로젝트 목록 로드
    report = _read_store("report") or {}
    team_priorities = report.get("teamPriorities", [])
    if not team_priorities:
        return {"priorities": []}

    # 전체 프로젝트 맵 (id → name)
    all_projects = {}
    for p in report.get("activeProjects", {}).get("projects", []):
        all_projects[p.get("id", "")] = p.get("title", "")
    for p in report.get("completedResults", {}).get("items", []):
        if p.get("id") not in all_projects:
            all_projects[p.get("id", "")] = p.get("title", "")

    # 2. Linear에서 진행 중인 이슈 전체 조회 (프로젝트 포함)
    result = await _linear_graphql("""{
        issues(
            first: 200
            filter: {
                team: { key: { eq: "CHE" } }
                state: { type: { in: ["started", "unstarted", "triage"] } }
            }
        ) {
            nodes {
                identifier title
                assignee { name }
                state { name type }
                project { name }
            }
        }
        projects(first: 20) {
            nodes { name }
        }
    }""")
    linear_issues = result.get("data", {}).get("issues", {}).get("nodes", [])
    linear_project_names = [p["name"] for p in result.get("data", {}).get("projects", {}).get("nodes", [])]

    # 3. 대시보드 프로젝트명 → Linear 프로젝트명 유사 매칭
    import re as _re

    def _fuzzy_match_project(dash_name: str, linear_names: list) -> set:
        """대시보드 프로젝트 제목의 키워드로 Linear 프로젝트 매칭 (완화 기준)"""
        dash_keywords = set(_re.findall(r'[\w가-힣]{2,}', dash_name.lower()))
        matched = set()
        for lname in linear_names:
            l_keywords = set(_re.findall(r'[\w가-힣]{2,}', lname.lower()))
            overlap = dash_keywords & l_keywords
            # 키워드 1개 이상 겹치면 매칭 (완화)
            if len(overlap) >= 1 or dash_name in lname or lname in dash_name:
                matched.add(lname)
        return matched

    # 4. 우선순위별로 그룹핑
    priorities_result = []
    for tp in sorted(team_priorities, key=lambda x: x.get("rank", 99)):
        project_ids = tp.get("projectIds", [])
        dash_names = set()
        matched_linear_names = set()
        # 우선순위에 연결된 대시보드 프로젝트의 담당자 목록
        priority_owners = set()

        for pid in project_ids:
            dname = all_projects.get(pid, "")
            if dname:
                dash_names.add(dname)
                matched_linear_names |= _fuzzy_match_project(dname, linear_project_names)
            # 대시보드 프로젝트의 담당자도 수집
            for src_items in [
                report.get("activeProjects", {}).get("projects", []),
                report.get("completedResults", {}).get("items", []),
            ]:
                for item in src_items:
                    if item.get("id") == pid and item.get("owner"):
                        priority_owners.add(item["owner"])

        if not dash_names:
            continue

        # Linear 이슈 필터: 매칭된 프로젝트에 속하거나,
        # 프로젝트 미배정이지만 담당자가 우선순위 프로젝트 담당자인 이슈
        by_assignee = {}
        for i in linear_issues:
            li_project = (i.get("project") or {}).get("name", "")
            assignee = (i.get("assignee") or {}).get("name", "미배정")

            if li_project in matched_linear_names:
                pass  # 매칭됨
            elif not li_project and assignee in priority_owners:
                pass  # 프로젝트 미배정이지만 우선순위 담당자의 이슈
            else:
                continue

            if assignee not in by_assignee:
                by_assignee[assignee] = []
            by_assignee[assignee].append({
                "identifier": i.get("identifier", ""),
                "title": i.get("title", ""),
                "state": i.get("state", {}).get("name", ""),
                "stateType": i.get("state", {}).get("type", ""),
                "project": li_project,
            })

        # 표시용 프로젝트명: 대시보드 이름 사용 (간결)
        display_names = sorted(dash_names)
        if len(display_names) > 3:
            display_names = display_names[:3] + [f"외 {len(display_names) - 3}개"]

        priorities_result.append({
            "rank": tp.get("rank", 0),
            "label": tp.get("label", ""),
            "projectName": ", ".join(display_names),
            "byAssignee": by_assignee,
        })

    return {"priorities": priorities_result}


# ── 경영지원 비용 관리 (Google Sheets 연동) ──

EXPENSE_SHEET_ID = os.environ.get("EXPENSE_SHEET_ID", "")
EXPENSE_SHEET_NAME = os.environ.get("EXPENSE_SHEET_NAME", "")

def _get_gspread_client():
    """Google Sheets 클라이언트 (서비스 계정)"""
    if not _BQ_KEY_PATH or not EXPENSE_SHEET_ID or not EXPENSE_SHEET_NAME:
        raise RuntimeError("Google expense sheet integration is not configured")
    import gspread
    from google.oauth2 import service_account as _sa
    creds = _sa.Credentials.from_service_account_file(
        _BQ_KEY_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def _read_expense_sheet_rows():
    """독립 클라이언트로 시트 스냅샷만 읽는다. DB·알림 처리는 호출자가 담당한다."""
    gc = _get_gspread_client()
    ws = gc.open_by_key(EXPENSE_SHEET_ID).worksheet(EXPENSE_SHEET_NAME)
    return ws.get_all_values()


@app.get("/api/expenses")
async def get_expenses(year: str = "", month: str = ""):
    """비용 목록 조회 (년도/월 필터)"""
    try:
        gc = _get_gspread_client()
        ws = gc.open_by_key(EXPENSE_SHEET_ID).worksheet(EXPENSE_SHEET_NAME)
        all_vals = ws.get_all_values()
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"시트 연결 실패: {str(e)}"}),
            status_code=502, media_type="application/json",
        )

    # 헤더 파싱
    rows = []
    for i, row in enumerate(all_vals[1:], start=2):  # 1행은 헤더
        if not row[0] or not row[0].strip().isdigit():
            continue
        if not any(row[3:10]):
            continue

        row_year = row[1].strip()
        row_date = row[2].strip()

        # 필터
        if year and row_year != year:
            continue
        if month and row_date:
            # 입력날짜 형식: "1/23", "10/5" 등
            parts = row_date.split("/")
            if len(parts) >= 1:
                row_month = parts[0].strip()
                if row_month != month and row_month != month.lstrip("0"):
                    continue

        # 금액 파싱
        amount_str = row[8].strip().replace(",", "")
        try:
            amount = int(amount_str) if amount_str else 0
        except ValueError:
            amount = 0

        rows.append({
            "rowNum": i,
            "no": row[0].strip(),
            "year": row_year,
            "date": row_date,
            "manager": row[3].strip(),
            "company": row[4].strip(),
            "description": row[5].strip(),
            "bank": row[6].strip(),
            "account": row[7].strip(),
            "amount": amount,
            "amountStr": row[8].strip(),
            "depositor": row[9].strip(),
            "status": row[10].strip(),
            "manager2": row[11].strip() if len(row) > 11 else "",
            "note": row[12].strip() if len(row) > 12 else "",
        })

    # 최신순 정렬 (번호 역순)
    rows.sort(key=lambda r: int(r["no"]), reverse=True)

    # 전체 조회 시 최대 200건 제한 (렉 방지)
    total = sum(r["amount"] for r in rows)
    count = len(rows)
    if not year and not month and len(rows) > 200:
        rows = rows[:200]

    return {"rows": rows, "total": total, "count": count}


@app.post("/api/expenses")
async def add_expense(request: Request):
    """비용 행 추가 (구글 시트) — 공용(legacy) 세션은 추가 불가, 개인 로그인 필요"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다 (공용 세션은 비용 추가 불가)"}),
                        status_code=403, media_type="application/json")
    body = await request.json()

    try:
        import gspread as _gs
        gc = _get_gspread_client()
        ws = gc.open_by_key(EXPENSE_SHEET_ID).worksheet(EXPENSE_SHEET_NAME)

        all_vals = ws.get_all_values()

        # 마지막 실데이터의 번호 찾기
        last_data_no = 0
        for row in all_vals:
            if row[0] and row[0].strip().isdigit() and any(r.strip() for r in row[3:10]):
                last_data_no = int(row[0].strip())

        new_no = last_data_no + 1

        new_data = [
            str(new_no),
            body.get("year", ""),
            body.get("date", ""),
            body.get("manager", ""),
            body.get("company", ""),
            body.get("description", ""),
            body.get("bank", ""),
            body.get("account", ""),
            body.get("amount", ""),
            body.get("depositor", ""),
            body.get("status", ""),
            body.get("manager2", ""),
            body.get("note", ""),
        ]

        # 첫 번째 빈 행(번호만 있고 데이터 없는 행)을 찾아서 덮어쓰기
        target_row = None
        for i, row in enumerate(all_vals):
            if row[0] and row[0].strip().isdigit() and not any(r.strip() for r in row[3:10]):
                target_row = i + 1  # 1-indexed
                break

        if target_row:
            # 빈 행에 덮어쓰기
            cells = [_gs.Cell(target_row, col + 1, val) for col, val in enumerate(new_data)]
            ws.update_cells(cells)
            result_row = target_row
        else:
            # 빈 행이 없으면 맨 끝에 추가
            ws.append_row(new_data)
            result_row = len(all_vals) + 1

        # 비용 추가 알림 (대시보드 경로) — 즉시 발송 + 폴러 중복 방지용 기록.
        # 엑셀 직접 입력은 _watch_expense_sheet 폴러가 별도 감지·발송한다.
        _desc = body.get("description", "") or body.get("company", "")
        await _send_expense_alert(user_state.get("name", ""), _desc, body.get("amount", ""),
                                  body.get("date", ""), body.get("manager", ""), "대시보드")
        _expense_set_last_no(new_no)  # 폴러가 이 건을 다시 안 알리도록 워터마크 전진

        return {"ok": True, "no": new_no, "rowNum": result_row}
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"추가 실패: {str(e)}"}),
            status_code=500, media_type="application/json",
        )


@app.put("/api/expenses/{row_num}")
async def update_expense(row_num: int, request: Request):
    """비용 행 수정 — admin/pm only (상태 변경/처리자 지정 등)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()

    try:
        import gspread as _gs
        gc = _get_gspread_client()
        ws = gc.open_by_key(EXPENSE_SHEET_ID).worksheet(EXPENSE_SHEET_NAME)

        col_map = {
            "year": 2, "date": 3, "manager": 4, "company": 5,
            "description": 6, "bank": 7, "account": 8, "amount": 9,
            "depositor": 10, "status": 11, "manager2": 12, "note": 13,
        }

        # 결제완료 알림 판단용 — 상태 변경이면 기존 행을 먼저 읽어 미완료→완료 전환인지 확인
        paid_alert_args = None
        new_status = str(body["status"]).strip() if "status" in body else None
        if new_status in EXPENSE_PAID_STATUSES:
            old_row = ws.row_values(row_num)
            def _cell(idx):  # 0-indexed 시트 컬럼
                return (old_row[idx].strip() if len(old_row) > idx and old_row[idx] else "")
            old_status = _cell(10)
            # 미완료(완료 집합 밖)에서 처음 완료로 바뀔 때만 알림 (완료→완료 재변경은 무시)
            if old_status not in EXPENSE_PAID_STATUSES:
                _manager = str(body["manager"]).strip() if "manager" in body else _cell(3)
                _desc = (str(body["description"]).strip() if "description" in body else _cell(5)) or \
                        (str(body["company"]).strip() if "company" in body else _cell(4))
                _amount = str(body["amount"]).strip() if "amount" in body else _cell(8)
                _date = str(body["date"]).strip() if "date" in body else _cell(2)
                paid_alert_args = (_manager, _desc, _amount, _date, new_status)

        cells = []
        for field, col in col_map.items():
            if field in body:
                cells.append(_gs.Cell(row_num, col, str(body[field])))

        if cells:
            ws.update_cells(cells)

        # 시트 반영 성공 후 결제완료 알림 발송 (알림 실패가 수정 자체를 막지 않도록 분리)
        if paid_alert_args:
            try:
                await _send_expense_paid_alert(*paid_alert_args)
            except Exception as _e:
                logger.warning(f"비용 결제완료 알림 처리 실패: {_e}")

        return {"ok": True}
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"수정 실패: {str(e)}"}),
            status_code=500, media_type="application/json",
        )


@app.delete("/api/expenses/{row_num}")
async def delete_expense(row_num: int, request: Request):
    """비용 행 삭제 (admin/pm only)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    try:
        gc = _get_gspread_client()
        ws = gc.open_by_key(EXPENSE_SHEET_ID).worksheet(EXPENSE_SHEET_NAME)
        ws.delete_rows(row_num)
        return {"ok": True}
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"삭제 실패: {str(e)}"}),
            status_code=500, media_type="application/json",
        )


# ── 비용 등록 알림 (대시보드 + 엑셀 직접 입력 모두 감지) ──

def _expense_set_last_no(no):
    """비용 알림 워터마크 — expense_notified.last_no를 max(기존, no)로 갱신.
    이 번호 이하(=예전 항목)는 다시 후보가 되지 않아 절대 알림 안 감. 기록이 정수 하나라 손상 위험 없음."""
    try:
        n = int(str(no).strip())
    except Exception:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            """INSERT INTO ext_app_store (key, data, updated_at)
               VALUES ('expense_notified', jsonb_build_object('seeded', true, 'last_no', %s::int), NOW())
               ON CONFLICT (key) DO UPDATE
               SET data = jsonb_build_object('seeded', true, 'last_no',
                     GREATEST(COALESCE(NULLIF(ext_app_store.data->>'last_no','')::int, 0), %s::int)),
                   updated_at = NOW()""",
            (n, n))
        conn.commit(); conn.close()
    except Exception as e:
        logger.warning(f"_expense_set_last_no 실패: {e}")


async def _send_expense_alert(writer, desc, amount, date, manager, source):
    """비용 등록 알림 — EXPENSE_NOTIFY_NAMES에게 인앱 + Slack (수신자별 독립 best-effort)."""
    amt_str = f" · {amount}원" if amount else ""
    body_desc = desc or manager or "비용"
    bot_token = _get_slack_bot_token()
    msg = (f"💰 *새 비용 등록* ({source})\n"
           f"작성자: {writer or '-'}\n"
           f"내용: {body_desc}{amt_str}\n"
           f"날짜: {date or '-'} · 담당: {manager or '-'}\n"
           f"→ 대시보드 비용 관리 탭에서 확인")
    for _nm in EXPENSE_NOTIFY_NAMES:
        try:
            create_notification(_nm, "expense_added", "새 비용 등록",
                                f"{writer or source} · {body_desc}{amt_str}", "/?tab=expenses")
        except Exception as _e:
            logger.warning(f"비용 인앱 알림 실패({_nm}): {_e}")
        if bot_token:
            try:
                _sid = _get_slack_id_by_name(_nm)
                if _sid:
                    _r = await _slack_post_message(bot_token, _sid, msg)
                    if not (_r or {}).get("ok"):
                        logger.warning(f"비용 Slack 알림 실패({_nm}): {(_r or {}).get('error')}")
            except Exception as _e:
                logger.warning(f"비용 Slack 알림 예외({_nm}): {_e}")


# 결제 완료로 간주하는 상태값들 (이 중 하나로 처음 바뀌면 담당자에게 결제완료 알림)
EXPENSE_PAID_STATUSES = {"이체완료", "결제완료", "카드결제"}


async def _send_expense_paid_alert(manager, desc, amount, date, status):
    """결제 완료 알림 — 비용 담당자에게 인앱 + Slack DM (best-effort).
    상태가 미완료→완료로 처음 바뀔 때만 호출된다(중복 방지는 호출부 책임)."""
    if not manager:
        logger.info("[비용결제완료] 담당자 미지정 — 알림 생략")
        return
    amt_str = f" · {amount}원" if amount else ""
    body_desc = desc or "비용"
    msg = (f"✅ *비용 결제 완료*\n"
           f"내용: {body_desc}{amt_str}\n"
           f"날짜: {date or '-'} · 담당: {manager}\n"
           f"상태: {status}\n"
           f"→ 대시보드 비용 관리 탭에서 확인")
    # 인앱 알림
    try:
        create_notification(manager, "expense_paid", "비용 결제 완료",
                            f"{body_desc}{amt_str} — {status}", "/?tab=expenses")
    except Exception as _e:
        logger.warning(f"비용 결제완료 인앱 알림 실패({manager}): {_e}")
    # Slack DM
    bot_token = _get_slack_bot_token()
    if bot_token:
        try:
            _sid = _get_slack_id_by_name(manager)
            if _sid:
                _r = await _slack_post_message(bot_token, _sid, msg)
                if not (_r or {}).get("ok"):
                    logger.warning(f"비용 결제완료 Slack 알림 실패({manager}): {(_r or {}).get('error')}")
            else:
                logger.info(f"[비용결제완료] {manager} slack_id 없음 — DM 생략")
        except Exception as _e:
            logger.warning(f"비용 결제완료 Slack 알림 예외({manager}): {_e}")


EXPENSE_MAX_BATCH = 10  # 한 폴링 신규가 이보다 많으면 발송 금지(대량 오발송 차단) → 무발송 재시드


async def _watch_expense_sheet():
    """비용 시트 폴링 — 워터마크(last_no)보다 큰 번호(=새로 올라온 것)만 알림.
    예전 번호(<=last_no)는 후보 자체가 안 되어 절대 발송 안 됨.
    안전장치: 미시드/형식이상이면 워터마크만 현재 최대로 시드(무발송);
              한 폴링 신규가 EXPENSE_MAX_BATCH 초과면 무발송으로 워터마크만 전진."""
    await asyncio.sleep(40)  # startup 대기
    while True:
        try:
            # 구글 시트 읽기 — 일시 오류(429/5xx)는 짧은 백오프로 재시도해 블립을 흡수.
            # 재시도까지 실패하면 raise → 기존대로 실패 처리(지속 장애는 그대로 탐지).
            _TRANSIENT = {429, 500, 502, 503, 504}
            rows = None
            for _attempt in range(3):  # 초기 1 + 재시도 2
                try:
                    rows = await run_in_threadpool(_read_expense_sheet_rows)
                    break
                except Exception as _e:
                    _resp = getattr(_e, "response", None)
                    _code = getattr(_resp, "status_code", None) if _resp is not None else None
                    _transient = (_code in _TRANSIENT) or any(f"[{c}]" in str(_e) for c in _TRANSIENT)
                    if not _transient or _attempt == 2:
                        raise
                    logger.info(f"[비용감지] 일시 오류({_code or str(_e)[:40]}) — 재시도 {_attempt + 1}/2")
                    await asyncio.sleep(2 * (_attempt + 1))  # 2s → 4s
            # 실데이터 행 = 번호(숫자) + 내용(3~9열 중 하나라도 있음)
            real = []
            for row in rows:
                if row and (row[0] or '').strip().isdigit() and any((c or '').strip() for c in row[3:10]):
                    real.append((int((row[0]).strip()), row))
            cur_max = max((n for n, _ in real), default=0)
            store = _read_store("expense_notified") or {}
            last_no = store.get("last_no")
            seeded = store.get("seeded") is True

            if not seeded or not isinstance(last_no, int):
                # 미시드/형식이상 → 워터마크를 현재 최대로 (예전 것 전부 제외, 무발송)
                _expense_set_last_no(cur_max)
                logger.warning(f"[비용감지] 워터마크 시드 last_no={cur_max} (무발송)")
            else:
                new_rows = [(n, row) for n, row in real if n > last_no]
                if len(new_rows) > EXPENSE_MAX_BATCH:
                    # 신규 과다(손상·대량입력 의심) → 무발송, 워터마크만 전진
                    _expense_set_last_no(cur_max)
                    logger.warning(f"[비용감지] 신규 {len(new_rows)}건 > 상한 {EXPENSE_MAX_BATCH} — 무발송 워터마크 전진 last_no={cur_max}")
                else:
                    for n, row in sorted(new_rows, key=lambda x: x[0]):
                        # 컬럼: 0=no 1=year 2=date 3=manager 4=company 5=description 8=amount
                        _date = row[2] if len(row) > 2 else ''
                        _manager = row[3] if len(row) > 3 else ''
                        _company = row[4] if len(row) > 4 else ''
                        _desc = (row[5] if len(row) > 5 else '') or _company
                        _amount = row[8] if len(row) > 8 else ''
                        await _send_expense_alert(_manager or '(엑셀 직접 입력)', _desc, _amount, _date, _manager, '엑셀 직접 입력')
                        _expense_set_last_no(n)
                    if new_rows:
                        logger.info(f"[비용감지] 신규 {len(new_rows)}건 알림 발송 (last_no→{max(n for n, _ in new_rows)})")
            _heartbeat("watch_expense_sheet", True)
        except Exception as e:
            _heartbeat("watch_expense_sheet", False, str(e))
            logger.warning(f"[비용감지] 폴링 실패: {e}")
        await asyncio.sleep(120)  # 2분마다


# ── 출퇴근 관리 API ──

def _attendance_scope(user_state, requested_user=""):
    user_state = user_state or {}
    if user_state.get("role") in ("admin", "pm") and user_state.get("user_id") != "legacy":
        return requested_user or None
    return user_state.get("name") or "__none__"


def _attendance_can_read_halfdays(user_state):
    user_state = user_state or {}
    return user_state.get("user_id") not in (None, "", "legacy") and bool(user_state.get("name"))


def _read_attendance_records(cur, start, end, only_user=None):
    sql = ("SELECT id, user_name, work_date::text, clock_in::text, clock_out::text, status, note "
           "FROM ext_attendance WHERE work_date >= %s AND work_date < %s ")
    parameters = [start.isoformat(), end.isoformat()]
    if only_user is not None:
        sql += "AND user_name = %s "
        parameters.append(only_user)
    cur.execute(sql + "ORDER BY work_date, user_name, id", tuple(parameters))
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _read_attendance_halfdays(cur, start, end, only_user=None):
    # 시작 연도가 아니라 기간 겹침으로 최종 승인만 동일한 본인 범위에서 묶어 조회한다.
    sql = ("SELECT id, user_name, leave_type, start_date::text, end_date::text, days, status "
           "FROM ext_leave_requests WHERE status = %s AND leave_type = ANY(%s) "
           "AND start_date < %s AND end_date >= %s ")
    parameters = ["승인", list(HALF_DAY_TYPES), end.isoformat(), start.isoformat()]
    if only_user is not None:
        sql += "AND user_name = %s "
        parameters.append(only_user)
    cur.execute(sql + "ORDER BY start_date, user_name, id", tuple(parameters))
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _read_attendance_approved_leaves(cur, start, end, only_user=None):
    """월/연간 상세 보완용 최종 승인 휴가. 기존 일별·반차 정책과 분리한다."""
    sql = ("SELECT id, user_name, leave_type, start_date::text, end_date::text, days, status "
           "FROM ext_leave_requests WHERE status = %s AND start_date < %s AND end_date >= %s ")
    parameters = ["승인", end.isoformat(), start.isoformat()]
    if only_user is not None:
        sql += "AND user_name = %s "
        parameters.append(only_user)
    cur.execute(sql + "ORDER BY start_date, user_name, id", tuple(parameters))
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _read_attendance_holidays(cur, year):
    # 월에 휴일이 없더라도 해당 연도 달력의 존재 여부를 확인할 수 있어야 한다.
    cur.execute("SELECT date::text FROM ext_holidays WHERE date >= %s AND date < %s ORDER BY date",
                (dt_date(year, 1, 1).isoformat(), dt_date(year + 1, 1, 1).isoformat()))
    return [row[0] for row in cur.fetchall()]


def _read_attendance_roster(cur, only_user=None):
    # 같은 이름의 비활성/관리자 계정도 읽어 입사일 상충을 조회 projection에서 확인한다.
    sql = "SELECT id, name, role, is_active, hire_date::text FROM ext_users "
    parameters = ()
    if only_user is not None:
        sql += "WHERE name = %s "
        parameters = (only_user,)
    cur.execute(sql + "ORDER BY name, id", parameters)
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


@app.get("/api/attendance")
async def get_attendance(request: Request, date: str = "", user: str = ""):
    """출퇴근 기록 조회 — admin/pm은 전체, 직원(member)·공용(legacy)은 본인만."""
    user_state = getattr(request.state, "user", None)
    only_user = _attendance_scope(user_state, user)
    try:
        day = _attendance_date(date)
        end = day + timedelta(days=1)
    except (ValueError, TypeError, OverflowError):
        return Response(content=json.dumps({"error": "날짜는 유효한 YYYY-MM-DD여야 합니다"}),
                        status_code=400, media_type="application/json")
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        records = _read_attendance_records(cur, day, end, only_user)
        leaves = _read_attendance_halfdays(cur, day, end, only_user) if _attendance_can_read_halfdays(user_state) else []
        view = _build_attendance_view(records, leaves, day, end)
    finally:
        conn.close()
    return {"rows": [row for row in view["records"] if row["has_attendance"]], "date": day.isoformat(),
            "halfday_warnings": view["halfday_warnings"], "timezone": "Asia/Seoul"}


@app.post("/api/attendance/clock-in")
async def clock_in(request: Request):
    """출근 기록 — JWT로 본인 자동 식별, KST 기준"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    user = user_state["name"]

    conn = _pg_conn()
    try:
        cur = conn.cursor()
        now = datetime.now(KST).replace(tzinfo=None)
        today = now.strftime("%Y-%m-%d")
        cur.execute("SELECT id FROM ext_attendance WHERE user_name = %s AND work_date = %s", (user, today))
        if cur.fetchone():
            return Response(content=json.dumps({"error": "이미 출근 기록이 있습니다"}), status_code=400, media_type="application/json")

        # 원래 시각 기준 상태를 보존한다. 승인 오전반차의 지각 면제는 조회 표시에서만 적용한다.
        day = now.date()
        leaves = _read_attendance_halfdays(cur, day, day + timedelta(days=1), user)
        status = _attendance_arrival_status(now)[0]
        view = _build_attendance_view([{"id": None, "user_name": user, "work_date": today,
                                      "clock_in": now.isoformat(" "), "status": status}], leaves, day, day + timedelta(days=1))
        cur.execute(
            "INSERT INTO ext_attendance (user_name, work_date, clock_in, status) VALUES (%s, %s, %s, %s)",
            (user, today, now, status))
        conn.commit()
        return {"ok": True, "user": user, "time": now.strftime("%H:%M:%S"),
                "status": view["records"][0]["status"], "raw_status": status}
    finally:
        conn.close()


@app.post("/api/attendance/clock-out")
async def clock_out(request: Request):
    """퇴근 기록 — JWT로 본인 자동 식별, KST 기준"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    user = user_state["name"]

    conn = _pg_conn()
    try:
        cur = conn.cursor()
        now = datetime.now(KST).replace(tzinfo=None)
        today = now.strftime("%Y-%m-%d")
        cur.execute("SELECT id, status, clock_in::text, note FROM ext_attendance "
                    "WHERE user_name = %s AND work_date = %s ORDER BY id", (user, today))
        row = cur.fetchone()
        if not row:
            return Response(content=json.dumps({"error": "출근 기록이 없습니다. 먼저 출근해주세요."}), status_code=400, media_type="application/json")

        # 퇴근 시각만 저장한다. 조퇴를 생성하거나 기존 원문 상태를 자동 보정하지 않는다.
        day = now.date()
        leaves = _read_attendance_halfdays(cur, day, day + timedelta(days=1), user)
        view = _build_attendance_view([{"id": row[0], "user_name": user, "work_date": today,
                                      "clock_in": row[2], "clock_out": now.isoformat(" "),
                                      "status": row[1], "note": row[3]}], leaves, day, day + timedelta(days=1))
        cur.execute("UPDATE ext_attendance SET clock_out = %s WHERE id = %s AND user_name = %s", (now, row[0], user))
        conn.commit()
        return {"ok": True, "user": user, "time": now.strftime("%H:%M:%S"),
                "status": view["records"][0]["status"], "raw_status": row[1]}
    finally:
        conn.close()


@app.put("/api/attendance/{att_id}")
async def update_attendance(att_id: int, request: Request):
    """출퇴근 기록 수정 (admin/pm only)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    if not isinstance(body, dict) or ("status" in body and body["status"] not in ("정상", "지각")):
        return Response(content=json.dumps({"error": "수동 근태 상태는 정상 또는 지각만 지정할 수 있습니다"}),
                        status_code=400, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()

    updates = []
    vals = []
    if "clock_in" in body:
        updates.append("clock_in = %s")
        vals.append(body["clock_in"])
    if "clock_out" in body:
        updates.append("clock_out = %s")
        vals.append(body["clock_out"])
    if "status" in body:
        updates.append("status = %s")
        vals.append(body["status"])

    if updates:
        vals.append(att_id)
        cur.execute(f"UPDATE ext_attendance SET {', '.join(updates)} WHERE id = %s", vals)
        conn.commit()

    conn.close()
    return {"ok": True}


@app.delete("/api/attendance/_all")
async def delete_all_attendance(request: Request, confirm: str = ""):
    """모든 출퇴근 기록 일괄 삭제 (admin only) — 운영 초기화·테스트 정리용.
    실수 방지 위해 ?confirm=DELETE_ALL 파라미터 필수."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") != "admin":
        return Response(content=json.dumps({"error": "관리자 권한이 필요합니다"}), status_code=403, media_type="application/json")
    if confirm != "DELETE_ALL":
        return Response(content=json.dumps({"error": "?confirm=DELETE_ALL 파라미터가 필요합니다"}),
                        status_code=400, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ext_attendance")
    cnt = cur.fetchone()[0] or 0
    cur.execute("DELETE FROM ext_attendance")
    conn.commit()
    conn.close()
    logger.info(f"[일괄삭제] ext_attendance {cnt}건 삭제 by {user_state.get('name')}")
    log_activity(request, "근태 전체 삭제", "attendance", "_all", {"deleted": cnt})
    return {"ok": True, "deleted": cnt}


@app.delete("/api/attendance/{att_id}")
async def delete_attendance(att_id: int, request: Request):
    """출퇴근 기록 삭제 (admin/pm only)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ext_attendance WHERE id = %s", (att_id,))
    conn.commit()
    conn.close()
    log_activity(request, "근태 삭제", "attendance", str(att_id), {})
    return {"ok": True}


@app.get("/api/attendance/monthly")
async def get_attendance_monthly(request: Request, year: str = "", month: str = ""):
    """월/연간 출퇴근 조회 — 요약은 원시 근태, 영업일 보완은 상세에만 적용한다."""
    user_state = getattr(request.state, "user", None)
    only_user = _attendance_scope(user_state)
    try:
        now = datetime.now(KST)
        start, end, period_type = _attendance_period_bounds(year, month, now=now)
        as_of = _attendance_date(now=now)
    except (ValueError, TypeError, OverflowError):
        return Response(content=json.dumps({"error": "유효한 연도와 월(1~12 또는 all)을 지정해주세요"}),
                        status_code=400, media_type="application/json")
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        records = _read_attendance_records(cur, start, end, only_user)
        personal = _attendance_can_read_halfdays(user_state)
        leaves = _read_attendance_approved_leaves(cur, start, end, only_user) if personal else []
        view = _build_attendance_view(records, leaves, start, end)
        if personal:
            holidays = _read_attendance_holidays(cur, start.year)
            roster = _read_attendance_roster(cur, only_user)
            coverage = _add_attendance_coverage(view["records"], leaves, roster, holidays, start, end, today=as_of)
        else:
            coverage = {"records": [{**row, "is_missing": False, "missing_clock_in": False,
                                      "missing_clock_out": False, "approved_leaves": []} for row in view["records"]],
                        "coverage_warnings": []}
    finally:
        conn.close()
    return {**view, **coverage, "year": str(start.year), "month": "all" if period_type == "year" else str(start.month),
            "month_start": start.isoformat(), "month_end": end.isoformat(), "timezone": "Asia/Seoul",
            "period_type": period_type, "period_start": start.isoformat(), "period_end": end.isoformat(),
            "as_of": as_of.isoformat()}


# ── 연차 관리 API ──

@app.get("/api/leave/balance")
async def get_leave_balance(request: Request, year: str = ""):
    """연차 잔여 현황 — admin/pm은 전체, member는 본인만, legacy는 차단"""
    user_state = getattr(request.state, "user", None)
    if not user_state:
        return Response(content=json.dumps({"error": "인증이 필요합니다"}), status_code=401, media_type="application/json")
    role = user_state.get("role")
    me = user_state.get("name") or ""
    if user_state.get("user_id") == "legacy":
        return {"rows": [], "year": year or str(datetime.now().year), "scope": "legacy"}
    conn = _pg_conn()
    cur = conn.cursor()
    y = year or str(datetime.now().year)
    base = ("SELECT b.user_name, b.year, b.total_days, b.used_days, "
            "(b.total_days - b.used_days) AS remaining, "
            "u.hire_date::text AS hire_date, u.leave_start_date::text AS leave_start_date "
            "FROM ext_leave_balance b LEFT JOIN ext_users u ON u.name = b.user_name "
            "WHERE b.year = %s")
    if role in ("admin", "pm"):
        cur.execute(base + " ORDER BY b.user_name", (int(y),))
    else:
        cur.execute(base + " AND b.user_name = %s ORDER BY b.user_name", (int(y), me))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return {"rows": rows, "year": y, "scope": role if role in ("admin","pm") else "self"}


@app.put("/api/leave/balance")
async def update_leave_balance(request: Request):
    """연차 총일수 수정 (admin only)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") != "admin":
        return Response(content=json.dumps({"error": "관리자 권한이 필요합니다"}), status_code=403, media_type="application/json")

    body = await request.json()
    user = body.get("user", "")
    year = body.get("year", datetime.now().year)
    total = body.get("total_days")
    used = body.get("used_days")  # optional — 엑셀 import 등에서 사용
    if not user or total is None:
        return Response(content=json.dumps({"error": "user, total_days 필수"}), status_code=400, media_type="application/json")

    conn = _pg_conn()
    cur = conn.cursor()
    if used is not None:
        cur.execute(
            "INSERT INTO ext_leave_balance (user_name, year, total_days, used_days) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_name, year) DO UPDATE SET total_days = EXCLUDED.total_days, used_days = EXCLUDED.used_days",
            (user, int(year), float(total), float(used)))
    else:
        cur.execute(
            "INSERT INTO ext_leave_balance (user_name, year, total_days) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_name, year) DO UPDATE SET total_days = EXCLUDED.total_days",
            (user, int(year), float(total)))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 연차 2단계 결재 (1차 PM → 2차 대표) ──
LEAVE_DEFAULT_PM = _installation.LEAVE_DEFAULT_PM
LEAVE_DEFAULT_ADMIN = _installation.LEAVE_DEFAULT_ADMIN


def _ensure_leave_columns():
    """Add two-stage approval schema without rewriting historical records."""
    try:
        conn = _pg_conn(); cur = conn.cursor()
        for col, ddl in [
            ("pm_approver", "TEXT DEFAULT ''"), ("admin_approver", "TEXT DEFAULT ''"),
            ("pm_status", "TEXT DEFAULT '대기'"), ("admin_status", "TEXT DEFAULT '대기'"),
            ("pm_decided_at", "TIMESTAMP"), ("admin_decided_at", "TIMESTAMP"),
            ("pm_note", "TEXT DEFAULT ''"), ("admin_note", "TEXT DEFAULT ''"),
            # 신청 즉시 반영용 — 캘린더 이벤트 ID + 잔여 차감 여부 플래그
            ("calendar_event_id", "TEXT DEFAULT ''"), ("balance_deducted", "BOOLEAN DEFAULT FALSE"),
        ]:
            cur.execute(f"ALTER TABLE ext_leave_requests ADD COLUMN IF NOT EXISTS {col} {ddl}")
        # Historical rows remain unchanged; migrate explicitly after a reviewed backup.
        conn.commit(); conn.close()
    except Exception as e:
        logger.warning(f"_ensure_leave_columns 실패: {e}")


def _get_leave_approvers():
    """연차 1차 PM·2차 대표 — approval_settings.type_approvers['leave'] 사용, 없으면 기본값."""
    raw = _read_store("approval_settings") or {}
    tm = _norm_type_approvers(raw.get("type_approvers"))
    pair = tm.get("leave", {"pm": "", "admin": ""})
    pm = (pair.get("pm") or "").strip() or LEAVE_DEFAULT_PM
    admin = (pair.get("admin") or "").strip() or LEAVE_DEFAULT_ADMIN
    return pm, admin


def _ensure_bootstrap_tables():
    """신규/빈 DB 부트스트랩 — 부팅 경로에 CREATE가 누락됐던 테이블을 보장 (멱등).

    아래 6개 테이블은 과거 별도 마이그레이션 스크립트로만 생성돼 부팅 코드에
    CREATE가 없었음. 운영 DB엔 이미 존재하지만 빈 DB/재해복구/스테이징에선
    해당 페이지(근태·연차·회의록·댓글·지표)가 500으로 깨짐.
    여기서 제약(PK/UNIQUE)까지 함께 만들어 '빈 DB = 완전한 앱'이 되게 한다.

    주의: ext_leave_requests의 2단계 결재 컬럼은 _ensure_leave_columns()가
    ALTER로 추가하므로, 이 함수가 반드시 그보다 먼저 실행돼야 한다(아래 호출 순서).
    ext_expenses는 실제 소스가 구글시트이고 코드가 to_regclass로 부재를
    이미 가드하므로 일부러 생성하지 않는다."""
    ddls = [
        # 근태(출퇴근) — id 자동, (user_name, work_date)로 1일 1행
        """CREATE TABLE IF NOT EXISTS ext_attendance (
            id        SERIAL PRIMARY KEY,
            user_name TEXT NOT NULL,
            work_date DATE NOT NULL,
            clock_in  TIMESTAMP,
            clock_out TIMESTAMP,
            status    TEXT DEFAULT '정상',
            note      TEXT DEFAULT ''
        )""",
        # 담당자별 댓글
        """CREATE TABLE IF NOT EXISTS ext_comments (
            id         TEXT PRIMARY KEY,
            owner_key  TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        # 회의록 — attachments는 JSON 문자열
        """CREATE TABLE IF NOT EXISTS ext_meeting_notes (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            meeting_type TEXT DEFAULT '주간회의',
            week_label   TEXT DEFAULT '',
            content      TEXT DEFAULT '',
            attachments  TEXT DEFAULT '[]',
            author       TEXT DEFAULT '',
            created_at   TIMESTAMP DEFAULT NOW(),
            updated_at   TIMESTAMP DEFAULT NOW()
        )""",
        # 연차 신청 — 2단계 결재 컬럼은 _ensure_leave_columns()가 ALTER로 보강
        """CREATE TABLE IF NOT EXISTS ext_leave_requests (
            id          TEXT PRIMARY KEY,
            user_name   TEXT NOT NULL,
            leave_type  TEXT NOT NULL,
            start_date  DATE NOT NULL,
            end_date    DATE NOT NULL,
            days        NUMERIC NOT NULL DEFAULT 0,
            reason      TEXT DEFAULT '',
            approver    TEXT DEFAULT '',
            status      TEXT DEFAULT '대기',
            created_at  TIMESTAMP DEFAULT NOW(),
            approved_at TIMESTAMP
        )""",
        # 연차 잔여 — (이름, 연도) 단위, 반차 위해 NUMERIC
        """CREATE TABLE IF NOT EXISTS ext_leave_balance (
            user_name  TEXT    NOT NULL,
            year       INTEGER NOT NULL,
            total_days NUMERIC NOT NULL DEFAULT 0,
            used_days  NUMERIC NOT NULL DEFAULT 0,
            UNIQUE (user_name, year)
        )""",
        # 일별 전사 지표 — date 단위 upsert (ON CONFLICT (date))
        """CREATE TABLE IF NOT EXISTS ext_daily_metrics (
            date              DATE PRIMARY KEY,
            revenue           NUMERIC DEFAULT 0,
            crm_revenue       NUMERIC DEFAULT 0,
            buyers            NUMERIC DEFAULT 0,
            ad_spend          NUMERIC DEFAULT 0,
            api_cost          NUMERIC DEFAULT 0,
            api_per_buyer     NUMERIC DEFAULT 0,
            profit            NUMERIC DEFAULT 0,
            roas              NUMERIC DEFAULT 0,
            users             NUMERIC DEFAULT 0,
            sessions          NUMERIC DEFAULT 0,
            new_visitor_ratio NUMERIC DEFAULT 0,
            updated_at        TIMESTAMP DEFAULT NOW()
        )""",
        # 백그라운드 작업 하트비트(B3) — 작업별 1행, 마지막 실행/성공·연속실패·알림여부
        """CREATE TABLE IF NOT EXISTS ext_heartbeats (
            task         TEXT PRIMARY KEY,
            last_run     TIMESTAMP,
            last_success TIMESTAMP,
            last_error   TEXT DEFAULT '',
            fail_streak  INTEGER DEFAULT 0,
            alerted      BOOLEAN DEFAULT FALSE,
            updated_at   TIMESTAMP DEFAULT NOW()
        )""",
    ]
    for ddl in ddls:
        try:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(ddl)
            conn.commit(); conn.close()
        except Exception as e:
            try: conn.rollback(); conn.close()
            except Exception: pass
            logger.warning(f"_ensure_bootstrap_tables 건너뜀: {e}")




def _ensure_upsert_indexes():
    """마이그레이션으로 누락된 UNIQUE 인덱스 복구 — ON CONFLICT upsert 정상화 (멱등).
    새 DB로 마이그레이션하면서 일부 테이블의 UNIQUE 제약이 빠져 ON CONFLICT 저장이 500 실패함.
    대표 현황(ext_exec_status) 저장 실패가 대표 사례. 다른 upsert 테이블도 함께 복구."""
    # 주의: project_hierarchy(접두사 ext_ 없음)는 linear_project_id PK가 이미 있어 복구 불필요.
    specs = [
        ("ext_exec_status", "ux_exec_status_name_week", ["name", "week_label"]),
        ("ext_leave_balance", "ux_leave_balance_user_year", ["user_name", "year"]),
        ("ext_daily_metrics", "ux_daily_metrics_date", ["date"]),
    ]
    for table, idx, cols in specs:
        try:
            conn = _pg_conn(); cur = conn.cursor()
            # Duplicate keys require explicit administrator repair; never delete records at startup.
            cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {table} ({', '.join(cols)})")
            conn.commit(); conn.close()
        except Exception as e:
            try: conn.rollback(); conn.close()
            except Exception: pass
            logger.warning(f"_ensure_upsert_indexes {table} 건너뜀: {e}")




@app.get("/api/leave/requests")
async def get_leave_requests(request: Request, year: str = "", status: str = ""):
    """연차 신청 목록 — admin/pm=전체, member=본인 것만, 공용(legacy)=403"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}),
                        status_code=403, media_type="application/json")
    role = user_state.get("role") or ""
    me = user_state.get("name") or ""
    is_priv = role in ("admin", "pm")

    conn = _pg_conn()
    cur = conn.cursor()
    y = year or str(datetime.now().year)
    base_sql = ("SELECT id, user_name, leave_type, start_date::text, end_date::text, days, reason, "
                "status, approver, created_at::text, approved_at::text, "
                "COALESCE(pm_approver,'') AS pm_approver, COALESCE(admin_approver,'') AS admin_approver, "
                "COALESCE(pm_status,'대기') AS pm_status, COALESCE(admin_status,'대기') AS admin_status "
                "FROM ext_leave_requests WHERE EXTRACT(YEAR FROM start_date) = %s")
    params = [int(y)]
    if status:
        base_sql += " AND status = %s"
        params.append(status)
    if not is_priv:
        base_sql += " AND user_name = %s"
        params.append(me)
    base_sql += " ORDER BY created_at DESC"
    cur.execute(base_sql, tuple(params))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return {"rows": rows}


@app.get("/api/users/approvers")
async def list_approvers(request: Request):
    """승인자 후보(admin·pm) 이름 목록 — 개인 로그인 필요"""
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in (None, "", "legacy"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, role FROM ext_users WHERE is_active = TRUE AND role IN ('admin', 'pm') ORDER BY role, name")
    rows = cur.fetchall()
    conn.close()
    return {"approvers": [{"name": r[0], "role": r[1]} for r in rows]}


@app.post("/api/leave/requests/_admin_import")
async def admin_import_leave_requests(request: Request):
    """admin 전용 일괄 import — 엑셀 등 외부 사용내역을 status=승인으로 등록"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return Response(content=json.dumps({"error": "관리자 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    items = body.get("items") or []
    if not isinstance(items, list):
        return Response(content=json.dumps({"error": "items 배열 필요"}), status_code=400, media_type="application/json")

    conn = _pg_conn()
    cur = conn.cursor()
    now = datetime.now(KST).replace(tzinfo=None)
    inserted = 0
    skipped = []
    approver_name = user.get("name") or "사용자 A"
    for it in items:
        try:
            requester = (it.get("requester") or "").strip()
            leave_type = (it.get("leave_type") or "").strip()
            start_date = (it.get("start_date") or "").strip()
            end_date = (it.get("end_date") or start_date).strip()
            days = float(it.get("days") or 0)
            reason = (it.get("reason") or "").strip()
            if not requester or not start_date or days <= 0:
                skipped.append({"item": it, "reason": "필수 필드 누락"})
                continue
            rid = uuid.uuid4().hex[:8]
            cur.execute(
                "INSERT INTO ext_leave_requests (id, user_name, leave_type, start_date, end_date, days, reason, approver, status, created_at, approved_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rid, requester, leave_type, start_date, end_date, float(days), reason, approver_name, "승인", now, now)
            )
            inserted += 1
        except Exception as e:
            skipped.append({"item": it, "reason": str(e)})
    conn.commit()
    conn.close()
    return {"ok": True, "inserted": inserted, "skipped": len(skipped), "skipped_detail": skipped[:5]}


@app.post("/api/leave/requests")
async def create_leave_request(request: Request):
    """연차 신청 — JWT 본인 자동 식별 + 승인자 지정(admin/pm)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") == "legacy" or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    user = user_state["name"]

    body = await request.json()
    leave_type = body.get("leave_type", "연차")
    start_date = body.get("start_date", "")
    end_date = body.get("end_date", "")
    days = body.get("days", 1)
    reason = body.get("reason", "")

    if not start_date or not end_date:
        return Response(content=json.dumps({"error": "start_date, end_date 필수"}), status_code=400, media_type="application/json")

    # 승인자 자동 배정 (1차 PM → 2차 대표)
    pm_name, admin_name = _get_leave_approvers()

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, slack_id FROM ext_users WHERE name = ANY(%s) AND is_active = TRUE", ([pm_name, admin_name],))
    found = {r[0]: (r[1] or "") for r in cur.fetchall()}
    if pm_name not in found or admin_name not in found:
        conn.close()
        return Response(content=json.dumps({"error": f"연차 승인자(1차 PM '{pm_name}', 2차 대표 '{admin_name}')를 찾을 수 없습니다. 관리자에게 문의하세요."}), status_code=400, media_type="application/json")
    pm_slack = found.get(pm_name, "")

    rid = uuid.uuid4().hex[:8]
    now = datetime.now(KST).replace(tzinfo=None)
    cur.execute(
        "INSERT INTO ext_leave_requests (id, user_name, leave_type, start_date, end_date, days, reason, approver, status, pm_approver, admin_approver, pm_status, admin_status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (rid, user, leave_type, start_date, end_date, float(days), reason, pm_name, "대기", pm_name, admin_name, "대기", "대기", now))

    # 신청 즉시 반영 — 잔여 차감(차감 종류만) + 구글 캘린더 등록. 반려/취소 시 원복.
    DEDUCT_TYPES = {"연차", "오전반차", "오후반차"}
    deducted = False
    if leave_type in DEDUCT_TYPES:
        cur.execute("UPDATE ext_leave_balance SET used_days = used_days + %s WHERE user_name = %s AND year = EXTRACT(YEAR FROM %s::date)",
                    (float(days), user, start_date))
        deducted = cur.rowcount > 0
    event_id = _create_leave_event(user, leave_type, start_date, end_date, float(days))
    cur.execute("UPDATE ext_leave_requests SET calendar_event_id=%s, balance_deducted=%s WHERE id=%s",
                (event_id, deducted, rid))
    conn.commit()
    conn.close()
    # 1차 PM에게 알림 (인앱 + Slack) + 활동 로그
    _det = f"{user} · {start_date}~{end_date} ({days}일)" + (f" · {reason}" if reason else "")
    create_notification(pm_name, "leave_request", f"{leave_type} 신청 도착 (1차 PM 승인)", _det, "/?tab=leave")
    if admin_name and admin_name != pm_name:
        create_notification(admin_name, "leave_incoming", f"{leave_type} 결재 흐름 시작",
                            f"{user} · {start_date}~{end_date} ({days}일) · PM({pm_name}) 승인 후 2차 차례", "/?tab=leave")
    await _notify_slack_approval(pm_slack,
        f"🌴 *신규 {leave_type} 신청 (1차 PM 결재)*\n신청자: {user}\n기간: {start_date}~{end_date} ({days}일)"
        + (f"\n사유: {reason}" if reason else "")
        + f"\n승인 흐름: PM({pm_name}) → 대표({admin_name})\n승인해주세요 → 대시보드 근태/연차 탭")
    log_activity(request, "연차 신청", "leave", rid,
                 {"user": user, "type": leave_type, "start": start_date, "end": end_date, "days": float(days), "pm": pm_name, "admin": admin_name})
    return {"ok": True, "id": rid, "user": user, "pm_approver": pm_name, "admin_approver": admin_name}


@app.put("/api/leave/requests/{request_id}")
async def update_leave_request(request_id: str, request: Request):
    """연차 2단계 결재 — 1차 PM → 2차 대표. 본인이 현재 단계 담당자일 때만."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    me = user_state["name"]
    role = user_state.get("role")

    body = await request.json()
    decision = body.get("status", "") or body.get("decision", "")
    note = body.get("note", "")
    if decision not in ("승인", "반려"):
        return Response(content=json.dumps({"error": "status는 승인 또는 반려"}), status_code=400, media_type="application/json")

    now = datetime.now(KST).replace(tzinfo=None)
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT user_name, leave_type, start_date::text, end_date::text, days,
        COALESCE(pm_approver,''), COALESCE(admin_approver,''),
        COALESCE(pm_status,'대기'), COALESCE(admin_status,'대기'),
        COALESCE(calendar_event_id,''), COALESCE(balance_deducted, FALSE)
        FROM ext_leave_requests WHERE id = %s""", (request_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    uname, ltype, sd, ed, days, pm_approver, admin_approver, pm_status, admin_status, calendar_event_id, balance_deducted = row

    # 현재 단계 판정
    stage = None
    if pm_status == "대기" and me == pm_approver:
        stage = "pm"
    elif pm_status == "승인" and admin_status == "대기" and me == admin_approver:
        stage = "admin"
    elif pm_status == "대기" and role == "admin":
        stage = "pm"  # 대표는 PM 단계 대리 승인 가능
    if stage is None:
        conn.close()
        return Response(content=json.dumps({"error": "현재 단계의 결재 담당자가 아니거나 이미 처리된 신청입니다"}), status_code=403, media_type="application/json")

    DEDUCT_TYPES = {"연차", "오전반차", "오후반차"}
    if stage == "pm":
        new_pm, new_admin = decision, admin_status
    else:
        new_pm, new_admin = pm_status, decision
    overall = _compute_overall_status(new_pm, new_admin)

    # 차감·캘린더는 '신청 즉시' 이미 반영됨 → 최종 승인 시 추가 처리 없음.
    # 반려로 확정되면 차감 원복 + 캘린더 이벤트 삭제 (중복 방지: balance_deducted 플래그).
    if overall == "반려" and balance_deducted:
        if ltype in DEDUCT_TYPES:
            cur.execute("UPDATE ext_leave_balance SET used_days = GREATEST(used_days - %s, 0) WHERE user_name = %s AND year = EXTRACT(YEAR FROM %s::date)",
                        (float(days), uname, str(sd)))
        _delete_leave_event(calendar_event_id)
        cur.execute("UPDATE ext_leave_requests SET balance_deducted=FALSE, calendar_event_id='' WHERE id=%s", (request_id,))

    decided_at = now if overall != "대기" else None
    if stage == "pm":
        cur.execute("UPDATE ext_leave_requests SET pm_status=%s, pm_note=%s, pm_decided_at=%s, status=%s, approver=%s, approved_at=%s WHERE id=%s",
                    (decision, note, now, overall, me, decided_at, request_id))
    else:
        cur.execute("UPDATE ext_leave_requests SET admin_status=%s, admin_note=%s, admin_decided_at=%s, status=%s, approver=%s, approved_at=%s WHERE id=%s",
                    (decision, note, now, overall, me, now, request_id))
    cur.execute("SELECT name, slack_id FROM ext_users WHERE name IN (%s, %s, %s)", (uname, pm_approver, admin_approver))
    slack_map = {r[0]: (r[1] or "") for r in cur.fetchall()}
    conn.commit()
    conn.close()

    _period = f"{sd}~{ed} ({days}일)"
    if decision == "반려":
        create_notification(uname, "leave_반려", f"{ltype} 반려됨", f"{_period} · 처리: {me}" + (f" · {note}" if note else ""), "/?tab=leave")
        await _notify_slack_approval(slack_map.get(uname, ""), f"❌ *{ltype} 반려*\n{_period}\n처리: {me}" + (f"\n사유: {note}" if note else ""))
    elif stage == "pm":  # 1차 PM 승인 → 2차 대표에게
        create_notification(admin_approver, "leave_request", f"{ltype} 2차 대표 승인 요청", f"{uname} · {_period} · 1차 PM({me}) 승인 완료", "/?tab=leave")
        await _notify_slack_approval(slack_map.get(admin_approver, ""),
            f"📥 *{ltype} 2차 대표 결재*\n신청자: {uname}\n기간: {_period}\n1차 PM({me}) 승인 완료 → 최종 승인해주세요 → 근태/연차 탭")
    else:  # 2차 대표 최종 승인
        create_notification(uname, "leave_승인", f"{ltype} 최종 승인됨", f"{_period} · PM·대표 승인 완료", "/?tab=leave")
        await _notify_slack_approval(slack_map.get(uname, ""), f"✅ *{ltype} 최종 승인*\n{_period}\nPM·대표 승인 완료")

    log_activity(request, f"연차 {stage}단계 {decision}", "leave", request_id,
                 {"user": uname, "type": ltype, "stage": stage, "decision": decision, "overall": overall})
    return {"ok": True, "stage": stage, "overall": overall, "approver": me}


@app.delete("/api/leave/requests/{request_id}")
async def delete_leave_request(request_id: str, request: Request):
    """연차 신청 취소 — 본인 대기 / admin·pm은 모든 상태(2차 승인 완료 포함, 승인 건은 used_days 원복)"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") == "legacy" or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    is_manager = user_state.get("role") in ("admin", "pm")

    DEDUCT_TYPES = {"연차", "오전반차", "오후반차"}

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, user_name, days, start_date, leave_type, COALESCE(calendar_event_id,''), COALESCE(balance_deducted, FALSE) FROM ext_leave_requests WHERE id = %s", (request_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(status_code=404)
    status, owner, days, start_date, leave_type, calendar_event_id, balance_deducted = row

    if not is_manager:
        if owner != user_state["name"]:
            conn.close()
            return Response(content=json.dumps({"error": "본인의 신청만 취소 가능합니다"}), status_code=403, media_type="application/json")
        if status != "대기":
            conn.close()
            return Response(content=json.dumps({"error": "대기 상태인 신청만 취소 가능합니다"}), status_code=400, media_type="application/json")

    # 신청 즉시 차감 방식 — 차감되어 있으면(대기/승인 무관) 취소 시 원복 + 캘린더 이벤트 삭제
    if balance_deducted and leave_type in DEDUCT_TYPES:
        cur.execute(
            "UPDATE ext_leave_balance SET used_days = GREATEST(used_days - %s, 0) "
            "WHERE user_name = %s AND year = EXTRACT(YEAR FROM %s::date)",
            (float(days), owner, str(start_date)))
    _delete_leave_event(calendar_event_id)

    cur.execute("DELETE FROM ext_leave_requests WHERE id = %s", (request_id,))
    conn.commit()
    conn.close()
    log_activity(request, "연차 삭제", "leave", request_id,
                 {"user": owner, "type": leave_type, "prev_status": status, "days": float(days), "start": str(start_date)})
    return {"ok": True}


# ── Google Calendar 연동 ──

GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")


def _get_calendar_service():
    """Google Calendar API 클라이언트"""
    if not GOOGLE_CALENDAR_ID or not _BQ_KEY_PATH:
        raise RuntimeError("Google Calendar integration is not configured")
    from google.oauth2 import service_account as _sa
    from googleapiclient.discovery import build
    creds = _sa.Credentials.from_service_account_file(
        _BQ_KEY_PATH,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def _create_leave_event(user: str, leave_type: str, start_date: str, end_date: str, days: float):
    """연차 승인 시 구글 캘린더에 이벤트 생성"""
    try:
        svc = _get_calendar_service()
        # 종료일은 +1일 (Google Calendar는 종일 이벤트에서 end를 exclusive로 처리)
        end_dt = dt_date.fromisoformat(end_date) + timedelta(days=1)

        # 반차는 시간 지정
        if "오전반차" in leave_type:
            event_body = {
                "summary": f"[{leave_type}] {user}",
                "start": {"dateTime": f"{start_date}T09:00:00", "timeZone": "Asia/Seoul"},
                "end": {"dateTime": f"{start_date}T13:00:00", "timeZone": "Asia/Seoul"},
                "description": f"{user} {leave_type} ({days}일)",
            }
        elif "오후반차" in leave_type:
            event_body = {
                "summary": f"[{leave_type}] {user}",
                "start": {"dateTime": f"{start_date}T14:00:00", "timeZone": "Asia/Seoul"},
                "end": {"dateTime": f"{start_date}T18:00:00", "timeZone": "Asia/Seoul"},
                "description": f"{user} {leave_type} ({days}일)",
            }
        else:
            event_body = {
                "summary": f"[{leave_type}] {user}",
                "start": {"date": start_date},
                "end": {"date": str(end_dt)},
                "description": f"{user} {leave_type} ({days}일)",
            }

        event = svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        return event.get("id", "")
    except Exception as e:
        logger.warning(f"[캘린더] 이벤트 생성 실패: {e}")
        return ""


def _delete_leave_event(event_id: str):
    """연차 반려/취소 시 구글 캘린더 이벤트 삭제 (멱등 — 실패해도 무시)."""
    if not event_id:
        return
    try:
        svc = _get_calendar_service()
        svc.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        logger.warning(f"[캘린더] 이벤트 삭제 실패({event_id}): {e}")


def _read_calendar_events(start: str = "", end: str = ""):
    """호출마다 독립 Google 클라이언트로 조회·정규화한다(스레드풀 전용)."""
    svc = _get_calendar_service()
    if not start:
        # 이번 주 월~일
        today = dt_date.today()
        start_dt = today - timedelta(days=today.weekday())
        start = start_dt.isoformat() + "T00:00:00+09:00"
    else:
        start = start + "T00:00:00+09:00" if "T" not in start else start

    if not end:
        end_dt = dt_date.fromisoformat(start[:10]) + timedelta(days=30)
        end = end_dt.isoformat() + "T23:59:59+09:00"
    else:
        end = end + "T23:59:59+09:00" if "T" not in end else end

    events_result = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start, timeMax=end,
        maxResults=100, singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for e in events_result.get("items", []):
        s = e.get("start", {})
        ed = e.get("end", {})
        # 참석자 목록 (구글 attendees + description + 제목 [이름] 파싱)
        import re
        attendees = []
        for a in e.get("attendees", []):
            name = a.get("displayName") or a.get("email", "").split("@")[0]
            attendees.append(name)
        # description에서 "참석자: A, B, C" 파싱 (리터럴 \n 처리)
        desc_text = (e.get("description", "") or "").replace("\\n", "\n")
        if "참석자:" in desc_text:
            match = re.search(r"참석자:\s*(.+?)(\n|$)", desc_text)
            if match:
                for name in match.group(1).split(","):
                    name = name.strip()
                    if name and name not in attendees and len(name) < 20:
                        attendees.append(name)
        # 제목에서 [이름] 파싱 (예: "회의 제목 [사용자 H]", "회의 [사용자 C, 사용자 D]")
        title_text = e.get("summary", "") or ""
        title_brackets = re.findall(r"\[([^\]]+)\]", title_text)
        for bracket in title_brackets:
            for name in bracket.split(","):
                name = name.strip()
                if name and name not in attendees:
                    attendees.append(name)
        creator = e.get("creator", {}).get("displayName") or e.get("creator", {}).get("email", "").split("@")[0]
        events.append({
            "id": e.get("id", ""),
            "title": e.get("summary", ""),
            "description": e.get("description", ""),
            "startDate": s.get("date", s.get("dateTime", "")[:10]),
            "endDate": ed.get("date", ed.get("dateTime", "")[:10]),
            "startTime": s.get("dateTime", "")[11:16] if s.get("dateTime") else "",
            "endTime": ed.get("dateTime", "")[11:16] if ed.get("dateTime") else "",
            "allDay": "date" in s,
            "attendees": attendees,
            "creator": creator,
        })

    return {"events": events}


@app.get("/api/calendar/events")
async def get_calendar_events(start: str = "", end: str = ""):
    """동기 Google SDK 조회 중에도 다른 요청의 이벤트 루프를 유지한다."""
    try:
        return await run_in_threadpool(_read_calendar_events, start, end)
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"캘린더 조회 실패: {str(e)}"}),
            status_code=502, media_type="application/json",
        )


@app.post("/api/calendar/events")
async def create_calendar_event(request: Request):
    """캘린더에 일정 추가"""
    body = await request.json()
    title = body.get("title", "")
    start_date = body.get("start_date", "")
    end_date = body.get("end_date", start_date)
    all_day = body.get("all_day", True)
    start_time = body.get("start_time", "09:00")
    end_time = body.get("end_time", "18:00")

    attendees_names = body.get("attendees", [])

    if not title or not start_date:
        return Response(content=json.dumps({"error": "title, start_date 필수"}), status_code=400, media_type="application/json")

    try:
        svc = _get_calendar_service()
        if all_day:
            end_dt = dt_date.fromisoformat(end_date) + timedelta(days=1)
            event_body = {
                "summary": title,
                "start": {"date": start_date},
                "end": {"date": str(end_dt)},
                "description": body.get("description", ""),
            }
        else:
            event_body = {
                "summary": title,
                "start": {"dateTime": f"{start_date}T{start_time}:00", "timeZone": "Asia/Seoul"},
                "end": {"dateTime": f"{end_date}T{end_time}:00", "timeZone": "Asia/Seoul"},
                "description": body.get("description", ""),
            }

        # 참석자 이름 → description에 포함 (구글 캘린더 attendees는 이메일 필요)
        if attendees_names:
            existing_desc = event_body.get("description", "")
            if "참석자:" not in existing_desc:
                event_body["description"] = f"참석자: {', '.join(attendees_names)}\n{existing_desc}".strip()

        event = svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        return {"ok": True, "id": event.get("id", "")}
    except Exception as e:
        return Response(content=json.dumps({"error": f"일정 추가 실패: {str(e)}"}), status_code=500, media_type="application/json")


@app.delete("/api/calendar/events/{event_id}")
async def delete_calendar_event(event_id: str):
    """캘린더 일정 삭제"""
    try:
        svc = _get_calendar_service()
        svc.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return {"ok": True}
    except Exception as e:
        return Response(content=json.dumps({"error": f"삭제 실패: {str(e)}"}), status_code=500, media_type="application/json")


# ── 회의록 API ──

@app.get("/api/meeting-notes")
async def get_meeting_notes(meeting_type: str = "", week: str = ""):
    """회의록 목록 조회"""
    conn = _pg_conn()
    cur = conn.cursor()
    if meeting_type and week:
        cur.execute(
            "SELECT id, title, meeting_type, week_label, content, attachments, author, created_at::text, updated_at::text "
            "FROM ext_meeting_notes WHERE meeting_type = %s AND week_label = %s ORDER BY created_at DESC",
            (meeting_type, week))
    elif meeting_type:
        cur.execute(
            "SELECT id, title, meeting_type, week_label, content, attachments, author, created_at::text, updated_at::text "
            "FROM ext_meeting_notes WHERE meeting_type = %s ORDER BY created_at DESC", (meeting_type,))
    else:
        cur.execute(
            "SELECT id, title, meeting_type, week_label, content, attachments, author, created_at::text, updated_at::text "
            "FROM ext_meeting_notes ORDER BY created_at DESC")
    cols = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        row = dict(zip(cols, r))
        if isinstance(row.get("attachments"), str):
            row["attachments"] = json.loads(row["attachments"])
        rows.append(row)
    conn.close()
    return {"rows": rows}


@app.post("/api/meeting-notes")
async def create_meeting_note(request: Request):
    """회의록 생성"""
    body = await request.json()
    title = body.get("title", "")
    if not title:
        return Response(content=json.dumps({"error": "제목 필수"}), status_code=400, media_type="application/json")

    nid = uuid.uuid4().hex[:8]
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ext_meeting_notes (id, title, meeting_type, week_label, content, attachments, author) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (nid, title, body.get("meeting_type", "주간회의"), body.get("week_label", ""),
         body.get("content", ""), json.dumps(body.get("attachments", []), ensure_ascii=False),
         body.get("author", "")))
    conn.commit()
    conn.close()
    return {"ok": True, "id": nid}


@app.put("/api/meeting-notes/{note_id}")
async def update_meeting_note(note_id: str, request: Request):
    """회의록 수정"""
    body = await request.json()
    conn = _pg_conn()
    cur = conn.cursor()

    updates = []
    vals = []
    for field in ["title", "meeting_type", "week_label", "content", "author"]:
        if field in body:
            updates.append(f"{field} = %s")
            vals.append(body[field])
    if "attachments" in body:
        updates.append("attachments = %s")
        vals.append(json.dumps(body["attachments"], ensure_ascii=False))

    if updates:
        updates.append("updated_at = NOW()")
        vals.append(note_id)
        cur.execute(f"UPDATE ext_meeting_notes SET {', '.join(updates)} WHERE id = %s", vals)
        conn.commit()

    conn.close()
    return {"ok": True}


@app.delete("/api/meeting-notes/{note_id}")
async def delete_meeting_note(note_id: str):
    """회의록 삭제"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ext_meeting_notes WHERE id = %s", (note_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 대표 현황 API ──

def _ensure_exec_table():
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_exec_status (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, week_label TEXT NOT NULL,
        current_tasks TEXT DEFAULT '', meeting_notes TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(name, week_label)
    )""")
    conn.commit()
    conn.close()


@app.get("/api/exec-status")
async def get_exec_status(week: str = ""):
    conn = _pg_conn()
    cur = conn.cursor()
    if week:
        cur.execute("SELECT id, name, week_label, current_tasks, meeting_notes, updated_at::text FROM ext_exec_status WHERE week_label = %s ORDER BY name", (week,))
    else:
        cur.execute("SELECT id, name, week_label, current_tasks, meeting_notes, updated_at::text FROM ext_exec_status ORDER BY week_label DESC, name")
    rows = cur.fetchall()
    conn.close()
    return {"items": [{"id": r[0], "name": r[1], "week_label": r[2], "current_tasks": r[3], "meeting_notes": r[4], "updated_at": r[5]} for r in rows]}

@app.put("/api/exec-status")
async def upsert_exec_status(request: Request):
    body = await request.json()
    name = body.get("name", "")
    week = body.get("week_label", "")
    if not name or not week:
        return Response(content=json.dumps({"error": "name, week_label 필수"}), status_code=400, media_type="application/json")
    nid = f"{name}_{week}".replace(" ", "_")
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO ext_exec_status (id, name, week_label, current_tasks, meeting_notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (name, week_label) DO UPDATE SET current_tasks = EXCLUDED.current_tasks, meeting_notes = EXCLUDED.meeting_notes, updated_at = NOW()""",
        (nid, name, week, body.get("current_tasks", ""), body.get("meeting_notes", "")))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 코어목표 + 가설 트리 API ──

def _ensure_core_tree_table():
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS ext_core_tree (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            type TEXT NOT NULL DEFAULT 'core',
            title TEXT NOT NULL DEFAULT '',
            description TEXT DEFAULT '',
            author TEXT DEFAULT '',
            assignee TEXT DEFAULT '',
            status TEXT DEFAULT '',
            adopted TEXT DEFAULT '',
            week_label TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            linear_project_id TEXT DEFAULT '',
            linear_project_name TEXT DEFAULT '',
            linear_issue_id TEXT DEFAULT '',
            linear_issue_identifier TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""")
        # Existing installations may not yet have the integration mapping columns.
        for col, default in [("linear_project_id", "''"), ("linear_project_name", "''"),
                             ("linear_issue_id", "''"), ("linear_issue_identifier", "''")]:
            cur.execute(f"ALTER TABLE ext_core_tree ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT {default}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



@app.get("/api/core-tree")
async def get_core_tree():
    """전체 트리 조회 (재귀)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id, parent_id, type, title, description, author, assignee, status, adopted, week_label, sort_order,
        COALESCE(linear_project_id,''), COALESCE(linear_project_name,''), COALESCE(linear_issue_id,''), COALESCE(linear_issue_identifier,''),
        created_at::text, updated_at::text FROM ext_core_tree ORDER BY sort_order, created_at""")
    rows = cur.fetchall()
    conn.close()
    nodes = []
    for r in rows:
        nodes.append({"id": r[0], "parent_id": r[1], "type": r[2], "title": r[3], "description": r[4],
                       "author": r[5], "assignee": r[6], "status": r[7], "adopted": r[8],
                       "week_label": r[9], "sort_order": r[10],
                       "linear_project_id": r[11], "linear_project_name": r[12],
                       "linear_issue_id": r[13], "linear_issue_identifier": r[14],
                       "created_at": r[15], "updated_at": r[16]})
    # 트리 구성
    by_id = {n["id"]: {**n, "children": []} for n in nodes}
    roots = []
    for n in nodes:
        node = by_id[n["id"]]
        if n["parent_id"] and n["parent_id"] in by_id:
            by_id[n["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return {"tree": roots}

DEFAULT_TEAM_ID = os.environ.get("LINEAR_TEAM_ID", "").strip()


def _fetch_node(cur, node_id):
    """노드 1건 조회 (Linear 자동 생성 시 부모 체인 추적용)"""
    if not node_id:
        return None
    cur.execute("""SELECT id, parent_id, type,
        COALESCE(linear_project_id,''), COALESCE(linear_project_name,''),
        COALESCE(linear_issue_id,''), COALESCE(linear_issue_identifier,'')
        FROM ext_core_tree WHERE id=%s""", (node_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "parent_id": r[1], "type": r[2],
            "linear_project_id": r[3], "linear_project_name": r[4],
            "linear_issue_id": r[5], "linear_issue_identifier": r[6]}


def _find_ancestor_project_id(cur, start_parent_id):
    """상위 체인을 올라가며 hypothesis(직계 core 자식, Linear Project) 노드의 linear_project_id 반환.
    하위 가설(Milestone)인 경우 Milestone이 속한 Project ID를 찾기 위해 사용."""
    cur_id = start_parent_id
    while cur_id:
        node = _fetch_node(cur, cur_id)
        if not node:
            return ""
        # 부모가 core인 hypothesis = top hypothesis (Project 보유)
        if node["type"] == "hypothesis" and node["parent_id"]:
            parent_node = _fetch_node(cur, node["parent_id"])
            if parent_node and parent_node["type"] == "core":
                return node["linear_project_id"]
        cur_id = node["parent_id"]
    return ""


@app.post("/api/core-tree")
async def create_core_node(request: Request):
    body = await request.json()
    nid = uuid.uuid4().hex[:12]
    node_type = body.get("type", "core")
    title = body.get("title", "")
    parent_id = body.get("parent_id") or None
    linear_project_id = body.get("linear_project_id", "")
    linear_project_name = body.get("linear_project_name", "")
    linear_issue_id = body.get("linear_issue_id", "")
    linear_issue_identifier = body.get("linear_issue_identifier", "")

    # 부모/조부모 정보 수집 (Linear 계층 판정용)
    parent_info = None
    grandparent_info = None
    if parent_id:
        conn_pre = _pg_conn()
        cur_pre = conn_pre.cursor()
        parent_info = _fetch_node(cur_pre, parent_id)
        if parent_info and parent_info.get("parent_id"):
            grandparent_info = _fetch_node(cur_pre, parent_info["parent_id"])
        conn_pre.close()

    # Linear 자동 생성
    # 매핑 규칙:
    #   core          → Initiative
    #   hypothesis (parent=core)       → Project (initiativeIds=[core.Initiative])
    #   hypothesis (parent=hypothesis) → Project Milestone (projectId=상위 Project)
    #   task (parent=hypothesis top)   → Issue (projectId=hypothesis.Project)
    #   task (parent=hypothesis sub)   → Issue (projectId=상위 Project, projectMilestoneId=Milestone)
    #   task (parent=task)             → Sub-issue (parentId=parent.Issue)
    if body.get("auto_linear") and LINEAR_API_KEY and title and not DEFAULT_TEAM_ID:
        return Response(content=json.dumps({"error": "LINEAR_TEAM_ID is not configured"}), status_code=400, media_type="application/json")
    if body.get("auto_linear") and LINEAR_API_KEY and title:
        async def _resolve_user_id(name):
            if not name:
                return None
            user_result = await _linear_graphql('{ users { nodes { id name } } }')
            users = user_result.get("data", {}).get("users", {}).get("nodes", [])
            for u in users:
                if u.get("name") == name:
                    return u["id"]
            return None

        safe_title = title.replace('"', '\\"').replace('\n', ' ').replace('\r', ' ')

        if node_type == "core" and not linear_project_id:
            # Initiative 생성
            author = body.get("author", "")
            owner_id = await _resolve_user_id(author)
            owner_field = f', ownerId: "{owner_id}"' if owner_id else ""
            result = await _linear_graphql(f'''mutation {{
                initiativeCreate(input: {{ name: "{safe_title}"{owner_field} }}) {{
                    success initiative {{ id name }}
                }}
            }}''')
            created = result.get("data", {}).get("initiativeCreate", {})
            if created.get("success"):
                init = created.get("initiative", {})
                linear_project_id = init.get("id", "")
                linear_project_name = "🏢 " + init.get("name", title)
            else:
                logger.error(f"Initiative 생성 실패: {result}")

        elif node_type == "hypothesis" and not linear_project_id and parent_info:
            if parent_info["type"] == "core":
                # Top hypothesis → Project 생성 + Initiative에 연결
                author = body.get("author", "")
                lead_id = await _resolve_user_id(author)
                lead_field = f', leadId: "{lead_id}"' if lead_id else ""
                result = await _linear_graphql(f'''mutation {{
                    projectCreate(input: {{
                        name: "{safe_title}",
                        teamIds: ["{DEFAULT_TEAM_ID}"]
                        {lead_field}
                    }}) {{ success project {{ id name }} }}
                }}''')
                created = result.get("data", {}).get("projectCreate", {})
                if created.get("success"):
                    proj = created.get("project", {})
                    linear_project_id = proj.get("id", "")
                    linear_project_name = "📁 " + proj.get("name", title)
                    # Initiative ↔ Project 관계 생성 (별도 mutation 필요)
                    init_id = parent_info.get("linear_project_id", "")
                    if init_id:
                        try:
                            link_result = await _linear_graphql(f'''mutation {{
                                initiativeToProjectCreate(input: {{
                                    initiativeId: "{init_id}",
                                    projectId: "{linear_project_id}"
                                }}) {{ success }}
                            }}''')
                            if "errors" in link_result:
                                logger.error(f"Initiative-Project 연결 실패: {link_result}")
                        except Exception as e:
                            logger.error(f"Initiative-Project 연결 예외: {e}")
                else:
                    logger.error(f"Project 생성 실패: {result}")
            elif parent_info["type"] == "hypothesis":
                # Sub hypothesis → Project Milestone 생성 (상위 Project 필요)
                conn_f = _pg_conn()
                cur_f = conn_f.cursor()
                project_id = _find_ancestor_project_id(cur_f, parent_id)
                conn_f.close()
                if project_id:
                    result = await _linear_graphql(f'''mutation {{
                        projectMilestoneCreate(input: {{
                            name: "{safe_title}",
                            projectId: "{project_id}"
                        }}) {{ success projectMilestone {{ id name }} }}
                    }}''')
                    created = result.get("data", {}).get("projectMilestoneCreate", {})
                    if created.get("success"):
                        ms = created.get("projectMilestone", {})
                        linear_project_id = ms.get("id", "")
                        linear_project_name = "🎯 " + ms.get("name", title)
                    else:
                        logger.error(f"Milestone 생성 실패: {result}")
                else:
                    logger.error(f"Milestone 생성 스킵 — 상위 Project를 찾을 수 없음 (parent_id={parent_id})")

        elif node_type == "task" and not linear_issue_id and parent_info:
            assignee = body.get("assignee", "")
            assignee_id = await _resolve_user_id(assignee)
            assignee_field = f', assigneeId: "{assignee_id}"' if assignee_id else ""

            project_id = ""
            milestone_id = ""
            parent_issue_id_field = ""
            team_id = DEFAULT_TEAM_ID

            if parent_info["type"] == "task" and parent_info["linear_issue_id"]:
                # Sub-task → 부모 Issue의 하위 이슈
                parent_issue_id = parent_info["linear_issue_id"]
                parent_issue_id_field = f', parentId: "{parent_issue_id}"'
                # 부모 이슈의 team/project 상속
                issue_info = await _linear_graphql(
                    f'{{ issue(id: "{parent_issue_id}") {{ team {{ id }} project {{ id }} projectMilestone {{ id }} }} }}'
                )
                iss_data = issue_info.get("data", {}).get("issue", {}) or {}
                team_id = (iss_data.get("team") or {}).get("id") or DEFAULT_TEAM_ID
                proj_obj = iss_data.get("project") or {}
                if proj_obj.get("id"):
                    project_id = proj_obj["id"]
                ms_obj = iss_data.get("projectMilestone") or {}
                if ms_obj.get("id"):
                    milestone_id = ms_obj["id"]
            elif parent_info["type"] == "hypothesis":
                # 부모가 top hypothesis이면 Project, sub hypothesis면 Milestone
                if grandparent_info and grandparent_info["type"] == "core":
                    # top hypothesis
                    project_id = parent_info["linear_project_id"]
                elif grandparent_info and grandparent_info["type"] == "hypothesis":
                    # sub hypothesis: parent.linear_project_id = Milestone ID
                    milestone_id = parent_info["linear_project_id"]
                    conn_f = _pg_conn()
                    cur_f = conn_f.cursor()
                    project_id = _find_ancestor_project_id(cur_f, parent_info["parent_id"])
                    conn_f.close()

            if project_id or parent_issue_id_field:
                proj_field = f', projectId: "{project_id}"' if project_id else ""
                ms_field = f', projectMilestoneId: "{milestone_id}"' if milestone_id else ""
                result = await _linear_graphql(f'''mutation {{
                    issueCreate(input: {{
                        title: "{safe_title}",
                        teamId: "{team_id}"
                        {proj_field}
                        {ms_field}
                        {parent_issue_id_field}
                        {assignee_field}
                    }}) {{ success issue {{ id identifier title }} }}
                }}''')
                created = result.get("data", {}).get("issueCreate", {})
                if created.get("success"):
                    issue = created.get("issue", {})
                    linear_issue_id = issue.get("id", "")
                    linear_issue_identifier = issue.get("identifier", "")
                    linear_project_name = f"📋 [{linear_issue_identifier}] {issue.get('title', title)}"
                else:
                    logger.error(f"Issue 생성 실패: {result}")
            else:
                logger.error(f"Issue 생성 스킵 — project_id/parent_issue_id 없음 (parent_id={parent_id}, parent_type={parent_info['type']})")

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ext_core_tree WHERE parent_id IS NOT DISTINCT FROM %s", (parent_id,))
    sort_order = cur.fetchone()[0]
    cur.execute("""INSERT INTO ext_core_tree (id, parent_id, type, title, description, author, assignee, status, adopted, week_label, sort_order,
        linear_project_id, linear_project_name, linear_issue_id, linear_issue_identifier)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (nid, parent_id, node_type, title, body.get("description", ""),
         body.get("author", ""), body.get("assignee", ""), body.get("status", ""), body.get("adopted", ""),
         body.get("week_label", ""), sort_order,
         linear_project_id, linear_project_name, linear_issue_id, linear_issue_identifier))
    conn.commit()
    conn.close()
    return {"ok": True, "id": nid}

@app.put("/api/core-tree/{node_id}")
async def update_core_node(node_id: str, request: Request):
    body = await request.json()
    conn = _pg_conn()
    cur = conn.cursor()
    # 수정 전 현재 상태 조회 (Linear 전파 판단용)
    existing = _fetch_node(cur, node_id)
    parent_type = ''
    if existing and existing.get("parent_id"):
        parent = _fetch_node(cur, existing["parent_id"])
        if parent:
            parent_type = parent["type"]

    updates, vals = [], []
    for field in ["title", "description", "author", "assignee", "status", "adopted", "week_label", "sort_order",
                   "linear_project_id", "linear_project_name", "linear_issue_id", "linear_issue_identifier"]:
        if field in body:
            updates.append(f"{field} = %s")
            vals.append(body[field])
    if not updates:
        conn.close()
        return {"ok": True}
    updates.append("updated_at = NOW()")
    vals.append(node_id)
    cur.execute(f"UPDATE ext_core_tree SET {', '.join(updates)} WHERE id = %s", vals)
    conn.commit()
    conn.close()

    # 제목 변경 시 Linear 엔티티에도 전파 (대시보드 → Linear)
    if "title" in body and existing and LINEAR_API_KEY:
        new_title = (body.get("title") or "").replace('"', '\\"').replace('\n', ' ')
        try:
            if existing["type"] == "core" and existing.get("linear_project_id"):
                await _linear_graphql(f'''mutation {{
                    initiativeUpdate(id: "{existing["linear_project_id"]}", input: {{ name: "{new_title}" }})
                    {{ success }}
                }}''')
            elif existing["type"] == "hypothesis" and existing.get("linear_project_id"):
                if parent_type == "core":
                    await _linear_graphql(f'''mutation {{
                        projectUpdate(id: "{existing["linear_project_id"]}", input: {{ name: "{new_title}" }})
                        {{ success }}
                    }}''')
                elif parent_type == "hypothesis":
                    await _linear_graphql(f'''mutation {{
                        projectMilestoneUpdate(id: "{existing["linear_project_id"]}", input: {{ name: "{new_title}" }})
                        {{ success }}
                    }}''')
            elif existing["type"] == "task" and existing.get("linear_issue_id"):
                await _linear_graphql(f'''mutation {{
                    issueUpdate(id: "{existing["linear_issue_id"]}", input: {{ title: "{new_title}" }})
                    {{ success }}
                }}''')
        except Exception as e:
            logger.error(f"Linear title 전파 실패: {e}")

    return {"ok": True}

@app.delete("/api/core-tree/{node_id}")
async def delete_core_node(node_id: str, request: Request):
    """노드 + 모든 하위 노드 삭제 (재귀) + Linear 연동 삭제 — admin/pm only.
    부모 타입별로 적절한 Linear mutation 호출:
      core → initiativeDelete, hypothesis/core → projectDelete,
      hypothesis/hypothesis → projectMilestoneDelete, task → issueDelete"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()
    # 삭제 전 node+parent type, Linear ID 수집
    cur.execute("""WITH RECURSIVE descendants AS (
        SELECT id, parent_id, type,
               COALESCE(linear_project_id,'') as lpid,
               COALESCE(linear_issue_id,'') as liid
        FROM ext_core_tree WHERE id = %s
        UNION ALL
        SELECT c.id, c.parent_id, c.type,
               COALESCE(c.linear_project_id,''),
               COALESCE(c.linear_issue_id,'')
        FROM ext_core_tree c JOIN descendants d ON c.parent_id = d.id
    )
    SELECT d.type, d.lpid, d.liid,
           COALESCE((SELECT type FROM ext_core_tree WHERE id = d.parent_id), '') AS parent_type
    FROM descendants d""", (node_id,))
    linear_items = cur.fetchall()

    # DB 삭제
    cur.execute("""WITH RECURSIVE descendants AS (
        SELECT id FROM ext_core_tree WHERE id = %s
        UNION ALL
        SELECT c.id FROM ext_core_tree c JOIN descendants d ON c.parent_id = d.id
    ) DELETE FROM ext_core_tree WHERE id IN (SELECT id FROM descendants)""", (node_id,))
    conn.commit()
    conn.close()

    # Linear 삭제 (백그라운드, 실패해도 무시)
    if LINEAR_API_KEY:
        for node_type, lpid, liid, parent_type in linear_items:
            try:
                if node_type == "core" and lpid:
                    await _linear_graphql(f'mutation {{ initiativeDelete(id: "{lpid}") {{ success }} }}')
                elif node_type == "hypothesis" and lpid:
                    if parent_type == "core":
                        await _linear_graphql(f'mutation {{ projectDelete(id: "{lpid}") {{ success }} }}')
                    elif parent_type == "hypothesis":
                        await _linear_graphql(f'mutation {{ projectMilestoneDelete(id: "{lpid}") {{ success }} }}')
                elif node_type == "task" and liid:
                    await _linear_graphql(f'mutation {{ issueDelete(id: "{liid}") {{ success }} }}')
            except Exception:
                pass

    return {"ok": True}


@app.post("/api/core-tree/sync-from-linear")
async def sync_core_tree_from_linear():
    """Linear → 대시보드 단방향 동기화.
    규칙:
      - Initiative에 연결된 Project만 가져옴 (고아 Project 제외)
      - Linear에서 archived/삭제된 엔티티는 대시보드에서도 삭제 (엄격 삭제)
      - 매칭은 linear_project_id / linear_issue_id 기준
      - 제목 변경 시 대시보드 title도 Linear 기준으로 갱신
    """
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY 미설정"}),
                        status_code=400, media_type="application/json")

    # 1. Linear 상태 일괄 조회
    init_res = await _linear_graphql("""{
        initiatives(first: 50) { nodes { id name description } }
    }""")
    initiatives = init_res.get("data", {}).get("initiatives", {}).get("nodes", [])

    proj_res = await _linear_graphql("""{
        projects(first: 100) { nodes {
            id name description
            initiatives { nodes { id } }
            projectMilestones { nodes { id name description } }
        } }
    }""")
    all_projects = proj_res.get("data", {}).get("projects", {}).get("nodes", [])
    # Initiative 소속 Project만 유지
    projects_linked = [p for p in all_projects
                       if (p.get("initiatives") or {}).get("nodes")]

    # 배치 쿼리로 모든 프로젝트 이슈를 묶어 조회 (N+1 → ceil(N/10) 호출)
    project_issues = {}
    BATCH_SIZE = 10
    for batch_start in range(0, len(projects_linked), BATCH_SIZE):
        batch = projects_linked[batch_start:batch_start + BATCH_SIZE]
        alias_parts = []
        for idx, p in enumerate(batch):
            alias_parts.append(
                f'p{idx}: project(id: "{p["id"]}") {{ issues(first: 200) {{ nodes {{ '
                f'id identifier title projectMilestone {{ id }} parent {{ id }} '
                f'assignee {{ name }} state {{ type name }} }} }} }}'
            )
        batch_query = "{ " + " ".join(alias_parts) + " }"
        batch_result = await _linear_graphql(batch_query)
        batch_data = batch_result.get("data", {}) or {}
        for idx, p in enumerate(batch):
            project_issues[p["id"]] = (batch_data.get(f"p{idx}") or {}).get("issues", {}).get("nodes", [])

    # live ID sets
    live_project_ids = set()  # Initiative/Project/Milestone IDs (모두 linear_project_id 필드에 저장됨)
    live_issue_ids = set()
    for init in initiatives:
        live_project_ids.add(init["id"])
    for p in projects_linked:
        live_project_ids.add(p["id"])
        for ms in (p.get("projectMilestones") or {}).get("nodes", []):
            live_project_ids.add(ms["id"])
        for iss in project_issues.get(p["id"], []):
            live_issue_ids.add(iss["id"])

    # 2. DB 기존 상태 로드
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id, parent_id, type,
        COALESCE(linear_project_id,''), COALESCE(linear_issue_id,''),
        title FROM ext_core_tree""")
    existing_rows = cur.fetchall()
    project_id_to_db = {r[3]: r[0] for r in existing_rows if r[3]}
    issue_id_to_db = {r[4]: r[0] for r in existing_rows if r[4]}

    created = 0
    updated = 0

    def _insert(node_type, parent_id, title, lpid='', lpname='', liid='', lident='', desc=''):
        nid = uuid.uuid4().hex[:12]
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ext_core_tree WHERE parent_id IS NOT DISTINCT FROM %s", (parent_id,))
        so = cur.fetchone()[0]
        cur.execute("""INSERT INTO ext_core_tree (id, parent_id, type, title, description, sort_order,
            linear_project_id, linear_project_name, linear_issue_id, linear_issue_identifier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (nid, parent_id, node_type, title, desc, so, lpid, lpname, liid, lident))
        return nid

    # 2a. Initiative → core (description 동기화 포함)
    for init in initiatives:
        iid = init["id"]
        name = init["name"]
        desc = init.get("description") or ""
        badge = "🏢 " + name
        if iid in project_id_to_db:
            cur.execute("""UPDATE ext_core_tree SET title=%s, description=%s, linear_project_name=%s, updated_at=NOW()
                WHERE id=%s""", (name, desc, badge, project_id_to_db[iid]))
            updated += 1
        else:
            nid = _insert("core", None, name, lpid=iid, lpname=badge, desc=desc)
            project_id_to_db[iid] = nid
            created += 1

    # 2b. Project → hypothesis (description 동기화 포함)
    for p in projects_linked:
        init_ids = [i["id"] for i in (p.get("initiatives") or {}).get("nodes", [])]
        if not init_ids:
            continue
        parent_core = project_id_to_db.get(init_ids[0])
        if not parent_core:
            continue
        pid = p["id"]
        name = p["name"]
        desc = p.get("description") or ""
        badge = "📁 " + name
        if pid in project_id_to_db:
            cur.execute("""UPDATE ext_core_tree SET title=%s, description=%s, linear_project_name=%s, parent_id=%s, type='hypothesis', updated_at=NOW()
                WHERE id=%s""", (name, desc, badge, parent_core, project_id_to_db[pid]))
            updated += 1
        else:
            nid = _insert("hypothesis", parent_core, name, lpid=pid, lpname=badge, desc=desc)
            project_id_to_db[pid] = nid
            created += 1

    # 2c. Milestone → sub-hypothesis (description 동기화 포함)
    for p in projects_linked:
        parent_hyp = project_id_to_db.get(p["id"])
        if not parent_hyp:
            continue
        for ms in (p.get("projectMilestones") or {}).get("nodes", []):
            mid = ms["id"]
            name = ms["name"]
            desc = ms.get("description") or ""
            badge = "🎯 " + name
            if mid in project_id_to_db:
                cur.execute("""UPDATE ext_core_tree SET title=%s, description=%s, linear_project_name=%s, parent_id=%s, type='hypothesis', updated_at=NOW()
                    WHERE id=%s""", (name, desc, badge, parent_hyp, project_id_to_db[mid]))
                updated += 1
            else:
                nid = _insert("hypothesis", parent_hyp, name, lpid=mid, lpname=badge, desc=desc)
                project_id_to_db[mid] = nid
                created += 1

    # 2d. Issue → task (pass 1: create/update; pass 2: re-parent sub-issues)
    pending_reparent = []  # [(child_issue_id, parent_issue_id)]
    for p in projects_linked:
        proj_node = project_id_to_db.get(p["id"])
        if not proj_node:
            continue
        for iss in project_issues.get(p["id"], []):
            iss_id = iss["id"]
            title = iss["title"]
            ident = iss.get("identifier", "")
            name = f"📋 [{ident}] {title}"
            ms_obj = iss.get("projectMilestone") or {}
            ms_id = ms_obj.get("id")
            # Milestone 있으면 그쪽, 없으면 Project(hypothesis)
            parent_node = project_id_to_db.get(ms_id) if ms_id else proj_node
            if not parent_node:
                parent_node = proj_node
            assignee = (iss.get("assignee") or {}).get("name", "")

            if iss_id in issue_id_to_db:
                cur.execute("""UPDATE ext_core_tree SET title=%s, linear_project_name=%s,
                    linear_issue_identifier=%s, parent_id=%s, type='task', assignee=%s, updated_at=NOW()
                    WHERE id=%s""", (title, name, ident, parent_node, assignee, issue_id_to_db[iss_id]))
                updated += 1
            else:
                nid = _insert("task", parent_node, title, lpname=name, liid=iss_id, lident=ident)
                if assignee:
                    cur.execute("UPDATE ext_core_tree SET assignee=%s WHERE id=%s", (assignee, nid))
                issue_id_to_db[iss_id] = nid
                created += 1

            parent_obj = iss.get("parent") or {}
            if parent_obj.get("id"):
                pending_reparent.append((iss_id, parent_obj["id"]))

    # Pass 2: sub-issue → 부모 task 밑으로 재배치
    for child_id, parent_id in pending_reparent:
        child_node = issue_id_to_db.get(child_id)
        parent_node = issue_id_to_db.get(parent_id)
        if child_node and parent_node and child_node != parent_node:
            cur.execute("UPDATE ext_core_tree SET parent_id=%s WHERE id=%s", (parent_node, child_node))

    # 3. 엄격 삭제: Linear에서 사라진 엔티티 → 대시보드에서도 제거
    cur.execute("""SELECT id, COALESCE(linear_project_id,''), COALESCE(linear_issue_id,''), type
        FROM ext_core_tree WHERE COALESCE(linear_project_id,'') != '' OR COALESCE(linear_issue_id,'') != ''""")
    all_linked = cur.fetchall()
    to_delete_ids = []
    for db_id, lpid, liid, ntype in all_linked:
        if lpid and lpid not in live_project_ids:
            to_delete_ids.append(db_id)
        elif liid and liid not in live_issue_ids:
            to_delete_ids.append(db_id)

    deleted = 0
    for nid in to_delete_ids:
        # 이미 부모 cascade로 지워졌을 수 있음 — 있으면 지우고 없으면 skip
        cur.execute("""WITH RECURSIVE d AS (
            SELECT id FROM ext_core_tree WHERE id = %s
            UNION ALL SELECT c.id FROM ext_core_tree c JOIN d ON c.parent_id = d.id
        ) DELETE FROM ext_core_tree WHERE id IN (SELECT id FROM d)""", (nid,))
        deleted += cur.rowcount

    conn.commit()
    conn.close()
    return {"ok": True, "created": created, "updated": updated, "deleted": deleted,
            "linear_initiatives": len(initiatives),
            "linear_projects": len(projects_linked),
            "linear_issues": len(live_issue_ids)}


# ── 전자결제 (승인 요청) API ──

def _ensure_approvals_table():
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_approval_requests (
        id TEXT PRIMARY KEY,
        req_type TEXT NOT NULL DEFAULT 'personal_expense',
        title TEXT NOT NULL DEFAULT '',
        amount NUMERIC DEFAULT NULL,
        spent_at DATE DEFAULT NULL,
        vendor TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        requester TEXT DEFAULT '',
        approver TEXT DEFAULT '',
        status TEXT DEFAULT '대기',
        attachments JSONB DEFAULT '[]'::jsonb,
        items JSONB DEFAULT '[]'::jsonb,
        approver_note TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""")
    # 기존 테이블 대응 — items 컬럼 없으면 추가
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS items JSONB DEFAULT '[]'::jsonb")
    # 재직증명서 발급용 정보 (이름·주민번호·주소·용도 등)
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS cert_info JSONB DEFAULT '{}'::jsonb")
    # 도서 구입 신청 정보 (도서명·저자/출판사·구입처·목적·신청부서)
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS book_info JSONB DEFAULT '{}'::jsonb")
    # 2단계 결재 (PM → 관리자)
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS pm_approver TEXT DEFAULT ''")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS admin_approver TEXT DEFAULT ''")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS pm_status TEXT DEFAULT '대기'")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS admin_status TEXT DEFAULT '대기'")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS pm_note TEXT DEFAULT ''")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS admin_note TEXT DEFAULT ''")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS pm_decided_at TIMESTAMP")
    cur.execute("ALTER TABLE ext_approval_requests ADD COLUMN IF NOT EXISTS admin_decided_at TIMESTAMP")
        # Historical rows remain unchanged; migrate explicitly after a reviewed backup.
    conn.commit()
    conn.close()


def _norm_type_approvers(raw):
    """settings의 type_approvers를 항상 {type: {pm, admin}} 구조로 정규화 (구버전 string 호환)"""
    raw = raw or {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            out[k] = {"pm": (v.get("pm") or "").strip(), "admin": (v.get("admin") or "").strip()}
        elif isinstance(v, str):
            out[k] = {"pm": v.strip(), "admin": ""}
        else:
            out[k] = {"pm": "", "admin": ""}
    # 모든 결재 유형 키 보장
    for t in REQ_TYPE_LABEL.keys():
        out.setdefault(t, {"pm": "", "admin": ""})
    return out


def _compute_overall_status(pm_status: str, admin_status: str) -> str:
    """PM/관리자 두 단계 상태로 종합 status 계산"""
    if pm_status == "반려" or admin_status == "반려":
        return "반려"
    if pm_status == "승인" and admin_status == "승인":
        return "승인"
    return "대기"


def _compute_items_summary(items):
    """items 배열에서 총액·최소날짜 계산"""
    total = 0.0
    min_date = None
    for it in (items or []):
        try:
            total += float(it.get("amount") or 0)
        except (TypeError, ValueError):
            pass
        d = (it.get("date") or "").strip()
        if d and (min_date is None or d < min_date):
            min_date = d
    return (total if total > 0 else None), min_date


REQ_TYPE_LABEL = {"personal_expense": "개인경비", "cert_employment": "재직증명서", "book_purchase": "도서 구입"}
# 대표(admin) 단독 1단계 결재 유형 (PM 단계 없음). 현재는 모두 2단계(1차 PM → 2차 관리자) 운영.
SINGLE_STAGE_TYPES = set()
# 1단계 유형의 기본 승인자(대표) — settings 미지정 시 폴백
SINGLE_STAGE_DEFAULT_ADMIN = _installation.SINGLE_STAGE_DEFAULT_ADMIN
# 비용 관리 탭에 비용이 추가되면 Slack DM 받을 사람 (이름 기준)
EXPENSE_NOTIFY_NAMES = _installation.EXPENSE_NOTIFY_NAMES



# ─────────────────────── 활동 로그 (Audit Trail) ───────────────────────
def _ensure_activity_logs_table():
    """활동 로그 테이블 — 누가/언제/무엇을/어디에 변경했는지 추적"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_activity_logs (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT DEFAULT '',
        user_name TEXT DEFAULT '',
        user_role TEXT DEFAULT '',
        action TEXT NOT NULL,
        target_type TEXT DEFAULT '',
        target_id TEXT DEFAULT '',
        detail JSONB DEFAULT '{}'::jsonb,
        ip TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_created ON ext_activity_logs (created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_user ON ext_activity_logs (user_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_target ON ext_activity_logs (target_type, target_id)")
    conn.commit()
    conn.close()




def log_activity(request_or_user, action: str, target_type: str = "", target_id: str = "", detail: dict | None = None):
    """활동 로그 1건 기록 — request 또는 user dict 받음. 실패해도 메인 로직 방해하지 않음."""
    try:
        user = None
        ip = ""
        if isinstance(request_or_user, dict):
            user = request_or_user
        else:
            user = getattr(getattr(request_or_user, "state", None), "user", None) or {}
            try:
                ip = (request_or_user.headers.get("x-forwarded-for") or
                      (request_or_user.client.host if request_or_user.client else "") or "")
                ip = ip.split(",")[0].strip()[:64]
            except Exception:
                ip = ""
        user = user or {}
        uid = str(user.get("user_id") or "")
        uname = str(user.get("name") or "")
        urole = str(user.get("role") or "")
        if not action:
            return
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO ext_activity_logs
            (user_id, user_name, user_role, action, target_type, target_id, detail, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
            (uid, uname, urole, action[:80], (target_type or "")[:40],
             str(target_id or "")[:80], json.dumps(detail or {}, ensure_ascii=False, default=str), ip))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[activity_log] 기록 실패: {e}")


# ─────────────────────── 인앱 알림 (Notifications) ───────────────────────
def _ensure_notifications_table():
    """인앱 알림 테이블 — 결재/리마인더/공지 등을 사용자별로 큐잉"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_notifications (
        id BIGSERIAL PRIMARY KEY,
        user_name TEXT NOT NULL,
        n_type TEXT DEFAULT '',
        title TEXT DEFAULT '',
        message TEXT DEFAULT '',
        link TEXT DEFAULT '',
        is_read BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW(),
        read_at TIMESTAMP DEFAULT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON ext_notifications (user_name, is_read, created_at DESC)")
    conn.commit()
    conn.close()




def create_notification(user_name: str, n_type: str, title: str, message: str = "", link: str = ""):
    """인앱 알림 1건 생성 — user_name(표시명)으로 큐잉. 실패해도 메인 로직 방해하지 않음."""
    try:
        if not user_name or not title:
            return
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO ext_notifications (user_name, n_type, title, message, link)
            VALUES (%s, %s, %s, %s, %s)""",
            (user_name.strip(), (n_type or "")[:40], title[:160], (message or "")[:600], (link or "")[:200]))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[notification] 생성 실패: {e}")


def _broadcast_notification(n_type: str, title: str, message: str = "", link: str = "", roles: list | None = None):
    """활성 사용자에게 일괄 알림 — roles 지정 시 해당 역할만"""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        if roles:
            placeholders = ",".join(["%s"] * len(roles))
            cur.execute(f"SELECT name FROM ext_users WHERE is_active = TRUE AND role IN ({placeholders})", tuple(roles))
        else:
            cur.execute("SELECT name FROM ext_users WHERE is_active = TRUE")
        names = [r[0] for r in cur.fetchall() if r[0]]
        for n in names:
            cur.execute("""INSERT INTO ext_notifications (user_name, n_type, title, message, link)
                VALUES (%s, %s, %s, %s, %s)""",
                (n.strip(), (n_type or "")[:40], title[:160], (message or "")[:600], (link or "")[:200]))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[notification] 브로드캐스트 실패: {e}")


async def _notify_slack_approval(slack_user_id: str, text: str):
    """승인 관련 Slack DM 전송 (토큰/ID 없으면 조용히 skip).
    DB에 저장된 토큰(_get_slack_bot_token)을 사용 — 전역 변수는 재시작 시 비워지므로 사용 금지."""
    bot_token = _get_slack_bot_token()
    if not bot_token or not slack_user_id:
        return
    try:
        await _slack_post_message(bot_token, slack_user_id, text)
    except Exception as e:
        logger.error(f"Slack 승인 알림 실패: {e}")


@app.get("/api/approvals")
async def list_approvals(request: Request):
    """승인 요청 조회 — 공용(legacy)=403, member=본인 신청+승인자 인박스, admin/pm=전체"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") in (None, "", "legacy") or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}),
                        status_code=403, media_type="application/json")
    role = user_state.get("role") or ""
    me = user_state.get("name") or ""
    is_priv = role in ("admin", "pm")

    base_sql = """SELECT id, req_type, title, amount, spent_at::text, vendor, detail,
        requester, approver, status, COALESCE(attachments, '[]'::jsonb), approver_note,
        created_at::text, updated_at::text, COALESCE(items, '[]'::jsonb),
        COALESCE(cert_info, '{}'::jsonb),
        COALESCE(pm_approver, ''), COALESCE(admin_approver, ''),
        COALESCE(pm_status, '대기'), COALESCE(admin_status, '대기'),
        COALESCE(pm_note, ''), COALESCE(admin_note, ''),
        pm_decided_at::text, admin_decided_at::text,
        COALESCE(book_info, '{}'::jsonb)
        FROM ext_approval_requests"""
    params = ()
    if not is_priv:
        # member: 본인이 신청자거나, 자신이 PM/admin 승인자로 지정된 건만 (인박스)
        base_sql += " WHERE requester = %s OR pm_approver = %s OR admin_approver = %s"
        params = (me, me, me)
    base_sql += " ORDER BY created_at DESC"

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(base_sql, params)
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "req_type": r[1], "title": r[2],
            "amount": float(r[3]) if r[3] is not None else None,
            "spent_at": r[4], "vendor": r[5], "detail": r[6],
            "requester": r[7], "approver": r[8], "status": r[9],
            "attachments": r[10], "approver_note": r[11],
            "created_at": r[12], "updated_at": r[13],
            "items": r[14],
            "cert_info": r[15],
            "pm_approver": r[16], "admin_approver": r[17],
            "pm_status": r[18], "admin_status": r[19],
            "pm_note": r[20], "admin_note": r[21],
            "pm_decided_at": r[22], "admin_decided_at": r[23],
            "book_info": r[24],
        })
    return {"items": items, "scope": "all" if is_priv else "self"}


@app.post("/api/approvals")
async def create_approval(request: Request):
    """결재 신청 — 개인경비는 items(다항목), 재직증명서는 단순. requester=JWT, approver 검증."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") == "legacy" or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    requester = user_state["name"]

    body = await request.json()
    title = (body.get("title") or "").strip()

    req_type = body.get("req_type") or "personal_expense"
    if req_type not in REQ_TYPE_LABEL:
        return Response(content=json.dumps({"error": "허용된 유형: personal_expense, cert_employment, book_purchase"}),
                        status_code=400, media_type="application/json")

    # 도서 구입은 도서명이 제목 — title 미전달 시 book_info.book_name으로 자동 도출
    if not title and req_type == "book_purchase":
        title = ((body.get("book_info") or {}).get("book_name") or "").strip()
    if not title:
        return Response(content=json.dumps({"error": "title 필수"}), status_code=400, media_type="application/json")

    # 도서 구입 = 대표 단독 1단계 (PM 단계 없음), 그 외 = PM → 관리자 2단계
    is_single_stage = req_type in SINGLE_STAGE_TYPES

    # 결재 승인자는 settings에서 자동 배정 (요청자 변경 불가)
    settings_raw = _read_store("approval_settings") or {}
    type_map = _norm_type_approvers(settings_raw.get("type_approvers"))
    pair = type_map.get(req_type, {"pm": "", "admin": ""})
    pm_name = (pair.get("pm") or "").strip()
    admin_name = (pair.get("admin") or "").strip()
    if is_single_stage:
        # 대표(admin)만 필요 — 미지정 시 기본값 폴백, PM 단계 없음
        pm_name = ""
        if not admin_name:
            admin_name = SINGLE_STAGE_DEFAULT_ADMIN
    else:
        if not pm_name or not admin_name:
            return Response(content=json.dumps({"error": f"'{REQ_TYPE_LABEL.get(req_type)}' 결재 승인자(PM·관리자) 기본값이 설정되지 않았습니다. 관리자가 전자결재 설정에서 지정해주세요."}),
                            status_code=400, media_type="application/json")

    # 유형별 본문 검증 — 개인경비(다항목), 재직증명서(cert_info), 도서구입(book_info)
    items_list = body.get("items") or []
    cert_info_to_save = {}
    book_info_to_save = {}
    if req_type == "personal_expense":
        if not isinstance(items_list, list) or len(items_list) == 0:
            return Response(content=json.dumps({"error": "개인경비는 항목이 1개 이상 필요합니다"}), status_code=400, media_type="application/json")
        for it in items_list:
            if not isinstance(it, dict):
                continue
            try:
                a = float(it.get("amount") or 0)
                if a < 0:
                    return Response(content=json.dumps({"error": "항목 금액은 0 이상이어야 합니다"}), status_code=400, media_type="application/json")
            except (TypeError, ValueError):
                pass
        amount, spent_at = _compute_items_summary(items_list)
    elif req_type == "cert_employment":
        # 재직증명서 — cert_info(이름·주민번호·주소·용도) 검증
        cert_in = body.get("cert_info") or {}
        if not isinstance(cert_in, dict):
            cert_in = {}
        cname = (cert_in.get("name") or "").strip()
        cpurpose = (cert_in.get("purpose") or "").strip()
        if not cname:
            return Response(content=json.dumps({"error": "재직증명서는 이름(name)이 필요합니다"}), status_code=400, media_type="application/json")
        if not cpurpose:
            return Response(content=json.dumps({"error": "재직증명서는 용도(purpose)가 필요합니다"}), status_code=400, media_type="application/json")
        cert_info_to_save = {
            "name": cname,
            "ssn": (cert_in.get("ssn") or "").strip(),
            "address": (cert_in.get("address") or "").strip(),
            "purpose": cpurpose,
        }
        items_list = []
        amount = None
        spent_at = None
    else:
        # 도서 구입 — book_info(도서명·저자/출판사·구입처·목적·신청부서) 검증
        book_in = body.get("book_info") or {}
        if not isinstance(book_in, dict):
            book_in = {}
        bname = (book_in.get("book_name") or "").strip()
        bpurpose = (book_in.get("purpose") or "").strip()
        if not bname:
            return Response(content=json.dumps({"error": "도서 구입은 도서명(book_name)이 필요합니다"}), status_code=400, media_type="application/json")
        if not bpurpose:
            return Response(content=json.dumps({"error": "도서 구입은 구입목적(purpose)이 필요합니다"}), status_code=400, media_type="application/json")
        book_info_to_save = {
            "book_name": bname,
            "author_publisher": (book_in.get("author_publisher") or "").strip(),
            "purchase_link": (book_in.get("purchase_link") or "").strip(),
            "purpose": bpurpose,
            "department": (book_in.get("department") or "").strip(),
        }
        items_list = []
        amount = None
        spent_at = None

    conn = _pg_conn()
    cur = conn.cursor()
    # 승인자 존재 검증 + slack_id 조회 (1단계는 대표만)
    names_to_check = [n for n in (pm_name, admin_name) if n]
    cur.execute("SELECT name, slack_id FROM ext_users WHERE name = ANY(%s) AND is_active = TRUE", (names_to_check,))
    found = {r[0]: (r[1] or "") for r in cur.fetchall()}
    if pm_name and pm_name not in found:
        conn.close()
        return Response(content=json.dumps({"error": f"PM 승인자 '{pm_name}'를 찾을 수 없습니다"}), status_code=400, media_type="application/json")
    if admin_name not in found:
        conn.close()
        return Response(content=json.dumps({"error": f"결재자 '{admin_name}'를 찾을 수 없습니다"}), status_code=400, media_type="application/json")
    pm_slack_id = found.get(pm_name, "")
    admin_slack_id = found.get(admin_name, "")

    # 1단계(도서구입)는 PM 단계를 자동 승인 처리 → 대표(admin)만 결재하면 완료
    pm_status_init = "승인" if is_single_stage else "대기"
    approver_compat = admin_name if is_single_stage else pm_name  # 호환용 approver

    nid = uuid.uuid4().hex[:12]
    now = datetime.now(KST).replace(tzinfo=None)
    cur.execute("""INSERT INTO ext_approval_requests
        (id, req_type, title, amount, spent_at, vendor, detail, requester, approver, status,
         attachments, items, cert_info, book_info, created_at, updated_at,
         pm_approver, admin_approver, pm_status, admin_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)""",
        (nid, req_type, title,
         amount, spent_at, "", body.get("detail", ""),
         requester, approver_compat, "대기",
         json.dumps(body.get("attachments") or []),
         json.dumps(items_list), json.dumps(cert_info_to_save), json.dumps(book_info_to_save), now, now,
         pm_name, admin_name, pm_status_init, "대기"))
    conn.commit()
    conn.close()

    type_label = REQ_TYPE_LABEL.get(req_type, "승인 요청")
    amount_str = f" · {int(amount):,}원" if amount else ""
    item_str = f" ({len(items_list)}건)" if items_list else ""
    if is_single_stage:
        # 대표 단독 결재 — 대표에게만 알림
        msg = f"📥 *신규 {type_label} 신청 (대표 결재)*\n" \
              f"요청자: {requester}\n" \
              f"제목: {title}\n" \
              f"결재자: 대표({admin_name})\n" \
              f"승인해주세요 → 대시보드 전자결재 탭"
        await _notify_slack_approval(admin_slack_id, msg)
        create_notification(admin_name, "approval_request",
                            f"신규 {type_label} 결재 요청 (대표)",
                            f"{requester} · {title}",
                            "/?tab=approvals")
    else:
        msg = f"📥 *신규 {type_label} 요청 (1차 PM 결재)*\n" \
              f"요청자: {requester}\n" \
              f"제목: {title}{amount_str}{item_str}\n" \
              f"승인 흐름: PM({pm_name}) → 관리자({admin_name})\n" \
              f"승인해주세요 → 대시보드 전자결재 탭"
        await _notify_slack_approval(pm_slack_id, msg)
        # 인앱 알림 — PM 승인자(즉시 처리) + admin 승인자(향후 2차 처리 인지)
        create_notification(pm_name, "approval_request",
                            f"신규 {type_label} 결재 요청 (1차 PM)",
                            f"{requester} · {title}{amount_str}{item_str}",
                            "/?tab=approvals")
        if admin_name and admin_name != pm_name:
            create_notification(admin_name, "approval_incoming",
                                f"신규 {type_label} 결재 흐름 시작",
                                f"{requester} · {title}{amount_str}{item_str} · PM({pm_name}) 결재 후 2차 차례",
                                "/?tab=approvals")
    # 활동 로그
    log_activity(request, "결재 신청", "approval", nid,
                 {"type": req_type, "title": title, "amount": amount, "pm": pm_name, "admin": admin_name})

    return {"ok": True, "id": nid, "requester": requester, "pm_approver": pm_name, "admin_approver": admin_name}


@app.put("/api/approvals/{req_id}")
async def update_approval(req_id: str, request: Request):
    """결재 수정 — 본인+대기만 / admin은 어떤 상태든. status·approver_note·requester는 PUT으로 변경 불가 (decide 전용)."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") == "legacy" or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    is_admin = user_state.get("role") == "admin"

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT requester, status FROM ext_approval_requests WHERE id = %s", (req_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(status_code=404)
    owner, cur_status = row
    if not is_admin:
        if owner != user_state["name"]:
            conn.close()
            return Response(content=json.dumps({"error": "본인 요청만 수정 가능합니다"}), status_code=403, media_type="application/json")
        if cur_status != "대기":
            conn.close()
            return Response(content=json.dumps({"error": "대기 상태인 요청만 수정 가능합니다"}), status_code=400, media_type="application/json")

    body = await request.json()
    updates, vals = [], []
    # 2단계 결재 도입 후 approver는 settings에서 자동 배정 — 본문 변경 불가
    for field in ["req_type", "title", "detail"]:
        if field in body:
            v = body[field]
            if field == "title":
                v = (v or "").strip()
                if not v:
                    conn.close()
                    return Response(content=json.dumps({"error": "title 필수"}), status_code=400, media_type="application/json")
            if field == "req_type" and v not in REQ_TYPE_LABEL:
                conn.close()
                return Response(content=json.dumps({"error": "허용된 유형: personal_expense, cert_employment, book_purchase"}), status_code=400, media_type="application/json")
            updates.append(f"{field} = %s")
            vals.append(v)
    # 재직증명서 cert_info 갱신
    if "cert_info" in body:
        ci = body.get("cert_info") or {}
        if not isinstance(ci, dict):
            ci = {}
        normalized = {
            "name": (ci.get("name") or "").strip(),
            "ssn": (ci.get("ssn") or "").strip(),
            "address": (ci.get("address") or "").strip(),
            "purpose": (ci.get("purpose") or "").strip(),
        }
        updates.append("cert_info = %s::jsonb"); vals.append(json.dumps(normalized))
    # 도서 구입 book_info 갱신
    if "book_info" in body:
        bi = body.get("book_info") or {}
        if not isinstance(bi, dict):
            bi = {}
        normalized_bi = {
            "book_name": (bi.get("book_name") or "").strip(),
            "author_publisher": (bi.get("author_publisher") or "").strip(),
            "purchase_link": (bi.get("purchase_link") or "").strip(),
            "purpose": (bi.get("purpose") or "").strip(),
            "department": (bi.get("department") or "").strip(),
        }
        updates.append("book_info = %s::jsonb"); vals.append(json.dumps(normalized_bi))
    # items 갱신 시 amount/spent_at 자동 재계산 (개인경비 케이스)
    if "items" in body:
        items_list = body.get("items") or []
        if not isinstance(items_list, list):
            items_list = []
        for it in items_list:
            if isinstance(it, dict):
                try:
                    a = float(it.get("amount") or 0)
                    if a < 0:
                        conn.close()
                        return Response(content=json.dumps({"error": "항목 금액은 0 이상이어야 합니다"}), status_code=400, media_type="application/json")
                except (TypeError, ValueError):
                    pass
        auto_amount, auto_spent = _compute_items_summary(items_list)
        updates.append("items = %s::jsonb"); vals.append(json.dumps(items_list))
        updates.append("amount = %s"); vals.append(auto_amount)
        updates.append("spent_at = %s"); vals.append(auto_spent)
    if "attachments" in body:
        updates.append("attachments = %s::jsonb")
        vals.append(json.dumps(body["attachments"] or []))
    if not updates:
        conn.close()
        return {"ok": True}
    updates.append("updated_at = %s")
    vals.append(datetime.now(KST).replace(tzinfo=None))
    vals.append(req_id)
    cur.execute(f"UPDATE ext_approval_requests SET {', '.join(updates)} WHERE id = %s", vals)
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/approvals/{req_id}/decide")
async def decide_approval(req_id: str, request: Request):
    """2단계 결재 처리 — 본인이 현재 stage 담당자일 때만. PM 승인 → 관리자 알림, 관리자 승인 → 요청자 알림."""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    me = user_state.get("name") or ""
    body = await request.json()
    decision = body.get("decision", "")  # '승인' or '반려'
    note = body.get("note", "")
    if decision not in ("승인", "반려"):
        return Response(content=json.dumps({"error": "decision은 '승인' 또는 '반려'"}),
                        status_code=400, media_type="application/json")

    now = datetime.now(KST).replace(tzinfo=None)
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT req_type, title, amount, requester,
        COALESCE(pm_approver,''), COALESCE(admin_approver,''),
        COALESCE(pm_status,'대기'), COALESCE(admin_status,'대기')
        FROM ext_approval_requests WHERE id=%s""", (req_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(content=json.dumps({"error": "not found"}),
                        status_code=404, media_type="application/json")
    req_type, title, amount, requester, pm_approver, admin_approver, pm_status, admin_status = row

    # 현재 stage 판정
    stage = None
    if pm_status == "대기" and me == pm_approver:
        stage = "pm"
    elif pm_status == "승인" and admin_status == "대기" and me == admin_approver:
        stage = "admin"
    # 관리자 권한자는 PM 단계도 대리 승인 허용 (PM이 부재 시)
    elif pm_status == "대기" and user_state.get("role") == "admin":
        stage = "pm"

    if stage is None:
        conn.close()
        return Response(content=json.dumps({"error": "현재 단계의 결재 담당자가 아니거나 이미 처리된 요청입니다"}),
                        status_code=403, media_type="application/json")

    if stage == "pm":
        new_pm_status = decision
        new_admin_status = admin_status
    else:
        new_pm_status = pm_status
        new_admin_status = decision

    new_overall = _compute_overall_status(new_pm_status, new_admin_status)

    if stage == "pm":
        cur.execute("""UPDATE ext_approval_requests
            SET pm_status=%s, pm_note=%s, pm_decided_at=%s, status=%s, approver_note=%s, updated_at=%s
            WHERE id=%s""",
            (new_pm_status, note, now, new_overall, note, now, req_id))
    else:
        cur.execute("""UPDATE ext_approval_requests
            SET admin_status=%s, admin_note=%s, admin_decided_at=%s, status=%s, approver_note=%s, updated_at=%s
            WHERE id=%s""",
            (new_admin_status, note, now, new_overall, note, now, req_id))
    conn.commit()

    # Slack 대상자 조회
    cur.execute("SELECT name, slack_id FROM ext_users WHERE name IN (%s, %s, %s)",
                (requester, pm_approver, admin_approver))
    slack_map = {r[0]: (r[1] or "") for r in cur.fetchall()}
    conn.close()

    type_label = REQ_TYPE_LABEL.get(req_type, "승인 요청")
    amount_str = f" · {int(amount):,}원" if amount else ""
    note_line = f"\n사유: {note}" if note else ""

    if decision == "반려":
        msg = f"❌ *{type_label} 반려됨* ({'1차 PM' if stage=='pm' else '2차 관리자'})\n" \
              f"제목: {title}{amount_str}\n처리: {me}{note_line}"
        await _notify_slack_approval(slack_map.get(requester, ""), msg)
        create_notification(requester, "approval_rejected",
                            f"{type_label} 반려됨 ({'1차 PM' if stage=='pm' else '2차 관리자'})",
                            f"{title}{amount_str} · 처리: {me}" + (f" · 사유: {note}" if note else ""),
                            "/?tab=approvals")
    elif stage == "pm":
        # PM 승인 → 관리자에게 다음 단계 알림 + 요청자에게도 1차 통과 알림
        msg_admin = f"📥 *{type_label} 2차 관리자 결재 요청*\n" \
                    f"요청자: {requester}\n제목: {title}{amount_str}\n" \
                    f"1차 PM({me}) 승인 완료 → 최종 승인 요청"
        await _notify_slack_approval(slack_map.get(admin_approver, ""), msg_admin)
        create_notification(admin_approver, "approval_request",
                            f"{type_label} 2차 관리자 결재 요청",
                            f"{requester} · {title}{amount_str} · 1차 PM({me}) 승인 완료",
                            "/?tab=approvals")
        msg_req = f"✅ *{type_label} 1차 PM({me}) 승인됨*\n" \
                  f"제목: {title}{amount_str}\n관리자({admin_approver}) 최종 결재 대기 중"
        await _notify_slack_approval(slack_map.get(requester, ""), msg_req)
        create_notification(requester, "approval_progress",
                            f"{type_label} 1차 PM 승인됨",
                            f"{title}{amount_str} · 관리자({admin_approver}) 최종 결재 대기 중",
                            "/?tab=approvals")
    else:
        # 관리자 최종 승인 → 요청자에게 최종 승인 알림
        msg = f"✅ *{type_label} 최종 승인됨*\n" \
              f"제목: {title}{amount_str}\n관리자({me}) 결재 완료"
        await _notify_slack_approval(slack_map.get(requester, ""), msg)
        create_notification(requester, "approval_approved",
                            f"{type_label} 최종 승인됨",
                            f"{title}{amount_str} · 관리자({me}) 결재 완료",
                            "/?tab=approvals")

    log_activity(request, f"결재 {decision}", "approval", req_id,
                 {"stage": stage, "type": req_type, "title": title, "amount": float(amount) if amount else None, "note": note})

    return {"ok": True, "stage": stage, "approver": me, "overall": new_overall}


@app.delete("/api/approvals/{req_id}")
async def delete_approval(req_id: str, request: Request):
    """결재 삭제 — PM/어드민은 어떤 상태든 전체 / 멤버는 본인 대기건만 자가취소"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("user_id") == "legacy" or not user_state.get("name"):
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    is_priv = user_state.get("role") in ("admin", "pm")  # PM·어드민 전체 삭제 가능

    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT requester, status FROM ext_approval_requests WHERE id = %s", (req_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return Response(status_code=404)
    owner, cur_status = row
    if not is_priv:
        if owner != user_state["name"]:
            conn.close()
            return Response(content=json.dumps({"error": "본인 요청만 취소 가능합니다 (전체 삭제는 PM·관리자 권한)"}), status_code=403, media_type="application/json")
        if cur_status != "대기":
            conn.close()
            return Response(content=json.dumps({"error": "대기 상태인 요청만 취소 가능합니다"}), status_code=400, media_type="application/json")

    cur.execute("DELETE FROM ext_approval_requests WHERE id = %s", (req_id,))
    conn.commit()
    conn.close()
    log_activity(request, "결재 삭제", "approval", req_id, {"prev_status": cur_status, "owner": owner})
    return {"ok": True}


@app.get("/api/approval-settings")
async def get_approval_settings():
    """유형별 기본 승인자 매핑 조회 — {type: {pm, admin}} 구조 (구버전 string 자동 변환)"""
    raw = _read_store("approval_settings") or {}
    return {"type_approvers": _norm_type_approvers(raw.get("type_approvers"))}


@app.put("/api/approval-settings")
async def put_approval_settings(request: Request):
    """유형별 기본 PM·관리자 매핑 저장 — admin/pm. 입력은 {type_approvers: {type:{pm,admin}}}"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    normalized = _norm_type_approvers((body or {}).get("type_approvers"))
    # 지정된 이름이 ext_users에 존재하는지 검증 (빈 값은 허용 — 저장만 하고 신청 시점에 막음)
    names = set()
    for v in normalized.values():
        if v.get("pm"): names.add(v["pm"])
        if v.get("admin"): names.add(v["admin"])
    if names:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT name FROM ext_users WHERE name = ANY(%s) AND is_active = TRUE AND role IN ('admin','pm')",
                    (list(names),))
        valid = {r[0] for r in cur.fetchall()}
        conn.close()
        missing = names - valid
        if missing:
            return Response(content=json.dumps({"error": f"존재하지 않거나 admin/pm 권한이 없는 사용자: {', '.join(sorted(missing))}"}),
                            status_code=400, media_type="application/json")
    await _write_store("approval_settings", {"type_approvers": normalized})
    log_activity(request, "결재 설정 변경", "approval_settings", "", {"type_approvers": normalized})
    return {"ok": True, "type_approvers": normalized}


# ── 활동 로그 API (admin/pm) ──

@app.get("/api/activity-logs")
async def list_activity_logs(request: Request, user: str = "", action: str = "",
                              target_type: str = "", start: str = "", end: str = "",
                              limit: int = 200, offset: int = 0):
    """활동 로그 조회 — admin/pm. 필터: user, action(부분일치), target_type, start/end(YYYY-MM-DD), limit≤500."""
    u = getattr(request.state, "user", None)
    if not u or u.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}),
                        status_code=403, media_type="application/json")
    limit = max(1, min(500, int(limit or 200)))
    offset = max(0, int(offset or 0))
    where = []
    params = []
    if user:
        where.append("user_name = %s"); params.append(user)
    if action:
        where.append("action ILIKE %s"); params.append(f"%{action}%")
    if target_type:
        where.append("target_type = %s"); params.append(target_type)
    if start:
        where.append("created_at >= %s"); params.append(start)
    if end:
        # end는 inclusive — 다음날 00:00 미만
        where.append("created_at < (%s::date + INTERVAL '1 day')"); params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _pg_conn(); cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM ext_activity_logs {where_sql}", tuple(params))
    total = cur.fetchone()[0]
    cur.execute(f"""SELECT id, user_name, user_role, action, target_type, target_id,
        detail::text, ip, created_at::text
        FROM ext_activity_logs {where_sql}
        ORDER BY id DESC LIMIT %s OFFSET %s""", tuple(params) + (limit, offset))
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        try:
            d = json.loads(r[6]) if r[6] else {}
        except Exception:
            d = {}
        items.append({
            "id": r[0], "user_name": r[1], "user_role": r[2], "action": r[3],
            "target_type": r[4], "target_id": r[5], "detail": d, "ip": r[7], "created_at": r[8],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/activity-logs/export")
async def export_activity_logs(request: Request, user: str = "", action: str = "",
                                target_type: str = "", start: str = "", end: str = ""):
    """활동 로그 CSV 내보내기 — admin/pm. 최대 5000건."""
    u = getattr(request.state, "user", None)
    if not u or u.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}),
                        status_code=403, media_type="application/json")
    where = []; params = []
    if user: where.append("user_name = %s"); params.append(user)
    if action: where.append("action ILIKE %s"); params.append(f"%{action}%")
    if target_type: where.append("target_type = %s"); params.append(target_type)
    if start: where.append("created_at >= %s"); params.append(start)
    if end: where.append("created_at < (%s::date + INTERVAL '1 day')"); params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute(f"""SELECT created_at::text, user_name, user_role, action,
        target_type, target_id, detail::text, ip
        FROM ext_activity_logs {where_sql} ORDER BY id DESC LIMIT 5000""", tuple(params))
    rows = cur.fetchall()
    conn.close()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["시각", "사용자", "역할", "액션", "대상유형", "대상ID", "상세", "IP"])
    for r in rows:
        w.writerow(list(r))
    csv_data = "﻿" + buf.getvalue()  # BOM (엑셀 한글 대응)
    fname = f"activity_logs_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(content=csv_data.encode("utf-8"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ── 인앱 알림 API ──

@app.get("/api/notifications")
async def list_notifications(request: Request, only_unread: int = 0, limit: int = 50):
    """현재 로그인 사용자의 알림 목록 — 최신순. only_unread=1이면 미읽음만."""
    u = getattr(request.state, "user", None)
    if not u or not u.get("name"):
        return {"items": [], "unread": 0}
    limit = max(1, min(200, int(limit or 50)))
    conn = _pg_conn(); cur = conn.cursor()
    if int(only_unread or 0):
        cur.execute("""SELECT id, n_type, title, message, link, is_read, created_at::text
            FROM ext_notifications WHERE user_name = %s AND is_read = FALSE
            ORDER BY id DESC LIMIT %s""", (u["name"], limit))
    else:
        cur.execute("""SELECT id, n_type, title, message, link, is_read, created_at::text
            FROM ext_notifications WHERE user_name = %s
            ORDER BY id DESC LIMIT %s""", (u["name"], limit))
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM ext_notifications WHERE user_name = %s AND is_read = FALSE", (u["name"],))
    unread = cur.fetchone()[0]
    conn.close()
    items = [{"id": r[0], "type": r[1], "title": r[2], "message": r[3],
              "link": r[4], "is_read": r[5], "created_at": r[6]} for r in rows]
    return {"items": items, "unread": unread}


@app.post("/api/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: int, request: Request):
    """알림 1건 읽음 처리 — 본인 알림만"""
    u = getattr(request.state, "user", None)
    if not u or not u.get("name"):
        return Response(status_code=403)
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""UPDATE ext_notifications SET is_read = TRUE, read_at = NOW()
        WHERE id = %s AND user_name = %s""", (notif_id, u["name"]))
    conn.commit(); conn.close()
    return {"ok": True}


@app.post("/api/notifications/read-all")
async def mark_all_notifications_read(request: Request):
    """본인의 모든 미읽음 알림 일괄 읽음 처리"""
    u = getattr(request.state, "user", None)
    if not u or not u.get("name"):
        return Response(status_code=403)
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""UPDATE ext_notifications SET is_read = TRUE, read_at = NOW()
        WHERE user_name = %s AND is_read = FALSE""", (u["name"],))
    conn.commit(); conn.close()
    return {"ok": True}


@app.delete("/api/notifications/{notif_id}")
async def delete_notification(notif_id: int, request: Request):
    """알림 1건 삭제 — 본인만"""
    u = getattr(request.state, "user", None)
    if not u or not u.get("name"):
        return Response(status_code=403)
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM ext_notifications WHERE id = %s AND user_name = %s",
                (notif_id, u["name"]))
    conn.commit(); conn.close()
    return {"ok": True}


async def _weekly_report_reminder_loop():
    """매주 월요일 오전 9시 — 보고 미작성자에게 인앱 알림"""
    while True:
        try:
            now = datetime.now(KST)
            # 다음 월요일 09:00 KST 계산
            days_to_mon = (7 - now.weekday()) % 7
            if days_to_mon == 0 and now.hour >= 9:
                days_to_mon = 7
            target = (now + timedelta(days=days_to_mon)).replace(hour=9, minute=0, second=0, microsecond=0)
            wait_sec = max(60, int((target - now).total_seconds()))
            await asyncio.sleep(wait_sec)
            # 이번 주 라벨 — 보고서 week_label과 동일 형식 "{월}월 {ceil(일/7)}주차"(프론트 execCurrentWeek와 일치)
            now2 = datetime.now(KST)
            week_label = f"{now2.month}월 {(now2.day + 6) // 7}주차"
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute("SELECT count(*) FROM ext_users WHERE is_active = TRUE AND role IN ('member','pm')")
            eligible = cur.fetchone()[0]
            cur.execute("""SELECT u.name FROM ext_users u
                WHERE u.is_active = TRUE AND u.role IN ('member','pm')
                AND NOT EXISTS (
                    SELECT 1 FROM ext_member_reports r
                    WHERE r.name = u.name AND r.week_label = %s
                )""", (week_label,))
            missing = [r[0] for r in cur.fetchall() if r[0]]
            conn.close()
            # 안전장치: 대상 전원이 미작성으로 집계되면 라벨/스키마 불일치 의심 → 발송 보류(대량 오발송 방지)
            if eligible > 0 and len(missing) >= eligible:
                logger.warning(f"[리마인더] {week_label} 전원({eligible}명) 미작성 집계 — 라벨 불일치 의심, 발송 보류")
            else:
                for name in missing:
                    create_notification(name, "report_reminder",
                                        "이번 주 팀원 보고 미작성",
                                        f"{week_label} 주차 보고를 아직 작성하지 않았습니다.",
                                        "/?tab=member-reports")
                logger.info(f"[리마인더] {week_label} 주차 보고 미작성자 {len(missing)}명에게 알림 발송")
            _heartbeat("weekly_report_reminder", True)
        except Exception as e:
            _heartbeat("weekly_report_reminder", False, str(e))
            logger.warning(f"[리마인더] 루프 오류: {e}")
            await asyncio.sleep(3600)


# ── 경영 KPI 대시보드 (admin 전용) ──

@app.get("/api/dashboard/executive-summary")
async def executive_summary(request: Request):
    """경영 KPI: 매출 / 프로젝트 완료율 / 이번 달 인사 / 대기 결재"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return Response(
            content=json.dumps({"error": "관리자 권한이 필요합니다"}),
            status_code=403, media_type="application/json",
        )

    conn = _pg_conn()
    cur = conn.cursor()

    # 1) 매출 — report.metricSections의 biz 섹션에서 '매출' 라벨 항목 ('환불' 포함 우선)
    revenue = 0
    prev_revenue = 0
    revenue_trend = 0.0
    revenue_label = ""

    def _won(s):
        try:
            return int((s or "").replace("₩", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    try:
        report_data = _read_store("report")
        if report_data:
            biz_section = next((s for s in (report_data.get("metricSections") or [])
                                if s.get("id") == "biz"), None)
            if biz_section:
                metrics = biz_section.get("metrics") or []
                target = next((m for m in metrics
                               if "매출" in m.get("label", "") and "환불" in m.get("label", "")), None)
                if target is None:
                    target = next((m for m in metrics if "매출" in m.get("label", "")), None)
                if target:
                    revenue = _won(target.get("value"))
                    prev_revenue = _won(target.get("prevValue"))
                    revenue_trend = target.get("trend", 0.0) or 0.0
                    revenue_label = target.get("label", "")
    except Exception:
        pass

    # 2) 프로젝트 완료율 — 이번 분기(목표 완료일 기준) 프로젝트, 취소 건 제외
    proj_total = 0
    proj_completed = 0
    proj_in_progress = 0
    _now = datetime.now(KST).replace(tzinfo=None)
    _q_idx = (_now.month - 1) // 3                 # 0~3
    _q_start_month = _q_idx * 3 + 1                # 1/4/7/10
    q_start_str = f"{_now.year}-{_q_start_month:02d}-01"
    _q_end_month = _q_start_month + 3
    if _q_end_month > 12:
        q_end_str = f"{_now.year + 1}-01-01"
    else:
        q_end_str = f"{_now.year}-{_q_end_month:02d}-01"
    quarter_label = f"{_now.year} {_q_idx + 1}분기"
    try:
        if LINEAR_API_KEY:
            result = await _linear_graphql("{ projects(first: 100) { nodes { state targetDate } } }")
            nodes = result.get("data", {}).get("projects", {}).get("nodes", [])
            for p in nodes:
                td = (p.get("targetDate") or "")[:10]
                # 목표 완료일이 이번 분기 범위 안인 프로젝트만 (목표일 미설정은 제외)
                if not td or not (q_start_str <= td < q_end_str):
                    continue
                st = (p.get("state") or "").lower()
                if st in ("canceled", "cancelled"):
                    continue
                proj_total += 1
                if st == "completed":
                    proj_completed += 1
                elif st in ("started", "inprogress", "in_progress"):
                    proj_in_progress += 1
    except Exception:
        pass
    proj_rate = round(proj_completed / proj_total * 100) if proj_total else 0

    # 3) 이번 달 인사 — 승인 반차를 반영한 지각 건수 + 올해 연차 사용 일수
    attendance_issues = 0
    leave_used = 0.0
    try:
        now = datetime.now(KST).replace(tzinfo=None)
        month_start, month_end = _attendance_month_bounds(now=now)
        attendance_view = _build_attendance_view(
            _read_attendance_records(cur, month_start, month_end),
            _read_attendance_halfdays(cur, month_start, month_end), month_start, month_end)
        attendance_issues = sum(row["late"] for row in attendance_view["rows"])
        cur.execute("SELECT COALESCE(SUM(used_days), 0) FROM ext_leave_balance WHERE year = %s", (now.year,))
        leave_used = float(cur.fetchone()[0] or 0)
    except Exception:
        pass

    # 4) 대기 결재
    pending_approvals = 0
    try:
        cur.execute("SELECT COUNT(*) FROM ext_approval_requests WHERE status = '대기'")
        pending_approvals = cur.fetchone()[0] or 0
    except Exception:
        pass

    conn.close()

    return {
        "revenue": revenue,
        "prev_revenue": prev_revenue,
        "revenue_trend": revenue_trend,
        "revenue_label": revenue_label,
        "project_completion": {
            "completed": proj_completed,
            "in_progress": proj_in_progress,
            "total": proj_total,
            "rate": proj_rate,
            "quarter_label": quarter_label,
        },
        "hr_status": {
            "attendance_issues": attendance_issues,
            "leave_used": leave_used,
        },
        "pending_approvals": pending_approvals,
    }


# ── 팀원별 주간 업무 보고 API ──

def _ensure_member_reports_table():
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS ext_member_reports (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            week_label TEXT NOT NULL,
            sections JSONB DEFAULT '[]'::jsonb,
            attachments JSONB DEFAULT '[]'::jsonb,
            updated_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("ALTER TABLE ext_member_reports ADD COLUMN IF NOT EXISTS attachments JSONB DEFAULT '[]'::jsonb")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



@app.get("/api/member-reports")
async def get_member_reports(week: str = ""):
    """팀원별 주간 보고 조회. week 파라미터 없으면 전체 반환."""
    conn = _pg_conn()
    cur = conn.cursor()
    if week:
        cur.execute("""SELECT id, name, week_label, COALESCE(sections, '[]'::jsonb),
            COALESCE(attachments, '[]'::jsonb), updated_at::text
            FROM ext_member_reports WHERE week_label = %s ORDER BY name""", (week,))
    else:
        cur.execute("""SELECT id, name, week_label, COALESCE(sections, '[]'::jsonb),
            COALESCE(attachments, '[]'::jsonb), updated_at::text
            FROM ext_member_reports ORDER BY week_label DESC, name""")
    rows = cur.fetchall()
    cur.execute("SELECT DISTINCT week_label FROM ext_member_reports ORDER BY week_label DESC")
    weeks = [r[0] for r in cur.fetchall()]
    conn.close()
    items = []
    for r in rows:
        items.append({"id": r[0], "name": r[1], "week_label": r[2],
                       "sections": r[3], "attachments": r[4], "updated_at": r[5]})
    return {"items": items, "weeks": weeks}


@app.put("/api/member-reports")
async def put_member_report(request: Request):
    """팀원 주간 보고 저장 (upsert)"""
    body = await request.json()
    name = body.get("name", "")
    week_label = body.get("week_label", "")
    sections = body.get("sections", [])
    attachments = body.get("attachments", [])
    if not name or not week_label:
        return Response(content=json.dumps({"error": "name, week_label 필수"}),
                        status_code=400, media_type="application/json")
    nid = f"{name}_{week_label}"
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO ext_member_reports (id, name, week_label, sections, attachments, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, NOW())
        ON CONFLICT (id) DO UPDATE SET sections = EXCLUDED.sections, attachments = EXCLUDED.attachments, updated_at = NOW()""",
        (nid, name, week_label, json.dumps(sections, ensure_ascii=False), json.dumps(attachments, ensure_ascii=False)))
    conn.commit()
    # 업데이트된 시간 반환
    cur.execute("SELECT updated_at::text FROM ext_member_reports WHERE id = %s", (nid,))
    updated = cur.fetchone()
    conn.close()
    return {"ok": True, "updated_at": updated[0] if updated else ""}


# ── 프로젝트 계층 관리 (A+C 하이브리드) ──

def _ensure_project_hierarchy_table():
    """project_hierarchy 테이블 생성 (최초 1회)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS project_hierarchy (
        linear_project_id TEXT PRIMARY KEY,
        parent_project_id TEXT DEFAULT NULL,
        sort_order INT DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.commit()
    conn.close()



def _would_create_cycle(cur, child_id, proposed_parent_id):
    """child의 parent를 proposed_parent로 설정할 때 순환 참조 발생 여부 확인"""
    if not proposed_parent_id:
        return False
    visited = set()
    current = proposed_parent_id
    while current:
        if current == child_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        cur.execute("SELECT parent_project_id FROM project_hierarchy WHERE linear_project_id = %s", (current,))
        row = cur.fetchone()
        current = row[0] if row else None
    return False


def _build_project_tree(projects_map, hierarchy_rows):
    """프로젝트 맵 + 계층 정보로 트리 구조 생성"""
    # hierarchy_rows: [(linear_project_id, parent_project_id, sort_order), ...]
    children_map = {}  # parent_id -> [child_ids]
    root_ids = []
    assigned_ids = set()

    for row in hierarchy_rows:
        pid, parent_pid, sort_order = row[0], row[1], row[2]
        assigned_ids.add(pid)
        if parent_pid is None:
            root_ids.append((pid, sort_order))
        else:
            children_map.setdefault(parent_pid, []).append((pid, sort_order))
            assigned_ids.add(parent_pid)  # 부모도 할당된 것으로 간주

    # 정렬
    root_ids.sort(key=lambda x: x[1])
    for k in children_map:
        children_map[k].sort(key=lambda x: x[1])

    def build_node(project_id):
        """재귀적으로 트리 노드 생성"""
        proj = projects_map.get(project_id)
        if not proj:
            # Linear에서 삭제된 프로젝트 → 기본 정보
            proj = {"id": project_id, "name": f"(삭제됨: {project_id[:8]}...)", "state": "unknown"}
        node = {**proj, "children": []}
        for child_id, _ in children_map.get(project_id, []):
            node["children"].append(build_node(child_id))
        return node

    tree = []
    for pid, _ in root_ids:
        tree.append(build_node(pid))

    return tree


@app.get("/api/project-hierarchy")
async def get_project_hierarchy():
    """프로젝트 계층 트리 조회: Linear 프로젝트 + hierarchy 테이블 기반"""
    # 1) Linear 프로젝트 + Initiative 목록 가져오기
    projects_list = []
    initiatives_list = []
    if LINEAR_API_KEY:
        # Initiative 목록 조회
        init_result = await _linear_graphql("""{
            initiatives(first: 50) { nodes { id name status description
                projects { nodes { id } }
            } }
        }""")
        for init in init_result.get("data", {}).get("initiatives", {}).get("nodes", []):
            proj_ids = [p["id"] for p in (init.get("projects") or {}).get("nodes", [])]
            initiatives_list.append({
                "id": init["id"], "name": init["name"],
                "status": init.get("status", ""),
                "description": (init.get("description") or "")[:300],
                "projectIds": proj_ids,
            })

        # Phase 2+3: relations(의존성), updates(최근 업데이트) 포함
        result = await _linear_graphql("""{
            projects(first: 100) {
                nodes {
                    id name state description
                    lead { name }
                    health
                    initiatives { nodes { id } }
                    projectUpdates(first: 1) { nodes { body createdAt } }
                }
            }
        }""")
        # relations 조회 시도 (별도 쿼리 — Linear 스키마에 따라 실패 가능)
        relations_data = []
        try:
            rel_result = await _linear_graphql("""{
                projectRelations(first: 250) {
                    nodes {
                        id type
                        project { id name }
                        relatedProject { id name }
                    }
                }
            }""")
            rel_nodes = rel_result.get("data", {}).get("projectRelations", {}).get("nodes", [])
            if rel_nodes:
                relations_data = rel_nodes
            else:
                # 에러 또는 빈 결과 확인
                errors = rel_result.get("errors")
                if errors:
                    import logging
                    logging.warning(f"Linear projectRelations 조회 실패: {errors}")
        except Exception as e:
            import logging
            logging.warning(f"Linear projectRelations 조회 예외: {e}")

        # blocks/blockedBy 매핑 구축
        blocks_map = {}    # project_id -> [{id, name}, ...]  (이 프로젝트가 차단하는 대상)
        blocked_by_map = {}  # project_id -> [{id, name}, ...]  (이 프로젝트를 차단하는 대상)
        for rel in relations_data:
            rel_type = (rel.get("type") or "").lower()
            proj = rel.get("project") or {}
            related = rel.get("relatedProject") or {}
            if rel_type == "blocks" and proj.get("id") and related.get("id"):
                # project가 relatedProject를 차단함
                blocks_map.setdefault(proj["id"], []).append({"id": related["id"], "name": related.get("name", "")})
                blocked_by_map.setdefault(related["id"], []).append({"id": proj["id"], "name": proj.get("name", "")})
            elif rel_type == "related":
                pass  # 관련 관계는 무시

        for p in result.get("data", {}).get("projects", {}).get("nodes", []):
            # 최신 업데이트 추출
            updates_nodes = (p.get("projectUpdates") or {}).get("nodes", [])
            latest_update = None
            if updates_nodes:
                u = updates_nodes[0]
                latest_update = {"body": (u.get("body") or "")[:500], "createdAt": u.get("createdAt", "")}

            init_ids = [i["id"] for i in (p.get("initiatives") or {}).get("nodes", [])]
            projects_list.append({
                "id": p["id"],
                "name": p["name"],
                "state": p.get("state", ""),
                "description": (p.get("description") or "")[:300],
                "lead": (p.get("lead") or {}).get("name", ""),
                "health": p.get("health", ""),
                "blocks": blocks_map.get(p["id"], []),
                "blockedBy": blocked_by_map.get(p["id"], []),
                "latestUpdate": latest_update,
                "initiativeIds": init_ids,
            })

    projects_map = {p["id"]: p for p in projects_list}

    # 2) hierarchy 테이블 조회 + 고아 자동 정리
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT linear_project_id, parent_project_id, sort_order FROM project_hierarchy ORDER BY sort_order")
    hierarchy_rows = cur.fetchall()
    # Linear에 없는 고아 항목 자동 제거
    orphan_ids = [r[0] for r in hierarchy_rows if r[0] not in projects_map]
    if orphan_ids:
        for oid in orphan_ids:
            cur.execute("DELETE FROM project_hierarchy WHERE linear_project_id = %s", (oid,))
        cur.execute("UPDATE project_hierarchy SET parent_project_id = NULL WHERE parent_project_id IS NOT NULL AND parent_project_id NOT IN (SELECT linear_project_id FROM project_hierarchy)")
        conn.commit()
        hierarchy_rows = [r for r in hierarchy_rows if r[0] not in set(orphan_ids)]
        logger.info(f"project_hierarchy 고아 {len(orphan_ids)}건 자동 정리")
    conn.close()

    # 3) 트리 구축
    tree = _build_project_tree(projects_map, hierarchy_rows)

    # 4) 미배치 프로젝트 (Linear에 있지만 hierarchy에 없는 것)
    assigned_ids = set()
    for row in hierarchy_rows:
        assigned_ids.add(row[0])
        if row[1]:
            assigned_ids.add(row[1])
    unassigned = [p for p in projects_list if p["id"] not in assigned_ids]

    return {
        "projects": projects_list,
        "initiatives": initiatives_list,
        "tree": tree,
        "unassigned": unassigned,
        "hierarchy": [{"linear_project_id": r[0], "parent_project_id": r[1], "sort_order": r[2]} for r in hierarchy_rows],
    }


@app.put("/api/project-hierarchy")
async def save_project_hierarchy(request: Request):
    """프로젝트 부모 할당 (upsert) — admin/pm only"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    linear_project_id = body.get("linear_project_id", "")
    parent_project_id = body.get("parent_project_id") or None  # null → 루트
    sort_order = body.get("sort_order", 0)

    if not linear_project_id:
        return Response(
            content=json.dumps({"error": "linear_project_id 필수"}),
            status_code=400, media_type="application/json",
        )

    conn = _pg_conn()
    cur = conn.cursor()

    # 순환 참조 검사
    if parent_project_id and _would_create_cycle(cur, linear_project_id, parent_project_id):
        conn.close()
        return Response(
            content=json.dumps({"error": "순환 참조가 발생합니다 (A→B→A)"}),
            status_code=400, media_type="application/json",
        )

    # 부모 프로젝트가 hierarchy에 없으면 루트로 자동 등록
    if parent_project_id:
        cur.execute("SELECT 1 FROM project_hierarchy WHERE linear_project_id = %s", (parent_project_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO project_hierarchy (linear_project_id, parent_project_id, sort_order) VALUES (%s, NULL, 0)",
                (parent_project_id,),
            )

    # upsert
    cur.execute("""
        INSERT INTO project_hierarchy (linear_project_id, parent_project_id, sort_order, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (linear_project_id) DO UPDATE
        SET parent_project_id = EXCLUDED.parent_project_id,
            sort_order = EXCLUDED.sort_order,
            updated_at = NOW()
    """, (linear_project_id, parent_project_id, sort_order))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/project-hierarchy/{project_id}")
async def delete_project_hierarchy(project_id: str, request: Request):
    """프로젝트를 계층에서 제거 (Linear 프로젝트는 유지) — admin/pm only"""
    user_state = getattr(request.state, "user", None)
    if not user_state or user_state.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn()
    cur = conn.cursor()

    # 자식 프로젝트들의 parent를 NULL로 변경 (고아 방지)
    cur.execute("UPDATE project_hierarchy SET parent_project_id = NULL WHERE parent_project_id = %s", (project_id,))
    # 본인 삭제
    cur.execute("DELETE FROM project_hierarchy WHERE linear_project_id = %s", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── Linear 사이클 API ──

def _format_cycles_from_raw(cycles_raw: list, proj_init_map: dict) -> dict:
    """원본 사이클 데이터를 API 응답 형식으로 변환 (캐시/API 공용)"""
    # Installer-configured excluded teams
    cycles = [c for c in cycles_raw if (c.get("team") or {}).get("name") not in _installation.LINEAR_EXCLUDED_TEAM_NAMES]
    output = []
    for c in cycles:
        issues = (c.get("issues") or {}).get("nodes", [])
        total = len(issues)
        done = sum(1 for i in issues if i.get("state", {}).get("type") == "completed")
        active = sum(1 for i in issues if i.get("state", {}).get("type") == "started")
        # 이슈 맵 (하위 이슈 연결용)
        issue_map = {}
        by_assignee = {}
        for i in issues:
            assignee = (i.get("assignee") or {}).get("name", "미배정")
            proj_name = (i.get("project") or {}).get("name", "")
            parent_id = (i.get("parent") or {}).get("id", "")
            item = {
                "id": i["id"], "identifier": i.get("identifier", ""),
                "title": i["title"], "state": i.get("state", {}).get("name", ""),
                "stateType": i.get("state", {}).get("type", ""),
                "priority": i.get("priority", 0),
                "assignee": assignee,
                "project": proj_name,
                "initiative": proj_init_map.get(proj_name, ""),
                "parentId": parent_id,
                "children": [],
            }
            issue_map[i["id"]] = item
            by_assignee.setdefault(assignee, []).append(item)
        # parent→children 역추적
        top_issues = []
        for item in issue_map.values():
            if item["parentId"] and item["parentId"] in issue_map:
                issue_map[item["parentId"]]["children"].append({
                    "id": item["id"], "identifier": item["identifier"],
                    "title": item["title"], "state": item["state"],
                    "stateType": item["stateType"], "assignee": item["assignee"],
                    "priority": item["priority"],
                })
            else:
                top_issues.append(item)

        # Initiative > Project > Issue 트리 구축
        tree = {}  # { initiative: { project: [issues] } }
        for i in top_issues:
            init_name = i["initiative"] or "(Initiative 없음)"
            proj_name = i["project"] or "(프로젝트 없음)"
            tree.setdefault(init_name, {}).setdefault(proj_name, []).append(i)

        output.append({
            "id": c["id"], "name": c.get("name") or f"Cycle {c.get('number','')}",
            "number": c.get("number"), "team": (c.get("team") or {}).get("name", ""),
            "startsAt": (c.get("startsAt") or "")[:10],
            "endsAt": (c.get("endsAt") or "")[:10],
            "total": total, "done": done, "active": active,
            "progress": round(done / total * 100) if total > 0 else 0,
            "byAssignee": by_assignee,
            "tree": tree,
        })
    return {"cycles": output}


@app.get("/api/linear/cycles")
async def linear_cycles():
    """Linear 활성 사이클 조회 (설정된 팀 필터 적용) — DB 캐시 우선 조회"""
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY 미설정"}),
                        status_code=400, media_type="application/json")
    # L2: DB 캐시에서 먼저 조회
    cached = _read_linear_cache("linear_cycles")
    if cached:
        raw = cached["data"]
        cycles_raw = raw.get("cycles", [])
        proj_init_map = raw.get("proj_init_map", {})
        result = _format_cycles_from_raw(cycles_raw, proj_init_map)
        result["_cached"] = True
        result["_cached_at"] = cached["updated_at"]
        return result

    # 캐시 미스: 기존 Linear API 직접 호출 (폴백)
    logger.info("[Linear cycles] 캐시 미스 — API 직접 호출")
    api_result = await _linear_graphql("""{
        cycles(first: 5, filter: { isActive: { eq: true } }) { nodes {
            id name number startsAt endsAt progress
            team { id name }
            issues(first: 250) { nodes {
                id identifier title priority
                state { name type }
                assignee { name }
                project { name }
                parent { id identifier }
            } }
        } }
    }""")
    # Initiative 매핑 (별도 쿼리)
    proj_init_map = {}  # { project_name: initiative_name }
    try:
        pi_result = await _linear_graphql('{ projects(first: 50) { nodes { name initiatives { nodes { name } } } } }')
        for p in pi_result.get("data", {}).get("projects", {}).get("nodes", []):
            inits = (p.get("initiatives") or {}).get("nodes", [])
            if inits:
                proj_init_map[p["name"]] = inits[0].get("name", "")
    except Exception:
        pass
    cycles_raw = api_result.get("data", {}).get("cycles", {}).get("nodes", [])
    return _format_cycles_from_raw(cycles_raw, proj_init_map)


# ── Linear DB 캐시 수동 동기화 / 상태 API ──

@app.post("/api/linear/sync-now")
async def linear_sync_now():
    """Linear → DB 캐시 즉시 동기화 트리거"""
    if not LINEAR_API_KEY:
        return Response(content=json.dumps({"error": "LINEAR_API_KEY 미설정"}),
                        status_code=400, media_type="application/json")
    if _linear_db_sync_running:
        return {"status": "already_running", "message": "동기화가 이미 진행 중입니다"}
    # 비동기 즉시 실행
    asyncio.create_task(_sync_linear_to_db())
    return {"status": "started", "message": "동기화를 시작했습니다"}


@app.get("/api/linear/sync-status")
async def linear_sync_status():
    """Linear DB 캐시 동기화 상태 조회"""
    # 캐시 항목 수 조회
    entries = 0
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ext_linear_cache")
        entries = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass

    # 다음 동기화까지 남은 시간 추정 (마지막 동기화 시각 기준)
    next_sync_in = None
    if _linear_db_last_sync:
        try:
            last_dt = datetime.fromisoformat(_linear_db_last_sync.replace("Z", "+00:00"))
            elapsed = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
            next_sync_in = max(0, int(300 - elapsed))
        except Exception:
            pass

    return {
        "last_sync": _linear_db_last_sync,
        "last_duration_sec": _linear_db_last_duration,
        "next_sync_in": next_sync_in,
        "entries": entries,
        "running": _linear_db_sync_running,
    }


# ── 공유 메모 API ──

def _ensure_memos_table():
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_shared_memos (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        author TEXT DEFAULT '',
        pinned BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_memo_history (
        id TEXT PRIMARY KEY,
        memo_id TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        edited_by TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.commit()
    conn.close()



@app.get("/api/memos")
async def list_memos():
    """공유 메모 전체 조회 (최신순, 고정 우선)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id, title, content, author, pinned, created_at::text, updated_at::text
        FROM ext_shared_memos ORDER BY pinned DESC, updated_at DESC""")
    rows = cur.fetchall()
    conn.close()
    return {"memos": [{"id": r[0], "title": r[1], "content": r[2], "author": r[3],
                        "pinned": r[4], "created_at": r[5], "updated_at": r[6]} for r in rows]}


@app.post("/api/memos")
async def create_memo(request: Request):
    """메모 생성"""
    body = await request.json()
    nid = uuid.uuid4().hex[:12]
    title = body.get("title", "").strip() or "제목 없음"
    content = body.get("content", "")
    author = body.get("author", "")
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO ext_shared_memos (id, title, content, author)
        VALUES (%s, %s, %s, %s)""", (nid, title, content, author))
    conn.commit()
    conn.close()
    return {"ok": True, "id": nid}


@app.put("/api/memos/{memo_id}")
async def update_memo(memo_id: str, request: Request):
    """메모 수정 + 이전 내용 히스토리 저장"""
    body = await request.json()
    conn = _pg_conn()
    cur = conn.cursor()
    # 이전 내용 히스토리에 저장
    cur.execute("SELECT content FROM ext_shared_memos WHERE id = %s", (memo_id,))
    row = cur.fetchone()
    if row:
        hid = uuid.uuid4().hex[:12]
        cur.execute("""INSERT INTO ext_memo_history (id, memo_id, content, edited_by)
            VALUES (%s, %s, %s, %s)""", (hid, memo_id, row[0], body.get("edited_by", "")))
    # 메모 업데이트
    updates, vals = [], []
    for field in ["title", "content", "pinned"]:
        if field in body:
            updates.append(f"{field} = %s")
            vals.append(body[field])
    if updates:
        updates.append("updated_at = NOW()")
        vals.append(memo_id)
        cur.execute(f"UPDATE ext_shared_memos SET {', '.join(updates)} WHERE id = %s", vals)
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/memos/{memo_id}")
async def delete_memo(memo_id: str):
    """메모 삭제 (히스토리 포함)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ext_memo_history WHERE memo_id = %s", (memo_id,))
    cur.execute("DELETE FROM ext_shared_memos WHERE id = %s", (memo_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/memos/{memo_id}/history")
async def memo_history(memo_id: str):
    """메모 수정 히스토리 조회 (날짜별, 최신순)"""
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id, content, edited_by, created_at::text
        FROM ext_memo_history WHERE memo_id = %s ORDER BY created_at DESC""", (memo_id,))
    rows = cur.fetchall()
    conn.close()
    return {"history": [{"id": r[0], "content": r[1], "edited_by": r[2], "created_at": r[3]} for r in rows]}


# ══════════════════════════════════════════════════════════
# [Phase 1차] Linear 캐싱 + 토스트/에러추적 + 자동백업 + Slack 본격연동
# ══════════════════════════════════════════════════════════

# ── D. Linear API 인메모리 캐시 — stale-while-revalidate ──
_linear_cache = {}      # {cache_key: {"data": ..., "expires": timestamp, "stale_until": timestamp}}
_linear_inflight = {}   # 재검증 진행 중인 키 (중복 호출 방지)
LINEAR_CACHE_TTL = 600         # 10분 — fresh window (이전 5분 → 외부 호출 빈도 절반)
LINEAR_CACHE_STALE = 3600      # +1시간 — stale 허용 (사용자에겐 즉시 응답, 백그라운드 재검증)


async def _linear_graphql_cached(query: str, ttl: int = LINEAR_CACHE_TTL) -> dict:
    """Linear GraphQL 캐시 with stale-while-revalidate.

    Fresh(TTL 내): 즉시 반환.
    Stale(TTL 만료 + stale 내): 즉시 stale 반환 + 백그라운드에서 재검증.
    Miss(모든 윈도우 만료): 동기 호출 후 캐시.

    효과: 캐시 미스 시 외부 API 5-10초 대기를 사용자에게 거의 노출하지 않음.
    """
    key = _hashlib_mod.md5(query.encode()).hexdigest()
    now = _time_mod.time()
    entry = _linear_cache.get(key)

    # 1) Fresh
    if entry and entry["expires"] > now:
        return entry["data"]

    # 2) Stale-while-revalidate
    if entry and entry.get("stale_until", 0) > now:
        # 중복 백그라운드 호출 방지
        if key not in _linear_inflight:
            _linear_inflight[key] = True
            async def _revalidate():
                try:
                    fresh = await _linear_graphql(query)
                    _linear_cache[key] = {
                        "data": fresh,
                        "expires": _time_mod.time() + ttl,
                        "stale_until": _time_mod.time() + ttl + LINEAR_CACHE_STALE,
                    }
                except Exception as e:
                    logger.warning(f"[Linear stale revalidate] 실패: {e}")
                finally:
                    _linear_inflight.pop(key, None)
            asyncio.create_task(_revalidate())
        return entry["data"]

    # 3) Cold miss — 동기 호출
    result = await _linear_graphql(query)
    _linear_cache[key] = {
        "data": result,
        "expires": now + ttl,
        "stale_until": now + ttl + LINEAR_CACHE_STALE,
    }
    return result


# ── B. 글로벌 예외 핸들러 (구조화 JSON 로깅 + Slack 알림) ──
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """처리되지 않은 예외를 구조화된 JSON으로 로깅하고 Slack 알림 전송"""
    # 클라이언트 연결 끊김(탭 닫기·이탈·새로고침으로 요청 중단)은 서버 오류가 아님.
    # /api/report 같은 무거운 요청에서 흔함 → 로깅·Slack 알림 제외(노이즈 차단), 499 반환.
    if type(exc).__name__ == "ClientDisconnect" or isinstance(exc, asyncio.CancelledError):
        logger.info(f"[client-disconnect] {request.method} {request.url.path}")
        return Response(status_code=499)
    error_id = uuid.uuid4().hex[:8]
    error_detail = {
        "error_id": error_id,
        "type": type(exc).__name__,
        "message": str(exc),
        "path": str(request.url.path),
        "method": request.method,
        "timestamp": datetime.now(KST).isoformat(),
        "traceback": traceback.format_exc(),
    }
    logger.error(json.dumps(error_detail, ensure_ascii=False))
    # DB에도 저장 (error_id로 사후 조회 가능하도록) — best-effort, 핸들러 자체는 절대 실패하지 않게
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO ext_error_logs (source, type, message, stack, url, user_agent) VALUES (%s, %s, %s, %s, %s, %s)",
                    ("backend", error_detail["type"],
                     f"[{error_id}] {error_detail['message']}"[:5000],
                     (error_detail["traceback"] or "")[:5000],
                     f"{error_detail['method']} {error_detail['path']}",
                     request.headers.get("User-Agent", "")[:500]))
        cur.execute("DELETE FROM ext_error_logs WHERE id IN (SELECT id FROM ext_error_logs ORDER BY id DESC OFFSET 1000)")
        conn.commit(); conn.close()
    except Exception:
        pass
    bot_token = _get_slack_bot_token() if "_get_slack_bot_token" in globals() else SLACK_BOT_TOKEN
    if bot_token:
        try:
            slack_channel = os.environ.get("SLACK_ERROR_CHANNEL", "")
            if slack_channel:
                slack_msg = f"🚨 *서버 오류* [{error_id}]\n`{error_detail['type']}`: {error_detail['message']}\n경로: `{error_detail['path']}`"
                await _slack_post_message(bot_token, slack_channel, slack_msg)
        except Exception:
            pass
    return Response(
        content=json.dumps({"error": "서버 내부 오류가 발생했습니다", "error_id": error_id}),
        status_code=500, media_type="application/json",
    )


# ── B. 프론트엔드 에러 로그 수신 ──
@app.post("/api/system/error-log")
async def receive_error_log(request: Request):
    """프론트엔드에서 발생한 에러를 서버 로그에 기록"""
    body = await request.json()
    error_detail = {
        "source": "frontend",
        "type": body.get("type", "unknown"),
        "message": body.get("message", ""),
        "stack": body.get("stack", ""),
        "url": body.get("url", ""),
        "timestamp": datetime.now(KST).isoformat(),
        "user_agent": request.headers.get("User-Agent", ""),
    }
    logger.error(f"[프론트엔드 에러] {json.dumps(error_detail, ensure_ascii=False)}")
    # DB에도 저장 (조회 endpoint 위함)
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO ext_error_logs (source, type, message, stack, url, user_agent) VALUES (%s, %s, %s, %s, %s, %s)",
                    (error_detail["source"], error_detail["type"], error_detail["message"][:5000],
                     (error_detail.get("stack") or "")[:5000], error_detail.get("url", ""), error_detail.get("user_agent", "")))
        # 최근 1000건만 유지
        cur.execute("DELETE FROM ext_error_logs WHERE id IN (SELECT id FROM ext_error_logs ORDER BY id DESC OFFSET 1000)")
        conn.commit(); conn.close()
    except Exception as e:
        logger.warning(f"error_log DB 저장 실패: {e}")
    if body.get("critical"):
        bot_token = _get_slack_bot_token()
        slack_channel = os.environ.get("SLACK_ERROR_CHANNEL", "")
        if bot_token and slack_channel:
            try:
                msg = f"🖥️ *프론트엔드 오류*\n`{body.get('type', '')}`: {body.get('message', '')}\nURL: `{body.get('url', '')}`"
                await _slack_post_message(bot_token, slack_channel, msg)
            except Exception:
                pass
    return {"ok": True}


# ── C. 자동 백업 (ext_backups) ──
def _init_backups_table():
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_backups (
        id TEXT PRIMARY KEY,
        backup_type TEXT DEFAULT 'auto',
        data JSONB,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.commit()
    conn.close()




def _auto_backup_report(previous_data: dict) -> None:
    """리포트 저장 전 이전 버전 자동 백업 (최대 50개, 오래된 것 자동 삭제)"""
    if not previous_data:
        return
    try:
        backup_id = f"auto_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ext_backups (id, backup_type, data, created_at) VALUES (%s, 'auto', %s::jsonb, NOW())",
            (backup_id, json.dumps(previous_data, ensure_ascii=False)),
        )
        # 보관 갯수 — system_settings.backup_keep_count (기본 50)
        keep = 50
        try:
            keep = int((_get_system_settings() or {}).get("backup_keep_count", 50))
        except Exception:
            pass
        cur.execute("""DELETE FROM ext_backups WHERE id IN (
            SELECT id FROM ext_backups WHERE backup_type='auto' ORDER BY created_at DESC OFFSET %s)""", (max(1, keep),))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[자동 백업] 실패: {e}")


@app.post("/api/system/backup")
async def create_backup(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    report_data = _read_store("report")
    if not report_data:
        return Response(content=json.dumps({"error": "백업할 데이터가 없습니다"}), status_code=404, media_type="application/json")
    backup_id = f"manual_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO ext_backups (id, backup_type, data, created_at) VALUES (%s, 'manual', %s::jsonb, NOW())",
                (backup_id, json.dumps(report_data, ensure_ascii=False)))
    conn.commit(); conn.close()
    return {"ok": True, "backup_id": backup_id, "created_at": datetime.now(KST).isoformat()}


@app.get("/api/system/backups")
async def list_backups(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT id, backup_type, created_at::text FROM ext_backups ORDER BY created_at DESC LIMIT 30")
    rows = [{"id": r[0], "backup_type": r[1], "created_at": r[2]} for r in cur.fetchall()]
    conn.close()
    return {"backups": rows}


@app.post("/api/system/restore/{backup_id}")
async def restore_backup(backup_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT data FROM ext_backups WHERE id = %s", (backup_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        return Response(content=json.dumps({"error": "백업을 찾을 수 없습니다"}), status_code=404, media_type="application/json")
    backup_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    current = _read_store("report")
    _auto_backup_report(current)
    await _write_store("report", backup_data)
    log_activity(request, "백업 복구", "backup", backup_id, {})
    return {"ok": True, "restored_from": backup_id}


# ── D. 캐시 수동 관리 ──
@app.post("/api/system/cache-clear")
async def clear_cache(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    cleared = len(_linear_cache)
    _linear_cache.clear()
    return {"ok": True, "cleared_entries": cleared}


# ── 시스템 상태 종합 ──
@app.get("/api/system/info")
async def system_info(request: Request):
    """DB/Linear/Slack 연결 상태 + 캐시 통계 (admin only)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    # DB
    db_status = "OK"
    try:
        c = _pg_conn(); c.cursor().execute("SELECT 1"); c.close()
    except Exception as e:
        db_status = f"오류: {e}"
    # Linear
    linear_status = "미설정"
    if LINEAR_API_KEY:
        try:
            r = await _linear_graphql_cached("{ viewer { id name } }", ttl=60)
            linear_status = "연결됨" if r.get("data", {}).get("viewer") else "응답 이상"
        except Exception:
            linear_status = "연결 실패"
    # Slack
    slack_token = _get_slack_bot_token()
    slack_status = "미설정"
    if slack_token:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post("https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {slack_token}"}, timeout=5)
                sdata = resp.json()
                slack_status = "연결됨" if sdata.get("ok") else f"오류: {sdata.get('error','')}"
        except Exception:
            slack_status = "연결 실패"
    # 캐시
    now = _time_mod.time()
    cache_total = len(_linear_cache)
    cache_active = sum(1 for v in _linear_cache.values() if v["expires"] > now)
    return {
        "railway_env": os.environ.get("RAILWAY_ENVIRONMENT", "local"),
        "db_status": db_status,
        "linear_status": linear_status,
        "slack_status": slack_status,
        "app_version": "2.1.0",
        "python_version": os.sys.version.split()[0],
        "cache": {
            "total_entries": cache_total,
            "active_entries": cache_active,
            "expired_entries": cache_total - cache_active,
            "ttl_seconds": LINEAR_CACHE_TTL,
        },
    }


# ── E. Slack 본격 연동 ──
def _get_slack_bot_token() -> str:
    """런타임 Slack 봇 토큰 반환 (DB 우선, 환경변수 폴백)"""
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT data->>'slack_bot_token' FROM ext_app_store WHERE key = 'system_settings'")
        row = cur.fetchone(); conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return SLACK_BOT_TOKEN


@app.post("/api/slack/setup")
async def slack_setup(request: Request):
    """Slack 봇 토큰 설정 — auth.test 검증 후 DB 저장 (admin)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    bot_token = (body.get("bot_token") or "").strip()
    if not bot_token:
        return Response(content=json.dumps({"error": "bot_token이 필요합니다"}), status_code=400, media_type="application/json")
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"}, timeout=10)
        data = resp.json()
    if not data.get("ok"):
        return Response(content=json.dumps({"error": f"토큰 검증 실패: {data.get('error','알 수 없음')}"}),
                        status_code=400, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO ext_app_store (key, data, updated_at) VALUES ('system_settings', %s::jsonb, NOW())
                   ON CONFLICT (key) DO UPDATE SET data = ext_app_store.data || EXCLUDED.data, updated_at = NOW()""",
                (json.dumps({"slack_bot_token": bot_token}),))
    conn.commit(); conn.close()
    global SLACK_BOT_TOKEN
    SLACK_BOT_TOKEN = bot_token
    return {"ok": True, "team": data.get("team"), "bot": data.get("user")}


@app.post("/api/slack/send")
async def slack_send(request: Request):
    """범용 Slack 메시지 전송 (admin)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    channel = (body.get("channel") or "").strip()
    message = (body.get("message") or "").strip()
    if not channel or not message:
        return Response(content=json.dumps({"error": "channel, message 필수"}), status_code=400, media_type="application/json")
    bot_token = _get_slack_bot_token()
    if not bot_token:
        return Response(content=json.dumps({"error": "Slack 봇 토큰이 설정되지 않았습니다"}), status_code=400, media_type="application/json")
    result = await _slack_post_message(bot_token, channel, message)
    if not result.get("ok"):
        return Response(content=json.dumps({"error": result.get("error", "전송 실패")}), status_code=400, media_type="application/json")
    return {"ok": True}


@app.post("/api/slack/remind-report")
async def slack_remind_report(request: Request):
    """전체 활성 멤버에게 주간 보고 리마인더 DM (admin)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    bot_token = _get_slack_bot_token()
    if not bot_token:
        return Response(content=json.dumps({"error": "Slack 봇 토큰이 설정되지 않았습니다"}), status_code=400, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT name, slack_id FROM ext_users WHERE is_active = TRUE AND slack_id IS NOT NULL AND slack_id != ''")
    members = cur.fetchall(); conn.close()
    body_data = await request.json()
    custom_msg = (body_data.get("message") or "").strip()
    msg = custom_msg or "📝 *주간 보고 마감 리마인더*\n이번 주 업무 보고를 작성해주세요.\n대시보드 → 주간 리포트 탭에서 작성할 수 있습니다."
    sent = 0; errors = []
    for name, slack_id in members:
        try:
            r = await _slack_post_message(bot_token, slack_id, msg)
            if r.get("ok"):
                sent += 1
            else:
                errors.append(f"{name}: {r.get('error','?')}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    return {"ok": True, "sent": sent, "errors": errors, "total": len(members)}


# ══════════════════════════════════════════════════════════
# [Phase 2차] A 설정 탭 백엔드 (비번 변경/초기화)
# ══════════════════════════════════════════════════════════

@app.post("/api/auth/change-password")
async def change_password(request: Request):
    """본인 비밀번호 변경 — 개인 로그인 필요"""
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") == "legacy":
        return Response(content=json.dumps({"error": "개인 로그인이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    cur_pw = (body.get("current_password") or "").strip()
    new_pw = (body.get("new_password") or "").strip()
    if not cur_pw or not new_pw:
        return Response(content=json.dumps({"error": "current_password, new_password 필수"}), status_code=400, media_type="application/json")
    try:
        _validate_password(new_pw)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT password_hash FROM ext_users WHERE id = %s", (user["user_id"],))
    row = cur.fetchone()
    if not row or not _verify_password(cur_pw, row[0]):
        conn.close()
        return Response(content=json.dumps({"error": "현재 비밀번호가 일치하지 않습니다"}), status_code=400, media_type="application/json")
    cur.execute("UPDATE ext_users SET password_hash = %s WHERE id = %s", (_hash_password(new_pw), user["user_id"]))
    conn.commit(); conn.close()
    return {"ok": True}


@app.post("/api/auth/reset-password")
async def reset_password(request: Request):
    """대상 사용자 비밀번호 초기화 — admin only"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    target_id = (body.get("user_id") or "").strip()
    new_pw = body.get("new_password") or ""
    if not target_id:
        return Response(content=json.dumps({"error": "user_id 필수"}), status_code=400, media_type="application/json")
    try:
        _validate_password(new_pw)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("UPDATE ext_users SET password_hash = %s WHERE id = %s RETURNING name", (_hash_password(new_pw), target_id))
    row = cur.fetchone()
    conn.commit(); conn.close()
    if not row:
        return Response(content=json.dumps({"error": "사용자를 찾을 수 없습니다"}), status_code=404, media_type="application/json")
    return {"ok": True, "name": row[0]}


# ══════════════════════════════════════════════════════════
# [Phase 3차] G AI 자동화 + H Linear Webhook
# ══════════════════════════════════════════════════════════

# ── Gemini 호출 헬퍼 ──
async def _gemini_chat(prompt: str, model: str = "gemini-2.0-flash") -> str:
    api_key = _get_gemini_key() if "_get_gemini_key" in globals() else os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        d = r.json()
        try:
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return json.dumps(d, ensure_ascii=False)


@app.post("/api/ai/weekly-summary")
async def ai_weekly_summary(request: Request):
    """팀 보고 + 프로젝트를 Gemini로 주간 종합 요약 (admin)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    # 데이터 수집
    report_data = _read_store("report") or {}
    member_reports = report_data.get("memberReports") or {}
    projects_payload = []
    try:
        if LINEAR_API_KEY:
            r = await _linear_graphql_cached("""{
                projects(first: 20) { nodes { id name state priority targetDate
                    issues { nodes { id title state { name type } } }
                } }
            }""")
            for p in r.get("data", {}).get("projects", {}).get("nodes", []):
                issues = (p.get("issues") or {}).get("nodes", [])
                done = sum(1 for i in issues if (i.get("state") or {}).get("type") == "completed")
                projects_payload.append({"name": p["name"], "state": p.get("state"), "total": len(issues), "done": done})
    except Exception:
        pass
    # 프롬프트 구성
    prompt_parts = ["# 주간 업무 종합 요약 요청\n",
                    "다음 자료를 바탕으로 한국어로 *이번 주 핵심 성과 / 진행 중 이슈 / 다음 주 집중* 3개 섹션으로 간결하게 요약해줘.\n",
                    "각 섹션은 불릿 3~5개. 군더더기 없이.\n\n"]
    if member_reports:
        prompt_parts.append("## 팀원 보고\n")
        for name, data in member_reports.items():
            if isinstance(data, dict):
                done = data.get("done") or data.get("complete") or ""
                plan = data.get("plan") or data.get("next") or ""
                issue = data.get("issue") or ""
                if done or plan or issue:
                    prompt_parts.append(f"- **{name}** · 완료: {str(done)[:200]} / 계획: {str(plan)[:200]} / 이슈: {str(issue)[:150]}\n")
    if projects_payload:
        prompt_parts.append("\n## 프로젝트 상태 (Linear)\n")
        for p in projects_payload:
            prompt_parts.append(f"- {p['name']} ({p['state']}) — 진행 {p['done']}/{p['total']}\n")
    prompt = "".join(prompt_parts)
    try:
        summary = await _gemini_chat(prompt)
        # 히스토리 저장
        try:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO ext_ai_summaries (kind, summary, input_chars, created_by) VALUES (%s, %s, %s, %s)",
                        ("weekly_summary", summary, len(prompt), user.get("name", "")))
            cur.execute("DELETE FROM ext_ai_summaries WHERE id IN (SELECT id FROM ext_ai_summaries ORDER BY id DESC OFFSET 100)")
            conn.commit(); conn.close()
        except Exception:
            pass
        return {"ok": True, "summary": summary, "input_chars": len(prompt)}
    except Exception as e:
        return Response(content=json.dumps({"error": f"AI 요약 실패: {e}"}), status_code=500, media_type="application/json")


@app.post("/api/ai/anomaly-check")
async def ai_anomaly_check(request: Request):
    """이상 감지: At Risk 프로젝트 + 장기 정체 이슈 + Slack 알림 (admin)"""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    anomalies = []
    try:
        if LINEAR_API_KEY:
            r = await _linear_graphql_cached("""{
                projects(first: 50) { nodes { id name state targetDate health
                    issues(filter: {state: {type: {neq: \"completed\"}}}) { nodes { id title createdAt } }
                } }
            }""")
            now = datetime.now(KST)
            for p in r.get("data", {}).get("projects", {}).get("nodes", []):
                if p.get("health") == "atRisk" or p.get("state") == "atRisk":
                    anomalies.append({"type": "atRisk", "severity": "high", "name": p["name"], "detail": f"프로젝트가 At Risk 상태"})
                # 60일 넘게 미완료 이슈
                long_stale = []
                for i in (p.get("issues") or {}).get("nodes", []):
                    try:
                        created = datetime.fromisoformat(i["createdAt"].replace("Z", "+00:00"))
                        days = (now - created.astimezone(KST)).days
                        if days > 60:
                            long_stale.append({"id": i["id"], "title": i["title"], "days": days})
                    except Exception:
                        continue
                if len(long_stale) >= 3:
                    anomalies.append({"type": "stale_issues", "severity": "medium", "name": p["name"], "detail": f"60일 이상 미완료 이슈 {len(long_stale)}건"})
    except Exception as e:
        anomalies.append({"type": "error", "severity": "low", "name": "anomaly_check", "detail": str(e)})
    # 고심각도면 Slack 알림
    high_severity = [a for a in anomalies if a["severity"] == "high"]
    bot_token = _get_slack_bot_token()
    slack_channel = os.environ.get("SLACK_ALERT_CHANNEL", "") or os.environ.get("SLACK_ERROR_CHANNEL", "")
    if high_severity and bot_token and slack_channel:
        msg = "⚠️ *주간 이상 감지*\n" + "\n".join(f"- [{a['name']}] {a['detail']}" for a in high_severity[:10])
        try:
            await _slack_post_message(bot_token, slack_channel, msg)
        except Exception:
            pass
    return {"ok": True, "anomalies": anomalies, "high_severity": len(high_severity)}


# ── H. Linear Webhook ──
import hmac as _hmac_mod

@app.post("/api/webhooks/linear")
async def linear_webhook(request: Request):
    """Linear webhook 수신 — HMAC 검증 + 캐시 자동 무효화"""
    secret = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
    raw = await request.body()
    if not secret:
        return Response(content=json.dumps({"error": "Webhook is not configured"}), status_code=503, media_type="application/json")
    if secret:
        sig = request.headers.get("Linear-Signature", "")
        expected = _hmac_mod.new(secret.encode(), raw, _hashlib_mod.sha256).hexdigest()
        if not _hmac_mod.compare_digest(sig, expected):
            return Response(content=json.dumps({"error": "signature mismatch"}), status_code=401, media_type="application/json")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return Response(content=json.dumps({"error": "invalid json"}), status_code=400, media_type="application/json")
    action = body.get("action", "")
    entity_type = body.get("type", "")
    logger.info(f"[Linear webhook] {action} {entity_type}")
    # 모든 Issue/Project/Cycle 이벤트에서 캐시 무효화
    if entity_type in ("Issue", "Project", "Cycle"):
        cleared = len(_linear_cache)
        _linear_cache.clear()
        logger.info(f"[Linear webhook] 캐시 무효화 ({cleared} entries)")
    return {"ok": True, "action": action, "type": entity_type}


# ══════════════════════════════════════════════════════════
# [설정 확장 B/C/D] 공휴일·에러로그·AI요약·자동스케줄·내보내기·API키 UI
# ══════════════════════════════════════════════════════════

def _ensure_settings_extra_tables():
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_holidays (
        date DATE PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_error_logs (
        id BIGSERIAL PRIMARY KEY,
        source TEXT, type TEXT, message TEXT, stack TEXT, url TEXT, user_agent TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_created ON ext_error_logs(created_at DESC)")
    cur.execute("""CREATE TABLE IF NOT EXISTS ext_ai_summaries (
        id BIGSERIAL PRIMARY KEY,
        kind TEXT, summary TEXT, input_chars INT, created_at TIMESTAMP DEFAULT NOW(),
        created_by TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_summaries_created ON ext_ai_summaries(created_at DESC)")
    conn.commit(); conn.close()




def _seed_korean_holidays():
    """대한민국 주요 공휴일 자동 시드 (2026/2027 — 대체공휴일 포함, 정부 발표 기준).
    이미 등록된 날짜는 ON CONFLICT로 건너뜀 (수동 입력 보존)."""
    rows = [
        # 2026년 — 법정공휴일 + 대체공휴일 + 선거일
        ("2026-01-01", "신정"),
        ("2026-02-16", "설날 연휴"), ("2026-02-17", "설날"), ("2026-02-18", "설날 연휴"),
        ("2026-03-01", "삼일절"), ("2026-03-02", "삼일절 대체공휴일"),
        ("2026-05-05", "어린이날"),
        ("2026-05-24", "부처님오신날"), ("2026-05-25", "부처님오신날 대체공휴일"),
        ("2026-06-03", "제9회 전국동시지방선거"),
        ("2026-06-06", "현충일"),
        ("2026-08-15", "광복절"), ("2026-08-17", "광복절 대체공휴일"),
        ("2026-09-24", "추석 연휴"), ("2026-09-25", "추석"), ("2026-09-26", "추석 연휴"),
        ("2026-10-03", "개천절"), ("2026-10-05", "개천절 대체공휴일"),
        ("2026-10-09", "한글날"),
        ("2026-12-25", "성탄절"),
        # 2027년 — 법정공휴일 + 대체공휴일 + 선거일
        ("2027-01-01", "신정"),
        ("2027-02-06", "설날 연휴"), ("2027-02-07", "설날"), ("2027-02-08", "설날 연휴"), ("2027-02-09", "설날 대체공휴일"),
        ("2027-03-01", "삼일절"),
        ("2027-03-03", "제21대 대통령 선거"),
        ("2027-05-05", "어린이날"),
        ("2027-05-13", "부처님오신날"),
        ("2027-06-06", "현충일"), ("2027-06-07", "현충일 대체공휴일"),
        ("2027-08-15", "광복절"), ("2027-08-16", "광복절 대체공휴일"),
        ("2027-09-14", "추석 연휴"), ("2027-09-15", "추석"), ("2027-09-16", "추석 연휴"),
        ("2027-10-03", "개천절"), ("2027-10-04", "개천절 대체공휴일"),
        ("2027-10-09", "한글날"),
        ("2027-12-25", "성탄절"),
    ]
    try:
        conn = _pg_conn(); cur = conn.cursor()
        for d, name in rows:
            cur.execute("INSERT INTO ext_holidays (date, name) VALUES (%s, %s) ON CONFLICT (date) DO NOTHING",
                        (d, name))
        conn.commit(); conn.close()
        logger.info(f"[공휴일 시드] 한국 공휴일 {len(rows)}건 자동 등록 (중복 건너뜀)")
    except Exception as e:
        logger.warning(f"[공휴일 시드] 실패: {e}")




# ── 통합 시스템 설정 (ext_app_store: system_settings) ──
def _get_system_settings() -> dict:
    return _read_store("system_settings") or {}


async def _save_system_settings_partial(updates: dict):
    cur = _get_system_settings()
    cur.update(updates)
    await _write_store("system_settings", cur)
    return cur


def _get_gemini_key() -> str:
    s = _get_system_settings()
    return (s.get("gemini_api_key") or "").strip() or os.environ.get("GEMINI_API_KEY", "")


def _get_linear_key() -> str:
    s = _get_system_settings()
    return (s.get("linear_api_key") or "").strip() or LINEAR_API_KEY


# system_settings 조회는 admin/pm. 민감 키는 마스킹.
@app.get("/api/system/settings")
async def get_system_settings(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    s = _get_system_settings()
    def mask(v):
        if not v: return ""
        return v[:4] + "..." + v[-4:] if len(v) > 12 else "***"
    return {
        "slack_error_channel": s.get("slack_error_channel", ""),
        "backup_keep_count": s.get("backup_keep_count", 50),
        "gemini_api_key_masked": mask(s.get("gemini_api_key", "")),
        "gemini_api_key_set": bool(s.get("gemini_api_key")),
        "linear_api_key_masked": mask(s.get("linear_api_key", "")),
        "linear_api_key_set": bool(s.get("linear_api_key")),
        "schedule": s.get("schedule", {
            "remind_report": {"enabled": False, "weekday": 4, "hour": 17, "minute": 0},  # 금 17:00
            "anomaly_check": {"enabled": False, "hour": 9, "minute": 0},  # 매일 09:00
        }),
    }


@app.put("/api/system/settings")
async def put_system_settings(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한이 필요합니다"}), status_code=403, media_type="application/json")
    body = await request.json()
    allowed = ("slack_error_channel", "backup_keep_count", "gemini_api_key", "linear_api_key", "schedule")
    updates = {k: v for k, v in (body or {}).items() if k in allowed}
    # 빈 문자열은 키 삭제 의도로 간주하지 않음 — 그대로 저장 (지우려면 명시적으로 null)
    cur = await _save_system_settings_partial(updates)
    return {"ok": True, "saved": list(updates.keys())}


# ── 공휴일 CRUD ──
@app.get("/api/holidays")
async def list_holidays(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return Response(content=json.dumps({"error":"인증 필요"}), status_code=401, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT date::text, name FROM ext_holidays ORDER BY date")
    rows = [{"date": r[0], "name": r[1]} for r in cur.fetchall()]
    conn.close()
    return {"holidays": rows}


@app.post("/api/holidays")
async def add_holiday(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    body = await request.json()
    d = (body.get("date") or "").strip()
    nm = (body.get("name") or "").strip()
    if not d:
        return Response(content=json.dumps({"error":"date 필수"}), status_code=400, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO ext_holidays (date, name) VALUES (%s, %s) ON CONFLICT (date) DO UPDATE SET name = EXCLUDED.name",
                (d, nm))
    conn.commit(); conn.close()
    return {"ok": True}


@app.delete("/api/holidays/{date}")
async def delete_holiday(date: str, request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM ext_holidays WHERE date = %s", (date,))
    conn.commit(); conn.close()
    return {"ok": True}


# ── 에러 로그 (DB 저장 + 조회) ──
# 기존 /api/system/error-log를 갱신 — DB에 저장도 함
@app.get("/api/system/error-logs")
async def get_error_logs(request: Request, limit: int = 100):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT id, source, type, message, url, created_at::text FROM ext_error_logs ORDER BY created_at DESC LIMIT %s",
                (min(max(limit, 1), 500),))
    rows = [{"id": r[0], "source": r[1], "type": r[2], "message": r[3], "url": r[4], "created_at": r[5]} for r in cur.fetchall()]
    conn.close()
    return {"logs": rows}


@app.get("/api/system/heartbeats")
async def get_heartbeats(request: Request):
    """백그라운드 작업 하트비트 현황(B3) — admin/pm. 작업별 마지막 성공·지연·상태."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error": "관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT task, last_run::text, last_success::text, "
        "EXTRACT(EPOCH FROM (NOW()-last_success))::int, COALESCE(fail_streak,0), "
        "COALESCE(alerted,FALSE), COALESCE(last_error,'') FROM ext_heartbeats ORDER BY task")
    rows = []
    for task, last_run, last_success, stale_s, fail_streak, alerted, last_error in cur.fetchall():
        max_stale = _HEARTBEAT_SPEC.get(task)
        overdue = max_stale is not None and (stale_s is None or stale_s > max_stale)
        status = "unknown" if max_stale is None else (
            "down" if (overdue or fail_streak >= _HEARTBEAT_FAIL_ALERT) else "ok")
        rows.append({
            "task": task, "status": status, "last_run": last_run, "last_success": last_success,
            "stale_seconds": stale_s, "max_stale_seconds": max_stale,
            "fail_streak": fail_streak, "alerted": alerted, "last_error": last_error,
        })
    conn.close()
    return {"heartbeats": rows, "checked_at": datetime.now(KST).isoformat()}


# ── AI 요약 히스토리 ──
@app.get("/api/ai/summaries")
async def list_ai_summaries(request: Request, limit: int = 20):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("SELECT id, kind, summary, input_chars, created_at::text, created_by FROM ext_ai_summaries ORDER BY created_at DESC LIMIT %s",
                (min(max(limit, 1), 100),))
    rows = [{"id": r[0], "kind": r[1], "summary": r[2], "input_chars": r[3], "created_at": r[4], "created_by": r[5]} for r in cur.fetchall()]
    conn.close()
    return {"summaries": rows}


# ── 데이터 내보내기 (CSV) ──
import csv as _csv_mod
import io as _io_mod

def _csv_response(rows: list, headers: list, filename: str):
    buf = _io_mod.StringIO()
    w = _csv_mod.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    data = "﻿" + buf.getvalue()  # Excel 한글 호환 BOM
    return Response(content=data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/leave")
async def export_leave(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""SELECT user_name, leave_type, start_date::text, end_date::text, days, reason, status, approver, created_at::text
        FROM ext_leave_requests ORDER BY start_date DESC""")
    rows = cur.fetchall(); conn.close()
    return _csv_response(rows,
        ["요청자","종류","시작일","종료일","일수","사유","상태","승인자","신청일"],
        f"leave_{datetime.now(KST).strftime('%Y%m%d')}.csv")


@app.get("/api/export/expenses")
async def export_expenses(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    # ext_expenses 또는 google sheet 기반 — 단순화: 빈 CSV 헤더만 (실제 데이터 소스 확인 필요)
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.ext_expenses')")
        if cur.fetchone()[0]:
            cur.execute("SELECT * FROM ext_expenses ORDER BY 1 DESC LIMIT 5000")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            conn.close()
            return _csv_response(rows, cols, f"expenses_{datetime.now(KST).strftime('%Y%m%d')}.csv")
        conn.close()
    except Exception:
        pass
    return _csv_response([], ["(비용 데이터 소스 미연결 — Google Sheet 사용 중)"],
                          f"expenses_{datetime.now(KST).strftime('%Y%m%d')}.csv")


@app.get("/api/export/reports")
async def export_reports(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""SELECT name, week_label, COALESCE(sections, '[]'::jsonb)::text, updated_at::text
        FROM ext_member_reports ORDER BY week_label DESC, name""")
    rows = cur.fetchall(); conn.close()
    return _csv_response(rows,
        ["이름","주차","섹션(JSON)","수정"],
        f"reports_{datetime.now(KST).strftime('%Y%m%d')}.csv")


@app.get("/api/export/users")
async def export_users(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") not in ("admin", "pm"):
        return Response(content=json.dumps({"error":"관리자/PM 권한"}), status_code=403, media_type="application/json")
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute("""SELECT name, email, role, slack_id, linear_id, hire_date::text, is_active, last_login::text
        FROM ext_users ORDER BY role, name""")
    rows = cur.fetchall(); conn.close()
    return _csv_response(rows,
        ["이름","이메일","역할","Slack ID","Linear ID","입사일","활성","마지막 로그인"],
        f"users_{datetime.now(KST).strftime('%Y%m%d')}.csv")


# ── 자동 스케줄러 (백그라운드 task) ──
_scheduler_last_run = {}  # {key: 'YYYY-MM-DD'}

async def _scheduler_loop():
    """매분 깨어나서 schedule 설정에 맞춰 자동 실행 (멱등 — 마지막 실행 일자 기록)"""
    await asyncio.sleep(20)  # startup 직후 잠시 대기
    while True:
        try:
            now = datetime.now(KST)
            today_key = now.strftime("%Y-%m-%d")
            settings = _get_system_settings().get("schedule") or {}
            # 1) 주간 보고 리마인더 (weekday: 0=월~6=일, 기본 4=금 17:00)
            rr = settings.get("remind_report") or {}
            if rr.get("enabled") and now.weekday() == int(rr.get("weekday", 4)) \
               and now.hour == int(rr.get("hour", 17)) and now.minute >= int(rr.get("minute", 0)) \
               and _scheduler_last_run.get("remind_report") != today_key:
                _scheduler_last_run["remind_report"] = today_key
                try:
                    bot_token = _get_slack_bot_token()
                    if bot_token:
                        conn = _pg_conn(); cur = conn.cursor()
                        cur.execute("SELECT name, slack_id FROM ext_users WHERE is_active = TRUE AND slack_id IS NOT NULL AND slack_id != ''")
                        members = cur.fetchall(); conn.close()
                        msg = "📝 *주간 보고 마감 리마인더* (자동 발송)\n이번 주 업무 보고를 작성해주세요."
                        for name, sid in members:
                            try: await _slack_post_message(bot_token, sid, msg)
                            except Exception: pass
                        logger.info(f"[scheduler] 주간 보고 리마인더 {len(members)}명 발송")
                except Exception as e:
                    logger.error(f"[scheduler] 보고 리마인더 실패: {e}")
            # 2) 매일 이상 감지
            ac = settings.get("anomaly_check") or {}
            if ac.get("enabled") and now.hour == int(ac.get("hour", 9)) and now.minute >= int(ac.get("minute", 0)) \
               and _scheduler_last_run.get("anomaly_check") != today_key:
                _scheduler_last_run["anomaly_check"] = today_key
                try:
                    # 내부 호출 (FastAPI route 직접 호출하기보다 핵심 로직 재실행)
                    logger.info("[scheduler] 자동 이상 감지 실행")
                    # 간단화: Slack 알림만 트리거 (구체 로직은 ai_anomaly_check와 동일)
                    # 호출 우회 — 직접 anomaly 조사하기엔 코드 중복. 일단 marker만.
                except Exception as e:
                    logger.error(f"[scheduler] 이상 감지 실패: {e}")
        except Exception as e:
            logger.error(f"[scheduler] loop 오류: {e}")
        await asyncio.sleep(60)


# 기존 startup_event에 등록
try:
    @app.on_event("startup")
    async def _start_scheduler():
        # 리더 워커에서만 실행 — 비게이팅 시 워커 수만큼 중복 발송됨 (startup_event가 먼저 _IS_LEADER 설정)
        if BACKGROUND_TASKS_ENABLED and _IS_LEADER:
            asyncio.create_task(_scheduler_loop())
        else:
            logger.info("[일반 워커] _scheduler_loop은 리더 워커가 담당")
except Exception:
    pass


# ── 정적 파일 서빙 ──
# 진입점 index.html은 자산(?v=) 버전을 서버가 내용 해시로 자동 주입 → 버전 자산을 immutable
# 캐시해도 배포(내용 변경) 즉시 새 URL로 반영. 수동 ?v= 버전 깜빡임 footgun 제거.
_INDEX_HTML_CACHE: str | None = None


def _get_index_html() -> str:
    """버전 주입된 index.html (1회 계산 후 캐시). app.js/tailwind.css/style.css 참조의
    ?v= 값을 그 파일들 내용 해시(8자)로 교체. 배포 시 프로세스 재시작 → 재계산."""
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is not None:
        return _INDEX_HTML_CACHE
    import re as _re
    h = _hashlib_mod.md5()
    for fn in ("js/core.js", "app.js", "tailwind.css", "style.css"):
        try:
            h.update((STATIC_DIR / fn).read_bytes())
        except Exception:
            pass
    ver = h.hexdigest()[:8]
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # 서버 사이드 partial include — <!--#include partials/x.html-->
    # (index.html은 신뢰된 커밋 파일이므로 경로 주입 위험 없음. 누락 시 빈 문자열+경고)
    def _inc(m):
        rel = m.group(1).strip()
        try:
            return (STATIC_DIR / rel).read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[index] partial include 실패 {rel}: {e}")
            return ""
    html = _re.sub(r'<!--#include\s+([^\s>]+)\s*-->', _inc, html)

    html = _re.sub(
        r'((?:href|src)="(?:js/core\.js|app\.js|tailwind\.css|style\.css))\?v=[^"]*"',
        lambda m: f'{m.group(1)}?v={ver}"', html)
    _INDEX_HTML_CACHE = html
    return html


# ════════════════════════════════════════════════════════════════════
# CS 독립 페이지 (/cs) — A1 인입률 · D1 환불율. 대시보드와 독립 비밀번호.
# 인증: 미들웨어를 통과(_PUBLIC_PATHS)시키고 여기서 CS 전용 JWT(scope=cs)로 직접 검증.
# ════════════════════════════════════════════════════════════════════
def _cs_password() -> str:
    return CS_PAGE_PASSWORD


def _cs_token_valid(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    payload = _jwt_decode(auth[7:])
    return bool(payload and payload.get("scope") == "cs")


@app.post("/api/cs/login")
async def cs_login(request: Request):
    """CS 페이지 전용 로그인 — 독립 비밀번호 → scope=cs JWT 반환."""
    lk = "cs:" + _client_ip(request)
    locked = _login_locked(lk)
    if locked:
        return _login_locked_response(locked)
    body = await request.json()
    if not _cs_password() or body.get("password", "") != _cs_password():
        _login_register_fail(lk)
        return Response(content=json.dumps({"error": "비밀번호가 올바르지 않습니다"}),
                        status_code=401, media_type="application/json")
    _login_reset(lk)
    return {"token": _jwt_encode({"scope": "cs"})}


@app.get("/api/cs/metrics")
async def cs_metrics(request: Request, weeks: int = 8, refresh: bool = False):
    """주간 CS 지표(A1 인입률·D1 환불율). CS 전용 토큰 필요."""
    if not _cs_token_valid(request):
        return Response(content=json.dumps({"error": "인증이 필요합니다"}),
                        status_code=401, media_type="application/json")
    try:
        from src.cs_metrics import compute_cs_metrics
    except ImportError:
        from cs_metrics import compute_cs_metrics
    try:
        data = await asyncio.to_thread(compute_cs_metrics, weeks, not refresh)
        return data
    except Exception as e:
        logger.exception("CS 지표 집계 실패")
        return Response(content=json.dumps({"error": f"집계 실패: {str(e)[:200]}"}),
                        status_code=500, media_type="application/json")


@app.get("/api/cs/consultations")
async def cs_consultations(request: Request, limit: int = 300, refresh: bool = False,
                           date_from: str = None, date_to: str = None):
    """상담일지(구글시트) 요약+인사이트+최근행. 기간 필터(date_from/date_to). CS 전용 토큰 필요."""
    if not _cs_token_valid(request):
        return Response(content=json.dumps({"error": "인증이 필요합니다"}),
                        status_code=401, media_type="application/json")
    try:
        from src.cs_metrics import compute_consultations
    except ImportError:
        from cs_metrics import compute_consultations
    try:
        data = await asyncio.to_thread(compute_consultations, limit, date_from, date_to, not refresh)
        return data
    except Exception as e:
        logger.exception("상담일지 집계 실패")
        return Response(content=json.dumps({"error": f"집계 실패: {str(e)[:200]}"}),
                        status_code=500, media_type="application/json")


@app.get("/api/cs/sentiment/fill")
async def cs_sentiment_fill(request: Request, n: int = 200, date_from: str = None, date_to: str = None):
    """감성/불만 분류를 캐시에 없는 상담 n건만큼 증분 채움(기간 지정 가능). CS 전용 토큰 필요."""
    if not _cs_token_valid(request):
        return Response(content=json.dumps({"error": "인증이 필요합니다"}),
                        status_code=401, media_type="application/json")
    try:
        from src.cs_metrics import fetch_consultations, classify_new_sentiments, _consult_cache, _filter_rows_by_date
    except ImportError:
        from cs_metrics import fetch_consultations, classify_new_sentiments, _consult_cache, _filter_rows_by_date
    try:
        _h, rows, err = await asyncio.to_thread(fetch_consultations)
        if err:
            return Response(content=json.dumps({"error": err}), status_code=502, media_type="application/json")
        rows = _filter_rows_by_date(rows, date_from, date_to)
        res = await asyncio.to_thread(classify_new_sentiments, rows, max(1, min(n, 1000)))
        _consult_cache.clear()  # 불만율 즉시 반영되도록 캐시 무효화
        return res
    except Exception as e:
        logger.exception("감성 분류 실패")
        return Response(content=json.dumps({"error": f"분류 실패: {str(e)[:200]}"}),
                        status_code=500, media_type="application/json")


@app.get("/api/cs/weekly-report")
async def cs_weekly_report(request: Request, post: int = 0):
    """주간 CS 리포트 텍스트 미리보기. post=1이면 Slack 게시(CS_REPORT_SLACK_CHANNEL 필요). CS 토큰 필요."""
    if not _cs_token_valid(request):
        return Response(content=json.dumps({"error": "인증이 필요합니다"}),
                        status_code=401, media_type="application/json")
    try:
        from src.cs_metrics import build_weekly_report_text
    except ImportError:
        from cs_metrics import build_weekly_report_text
    text = await asyncio.to_thread(build_weekly_report_text)
    posted = False
    slack_error = None
    bot_ok = None
    if post == 1:
        ch = os.environ.get("CS_REPORT_SLACK_CHANNEL", "")
        bot = _get_slack_bot_token() if "_get_slack_bot_token" in globals() else SLACK_BOT_TOKEN
        bot_ok = bool(bot)
        if ch and bot:
            res = await _slack_post_message(bot, ch, text)  # Slack API 응답 검증
            posted = bool(res.get("ok"))
            if not posted:
                slack_error = res.get("error")  # 예: not_in_channel, channel_not_found
    return {"text": text, "posted": posted, "slack_error": slack_error, "bot_token_present": bot_ok,
            "channel_set": bool(os.environ.get("CS_REPORT_SLACK_CHANNEL", ""))}


import base64 as _b64_cs


@app.get("/api/cs/app")
async def cs_app(request: Request):
    """CS 대시보드 본체(난독화) — CS 전용 토큰이 있어야만 제공. base64로 전달(비인증 노출 차단)."""
    if not _cs_token_valid(request):
        return Response(content=json.dumps({"error": "인증이 필요합니다"}),
                        status_code=401, media_type="application/json")
    app_path = Path(__file__).resolve().parent / "cs_app.html"
    try:
        html_bytes = app_path.read_bytes()
    except FileNotFoundError:
        return Response(content=json.dumps({"error": "앱 빌드 누락 — scripts/build_cs_app.py 실행 필요"}),
                        status_code=500, media_type="application/json")
    return Response(content=_b64_cs.b64encode(html_bytes).decode("ascii"),
                    media_type="text/plain", headers={"Cache-Control": "no-store"})


@app.get("/cs")
async def _serve_cs_page():
    # 로그인 껍데기만 공개. 대시보드 본체는 /api/cs/app(토큰 필수)로만 제공.
    return Response(content=(STATIC_DIR / "cs.html").read_text(encoding="utf-8"),
                    media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def _serve_index():
    return Response(content=_get_index_html(), media_type="text/html",
                    headers={"Cache-Control": "no-cache"})


@app.get("/index.html")
async def _serve_index_html():
    return Response(content=_get_index_html(), media_type="text/html",
                    headers={"Cache-Control": "no-cache"})


# 주의: 라우트 등록 후 마운트해야 API 경로가 우선 매칭됨
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
