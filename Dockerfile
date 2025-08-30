# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1         PYTHONDONTWRITEBYTECODE=1         PYTHONUNBUFFERED=1

# rclone + basics
RUN apt-get update && apt-get install -y --no-install-recommends         rclone ca-certificates tzdata tini      && rm -rf /var/lib/apt/lists/*

# Telethon
RUN pip install --no-cache-dir telethon==1.40.0

WORKDIR /app
RUN mkdir -p /data/downloads /data/logs
COPY monitor.py /app/monitor.py

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/monitor.py"]
