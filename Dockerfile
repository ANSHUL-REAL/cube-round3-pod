# Pod 12: the console, the API and all five agents in one container (the agents run in-process).
# Durable state lives in Postgres (DATABASE_URL); the container's disk is only a working copy, restored on start.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8100 LOG_FORMAT=json \
    INPUT_DIR=/app/data/input PACK_LEDGER_PATH=/app/out/pack-ledger.json OUT_DIR=/app/out

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 pod \
    && mkdir -p /app/out /app/data/input /app/.cache \
    && chown -R pod:pod /app
USER pod

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8100\")}/health', timeout=8)"
# One worker: every request of a workflow reads and writes it whole; one process keeps that simple and correct.
# --proxy-headers: Render's load balancer says the visitor's address and https in X-Forwarded-* headers.
CMD ["sh", "-c", "exec uvicorn orchestration.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
