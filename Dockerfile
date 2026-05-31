# ── Stage 1: build the React + Vite frontend ─────────────────────────────────
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
COPY plugins/ /plugins/
RUN npm run build

# ── Stage 3: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim

ARG BUILD_VERSION=dev
LABEL org.opencontainers.image.title="mycelium" \
      org.opencontainers.image.description="Self-hosted media pipeline: watchlist to .strm via TorBox" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/corveck79/mycelium"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8088 \
    PUID=99 \
    PGID=100

WORKDIR /app

# gosu lets the entrypoint drop privileges to the mapped UID/GID after fixing
# ownership on /data; ffmpeg is required for stub MKV generation.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 100 mycgrp \
    && useradd -u 99 -g 100 -m -s /bin/sh mycelium \
    && mkdir -p /data && chown -R mycelium:mycgrp /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY releases.json ./
COPY plugins/ ./plugins/
COPY templates/ ./templates/
COPY docs/ ./docs/
# Built SPA from stage 1 (Vite writes to ../static/app relative to frontend/)
COPY --from=frontend /static/app/ ./static/app/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R mycelium:mycgrp /app

EXPOSE 8088

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
port=os.environ.get('LISTEN_PORT','8088'); \
r=urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5); \
sys.exit(0 if r.status==200 else 1)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn --bind ${LISTEN_HOST}:${LISTEN_PORT} --workers 1 --threads 8 --access-logfile - app:app"]
