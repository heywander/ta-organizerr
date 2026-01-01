FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests flask
RUN mkdir -p /app/data
EXPOSE 5000
CMD ["python", "ta_symlink.py"]
