# 선택 연동

외부 연동 없이 먼저 새 DB와 초기 관리자로 앱을 실행하세요. 이 문서는 어떤 설정을 준비해야 하는지 설명합니다. 외부 서비스의 계정, 데이터셋, 스프레드시트나 운영용 API는 저장소에 포함하지 않습니다.

설정은 서버 환경변수와 관리자 화면의 연동 설정에서 읽습니다. 일부 설정은 관리자 화면의 값이 환경변수보다 우선할 수 있습니다. 사용하지 않을 연동은 자격증명과 자원 ID를 모두 비워 두세요. 브라우저에 키를 넣지 않습니다.

## 공통 설정

| 설정 | 용도 |
| --- | --- |
| `PUBLIC_BASE_URL` | 설치한 대시보드의 공개 주소. 외부 알림의 링크 등에 사용합니다. |
| `ALLOWED_ORIGINS` | 허용할 브라우저 출처 목록. 스킴·호스트·포트를 포함하고 쉼표로 구분합니다. |
| `BACKGROUND_TASKS_ENABLED` | 예약 동기화, 수집, 알림 등의 백그라운드 작업 활성화. 기본값은 `false`입니다. |
| `EGRESS_DIAGNOSTICS_ENABLED` | 외부 연결 진단 기능 활성화. 기본값은 `false`입니다. |

연동을 활성화할 때는 가상 데이터로 수동 요청을 먼저 확인한 다음 예약 작업을 켭니다. 외부 연동이 실패해도 빈 화면이나 0이 표시될 수 있으므로 응답의 오류·경고와 서버 로그를 함께 확인하세요.

## 서비스별 준비

| 서비스 | 서버 설정 | 준비할 자원과 권한 |
| --- | --- | --- |
| Slack | `SLACK_BOT_TOKEN`, 선택 알림 채널 설정 | 자신의 워크스페이스 봇, 대상 채널 참여 및 필요한 메시지 권한 |
| Linear | `LINEAR_API_KEY`, `LINEAR_TEAM_ID`, 웹훅 사용 시 `LINEAR_WEBHOOK_SECRET` | 자신의 팀과 필요한 프로젝트·이슈 읽기/쓰기 권한 |
| Google Calendar | `GOOGLE_CALENDAR_ID`, Google 자격증명 | 자신의 캘린더와 조회·수정에 필요한 공유 권한 |
| 지출 Google Sheets | `EXPENSE_SHEET_ID`, `EXPENSE_SHEET_NAME`, Google 자격증명 | 자신의 시트, 앱이 사용하는 열 구조 및 읽기/쓰기 권한 |
| BigQuery | `BQ_PROJECT`, `BQ_ORDERS_TABLE`, `BQ_LLM_TABLE`, 선택 `BQ_GA4_DATASET`, Google 자격증명 | 자신의 데이터셋·테이블/뷰와 쿼리 권한 |
| 지표 API | `METRICS_API_URL`, `METRICS_PROD_API_URL`, `METRICS_API_KEY` | 앱이 기대하는 응답을 제공하는 자신의 API 또는 변환 계층 |
| 분석 DB | `ANALYTICS_DB_URL` | 자신의 분석 스키마·뷰 및 읽기 전용 계정. 앱 저장용 `DATABASE_URL`과 구분합니다. |
| Gemini | `GEMINI_API_KEY` | 자신의 API 계정, 전송할 본문과 비용 정책 검토 |
| CS | `CS_PAGE_PASSWORD`, `CS_SHEET_ID`, `CS_SHEET_TAB`, 필요 시 `CHANNELTALK_ACCESS_KEY`, `CHANNELTALK_ACCESS_SECRET` | 자신의 상담 시트·CS 계정과 분석 소스 |
| 파일 저장소 | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `STORAGE_BUCKET` | 자신의 버킷과 파일 접근 정책. 서비스 역할 키는 서버 전용입니다. |

Google 자격증명은 설치 환경의 `GOOGLE_APPLICATION_CREDENTIALS` 경로 또는 `GOOGLE_CREDENTIALS_JSON`으로 공급합니다. 자격증명 파일과 JSON을 저장소나 컨테이너 이미지에 복사하지 마세요. 사용하는 SDK 경로가 실제 자격증명 전달 방식을 지원하는지도 확인합니다.

Slack의 오류·경보·CS 보고 채널은 각각 `SLACK_ERROR_CHANNEL`, `SLACK_ALERT_CHANNEL`, `CS_REPORT_SLACK_CHANNEL`로 설정할 수 있습니다. 실제 수신자를 설정하기 전 시험용 채널에서 확인하세요.

## 자신의 데이터에 맞추기

업무용 DB에는 앱 자체 테이블만 초기화됩니다. 외부 분석 테이블·뷰·API와 시트의 업무 데이터는 자동 생성되지 않습니다. 키와 ID를 넣는 것만으로 임의의 데이터 소스가 호환되지는 않습니다.

연동 전에 [백엔드](../src/main.py), [CS 집계](../src/cs_metrics.py), [지표 변환](../src/util.py)의 쿼리와 응답 처리 코드를 확인하세요. 필요한 열 이름과 형식을 자신의 데이터 소스에 맞춘 뷰로 제공하거나, 해당 어댑터를 수정해야 합니다. 최소한 다음 계약을 확인합니다.

- 주문·매출: 시각, 결제 상태, 금액과 집계 단위. 구매 건수와 구매자 수의 의미도 정합니다.
- LLM 사용량: 모델 이름, 입력·출력·캐시 토큰, 성공 상태와 통화. `LLM_PRICING_JSON`, `USD_KRW_RATE`, `API_COST_MARKUP`은 자신의 기준으로 설정합니다.
- 일별 요약 API: 날짜와 일별 순매출·건수·LLM 비용 필드, 조회 기간과 `llm_cost_available` 의미.
- 방문자: GA4 이벤트 테이블과 사용자·세션 식별 방식.
- 지출·상담 시트: 코드가 읽는 탭, 열 위치, 날짜·금액 형식과 수정 대상 행.

처음에는 검증용 작은 기간을 조회하여 원본 합계와 대조합니다. 원본이 미설정이거나 조회 실패한 상태의 빈 값·0은 실제 실적 0으로 해석하면 안 됩니다. 캐시를 다시 수집하기 전에 소스와 오류를 확인하세요.

## 파일과 데이터베이스 권한

`STORAGE_AUTO_CREATE_BUCKET=false`가 기본값입니다. 버킷을 직접 준비하고, 파일의 공개 여부와 다운로드 경로를 검토한 뒤 업로드를 시험합니다. 로컬 임시 파일과 외부 저장소 파일은 백업·보관 정책도 다를 수 있습니다.

PostgreSQL 앱 테이블은 서버 API를 통해 접근하는 비공개 데이터입니다. Supabase를 사용하더라도 브라우저에서 서비스 역할 키로 테이블에 직접 접근하지 않습니다. 공개 Data API를 사용한다면 적절한 RLS 정책을 적용하거나 비공개 스키마·역할 권한으로 앱 테이블을 보호해야 합니다.

Supabase의 Data API에 노출되는 `public` 스키마에 설치할 경우, 앱 테이블을 초기화한 뒤 소유자 권한으로 [앱 테이블 보호 SQL](../sql/protect_data_api.sql)을 검토하여 적용하세요. 이 SQL은 `ext_`로 시작하는 테이블과 `project_hierarchy`에 RLS를 켜고 `PUBLIC`, `anon`, `authenticated`의 테이블 권한을 회수합니다. 서버는 비공개 PostgreSQL 연결과 앱의 권한 검사를 계속 사용합니다. 직접 Data API 접근을 허용하는 정책은 추가하지 않습니다. 이 파일은 설치자가 자신의 프로젝트에 적용하는 선택 설치 절차이며, 저장소를 내려받는 것만으로 기존 프로젝트에 실행되지 않습니다. 앱 테이블을 새로 추가한 뒤에도 보호 범위를 다시 확인하세요.

## 선택 배포 래퍼

[cf/](../cf/)는 FastAPI 컨테이너로 요청을 전달하는 Cloudflare Containers 래퍼입니다. 로컬 실행에는 필요하지 않습니다. 배포하려면 자신의 Cloudflare 계정, 컨테이너 자원, DB 네트워크 경로, 도메인과 비밀 저장소를 준비하고 설정을 검토해야 합니다. 이 저장소는 계정이나 배포 자원을 대신 만들지 않습니다.

자격증명과 실제 업무 자료를 다룰 때는 [개인정보 안내](PRIVACY.md)와 [보안 정책](../SECURITY.md)을 함께 확인하세요.
