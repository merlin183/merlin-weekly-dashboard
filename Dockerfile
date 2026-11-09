# 주간 업무 보고 대시보드 — Cloudflare Containers 이미지
# (Railway는 Procfile로 구동, Cloudflare는 이 Dockerfile로 빌드. 실행 명령은 Procfile과 동일하게 유지)
FROM python:3.12-slim

# 시간대는 Railway와 같은 UTC 유지 — 코드의 스케줄(datetime.now() 기준)이 UTC 전제로 돌고 있음
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.txt

COPY src ./src
COPY static ./static
COPY scripts/bootstrap_admin.py ./scripts/bootstrap_admin.py

# data/ 는 임시 작업 폴더(백업 등) — 실데이터는 모두 DB·Supabase Storage에 있어 재시작 시 사라져도 무방
RUN mkdir -p data

EXPOSE 8080
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 2"]
