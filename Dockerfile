# -- Stage 1: build the React + Vite frontend -----------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
COPY plugins/ /plugins/
RUN npm run build

# -- Stage 3: Python runtime ----------------------------------------------------------
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
    LIBVA_DRIVER_NAME=iHD \
    PUID=99 \
    PGID=100

WORKDIR /app

ARG TARGETARCH
# gosu lets the entrypoint drop privileges to the mapped UID/GID after fixing
# ownership on /data. ffmpeg is required for stub MKV generation; the Intel
# VA-API driver (iHD = Gen8+, includes J3455/J4125) enables webplayer hardware
# transcode and is x86-only (skipped on arm64).
# Note: the build-time UID/GID are arbitrary (8088). At runtime the entrypoint
# remaps them to PUID/PGID (default 99/100 for Unraid) with usermod -o /
# groupmod -o so duplicate IDs against base-image groups (Debian uses GID 100
# for "users") are not a conflict.
RUN echo "deb http://deb.debian.org/debian bookworm contrib non-free non-free-firmware" \
        > /etc/apt/sources.list.d/non-free.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        libva2 \
        libva-drm2 \
    && if [ "$TARGETARCH" = "amd64" ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 8088 mycgrp \
    && useradd -u 8088 -g 8088 -m -s /bin/sh mycelium \
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
# Also copy pre-built SPA if present (skips npm build when static/app/ is tracked)
COPY static/ ./static/

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
