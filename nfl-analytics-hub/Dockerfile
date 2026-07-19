FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    DATA_DIR=/app/data

RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /app/data && chown -R app:app /app

USER app
EXPOSE 8080

CMD ["gunicorn", "app:app", "-c", "gunicorn_conf.py"]
