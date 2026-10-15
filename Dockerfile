# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]
# One process per container: scale with API_REPLICAS. The container port is never published to the host, so
# only Traefik (on the private Docker network) can reach it. We trust X-Forwarded-* only from private
# ranges, NOT from "*": with "*" uvicorn would take the left-most (client-controlled) X-Forwarded-For entry.
# Override with API_FORWARDED_ALLOW_IPS (see docker-compose.yml) when a CDN sits in front.
ENV FORWARDED_ALLOW_IPS=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--no-server-header", "--proxy-headers", "--timeout-graceful-shutdown", "20"]
