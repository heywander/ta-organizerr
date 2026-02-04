# --- Stage 1: Frontend Build (Bun) ---
FROM oven/bun:1 as frontend
WORKDIR /app/ui

# Copy frontend code
COPY ui/package.json ui/bun.lock ./
RUN bun install --frozen-lockfile

COPY ui .
RUN bun run build

# --- Stage 2: Backend (Python) ---
FROM python:3.11-slim
WORKDIR /app

# 1. Install System Deps (ffmpeg, curl for yt-dlp)
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp

# 2. Install Python Deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy Backend Code
COPY ta_symlink.py .
# Copy other backend files if any (templates/ was old, but maybe still needed if I missed something? No, I replaced routes.)
# But just in case, I'll exclude templates from copy or just copy everything and ignore.
# Using .dockerignore would be good, but explicit copy is fine too.
# Let's copy specific files or everything. The previous dockerfile did `COPY . .`.
# I should probably just copy the python file and any others.
# Let's stick to what was there but ensure we copy the BUILT frontend.

# 4. Copy Built Frontend from Stage 1
# Python app expects: os.path.join(os.getcwd(), 'ui', 'dist')
# defaults WORKDIR /app, so /app/ui/dist
COPY --from=frontend /app/ui/dist ./ui/dist

# 5. CMD
CMD ["python", "ta_symlink.py"]