# --- Stage 1: Frontend Build (Bun) ---
FROM oven/bun:1.1.34 AS frontend
WORKDIR /app/ui

COPY ui/package.json ui/bun.lock ./
RUN bun install --frozen-lockfile

COPY ui .
RUN bun run build


# --- Stage 2: Backend (Python) ---
FROM python:3.11.10-slim AS backend
WORKDIR /app

# 1. System deps. ffmpeg is a runtime requirement of the transcoder.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 2. Python deps (yt-dlp pinned via pip, avoiding the latest-binary curl).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Backend code
COPY ta_organizerr ./ta_organizerr

# 4. Built frontend from stage 1
COPY --from=frontend /app/ui/dist ./ui/dist

# 5. Drop privileges. App data dirs are bind-mounted at runtime; we just need
#    a valid uid that owns /app for any auxiliary writes (e.g. caches).
RUN useradd --system --uid 1001 --no-create-home --shell /usr/sbin/nologin app \
 && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1 \
    PORT=5000 \
    HOST=0.0.0.0

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",5000)}/api/auth/setup-status', timeout=3).read()" || exit 1

# Single worker because the scheduler runs in-process; multiple workers would
# scan in parallel and clobber the DB.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", \
     "--timeout", "300", "--access-logfile", "-", "ta_organizerr.wsgi:app"]
