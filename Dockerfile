FROM python:3.11-slim
WORKDIR /app
COPY ta_symlink.py .
RUN pip install --no-cache-dir requests
CMD ["python", "ta_symlink.py"]
