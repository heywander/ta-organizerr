FROM python:3.11-slim
WORKDIR /app

# 1. Install System Deps (ffmpeg) FIRST
# These rarely change, so Docker will cache this layer forever.
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# 2. Install Python Deps SECOND
# Only re-runs if requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy Code LAST
# Now, if you change code, only this fast step re-runs.
COPY . .

CMD ["python", "ta_symlink.py"]