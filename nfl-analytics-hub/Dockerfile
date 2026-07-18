FROM python:3.11-slim

# Bundled redis so REDIS_URL=redis://localhost:6379 works out of the box;
# point REDIS_URL at a managed Redis in production if preferred.
RUN apt-get update && apt-get install -y --no-install-recommends redis-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080 \
    DATA_DIR=/app/data

EXPOSE 8080

CMD ["sh", "-c", "redis-server --daemonize yes --save '' --appendonly no && exec gunicorn app:app -c gunicorn_conf.py"]
