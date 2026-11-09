# 개발 안내

새 로컬 DB와 가상 데이터로 개발합니다. 기존 업무 DB·클라우드 자원·직원 계정을 테스트 대상으로 사용하지 마세요. Docker로 시작하는 방법은 [README](../README.md)에 있습니다.

## 필요한 도구

- Python 3.12
- PostgreSQL 16 또는 README의 로컬 Compose 구성
- 프런트엔드 검사·CSS 빌드 및 선택 Cloudflare 래퍼 개발 시 Node.js 22와 npm

일반 화면은 FastAPI가 정적 파일을 함께 제공합니다. 프런트엔드 빌드 서버는 필요하지 않으며, 새 CSS 클래스나 생성 화면을 수정할 때만 해당 빌드를 실행합니다.

## Python에서 직접 실행

저장소 루트에서 가상환경을 만들고 활성화합니다.

```sh
python -m venv .venv
```

| 셸 | 활성화 명령 |
| --- | --- |
| PowerShell | `.venv\Scripts\Activate.ps1` |
| POSIX 셸 | `. .venv/bin/activate` |

이후의 `python`은 가상환경의 Python을 사용합니다.

```sh
python -m pip install -r requirements.txt
python scripts/setup_env.py
```

`.env`의 `DATABASE_URL`을 자신이 만든 빈 PostgreSQL에 연결하도록 바꿉니다. 생성되는 로컬 URL은 `127.0.0.1:5432`를 사용하며 DB 서버를 직접 만들지는 않습니다. Compose는 실행 시 컨테이너 내부의 `db` 주소로 덮어쓰고 호스트에 DB 포트를 공개하지 않으므로, 네이티브 실행용 DB는 별도로 준비합니다. 기존 서비스의 접속 문자열을 복사하지 마세요.

서명키 `JWT_SECRET`은 생성된 값을 유지하거나 독립적으로 생성한 최소 32바이트의 난수로 교체합니다. 기본 비밀값과 기본 사용자는 없습니다. 선택 연동 설정은 비워 두고 백그라운드 작업·자동 버킷 생성·공용 로그인을 꺼 둡니다.

첫 번째 터미널에서 실행합니다.

```sh
python -m uvicorn src.main:app --host 127.0.0.1 --port 8080
```

앱의 테이블 초기화와 `/api/health` 정상 응답을 확인합니다. 두 번째 터미널에도 가상환경을 활성화한 뒤 초기 관리자를 만듭니다. 앱과 초기화 스크립트는 모두 루트 `.env`를 읽습니다. 이미 프로세스에 설정한 환경변수가 파일보다 우선하므로 두 프로세스가 같은 새 DB를 사용하도록 확인하세요.

```sh
python scripts/bootstrap_admin.py --name 관리자 --email admin@example.com
```

이름·이메일은 자신의 값으로 바꾸고 숨김 입력으로 비밀번호를 지정합니다. 기존 사용자가 있으면 명령은 중단됩니다. 테스트 자동화용 `--password-stdin`은 격리된 CI에서 안전한 표준입력 파이프로만 사용합니다. 실제 비밀번호를 셸 명령이나 로그에 남기지 마세요.

브라우저에서 `http://localhost:8080`에 접속합니다. `PORT`를 설정하지 않은 개발 모드에서는 `/docs`, `/redoc`, `/openapi.json`을 확인할 수 있습니다. 수동 JSON 본문을 받는 API도 있으므로 OpenAPI만으로 모든 요청 필드가 설명되지는 않습니다. 해당 엔드포인트의 소스와 테스트도 함께 확인하세요.

## 화면과 생성 파일 수정

전체 구조는 [구조 안내](ARCHITECTURE.md)에 있습니다. 기본 화면은 `static/index.html`과 `static/partials/`, 동작은 `static/app.js`와 `static/js/core.js`를 확인합니다.

루트의 잠금 파일로 Node 의존성을 설치합니다.

```sh
npm ci
```

새 Tailwind 클래스를 추가했다면 CSS를 다시 생성합니다.

```sh
npm run css:build
```

`static/tailwind.css`와 해당 라이선스 표시도 함께 보존합니다. 클래스가 빌드의 검색 대상에 포함되어 있는지 `tailwind/tailwind.config.cjs`를 확인하세요.

CS 본체는 `src/cs_app.src.html`을 수정한 뒤 표준 라이브러리 기반 빌더로 생성합니다. 생성 파일 `src/cs_app.html`을 직접 편집하지 않습니다.

```sh
python scripts/build_cs_app.py
python scripts/build_cs_app.py --check
```

생성 표시는 소스 해시를 기록하기 위한 것입니다. HTML 변환이나 클라이언트 코드 숨김이 인증을 대신하지 않습니다.

## 검증

다음 명령은 실행할 검사 목록입니다. 이 문서 자체가 특정 환경에서의 통과를 보증하지는 않습니다. PR에는 실제 실행 결과와 한계를 기록합니다.

### 오프라인 정책·화면 검사

Python 의존성이 설치된 환경에서 실행합니다. `test_*.py`는 가상 데이터와 격리된 함수 검사를 대상으로 하며 서버·운영 DB를 사용하지 않습니다.

```sh
python tests/ci_test.py
python -m unittest discover -s tests -p "test_*.py"
npm run check:js
npm run test:frontend
python scripts/build_cs_app.py --check
python scripts/check_release.py
```

공개 자료 검사는 비밀정보·사적 식별자·실제 데이터가 코드와 정적 자산에 섞이지 않았는지 확인하는 보조 장치입니다. 자동 검사와 함께 변경 내용, 주석, 생성 파일과 화면 캡처를 사람이 검토해야 합니다.

### 서버·DB 통합 검사

HTTP 스모크와 DB 변경 검사는 빈 일회용 PostgreSQL, 새 서명키, 가상 계정과 로컬 서버가 필요합니다. 네트워크를 사용하는 검사 전체를 운영 DB 대상으로 실행하지 마세요. 최신 실행 계약은 `.github/workflows/`의 CI 설정과 [테스트 파일](../tests/)에서 확인합니다.

새 설치 검증에는 빈 상태의 시작, 초기 관리자 생성, 재초기화 거부, 개인 로그인, 미인증 요청 차단, 역할별 접근과 저장·조회가 포함되어야 합니다. 온라인 연동의 키가 없는 상태와 실패 응답도 별도로 확인합니다. 오프라인 검사가 통과해도 실제 외부 API 연결까지 검증한 것은 아닙니다.

### 선택 Cloudflare 래퍼

`cf/`에서 실행합니다.

```sh
npm ci
npm run typecheck
```

이 검사는 TypeScript 검사이며 계정 생성·배포·실제 DB 연결을 수행하지 않습니다. 배포 설정과 비밀정보는 설치자가 자신의 환경에 맞게 준비합니다.

## 자주 발생하는 문제

| 증상 | 대응 |
| --- | --- |
| JWT 설정 오류 | 생성 스크립트로 `.env`를 만들고 충분한 난수 키인지 확인합니다. 예제·반복 문자열은 거부됩니다. |
| DB 연결 실패 | 빈 DB 존재 여부, 사용자 권한, URL과 컨테이너/호스트 주소 차이를 확인합니다. |
| 초기 관리자 생성 중 테이블 없음 | 앱을 먼저 시작해 스키마 초기화를 마칩니다. |
| CLI에서 `DATABASE_URL is required` | 루트 `.env`의 값 또는 프로세스의 환경변수를 확인합니다. |
| 새 CSS 클래스가 적용되지 않음 | Tailwind 검색 대상과 CSS 재빌드, 브라우저 캐시를 확인합니다. |
| CS 생성 파일이 오래됨 | 편집용 소스를 저장하고 빌더를 실행한 뒤 `--check`를 다시 실행합니다. |

기여 전에 [기여 안내](../CONTRIBUTING.md)와 [개인정보 안내](PRIVACY.md)를 확인하세요.
