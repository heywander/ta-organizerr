FROM python:3.11-slim
WORKDIR /app
COPY ta_symlink.py .
RUN pip install --no-cache-dir requests flask
EXPOSE 5000
CMD ["python", "ta_symlink.py"]
