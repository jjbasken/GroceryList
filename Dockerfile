FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

RUN groupadd --system --gid 10001 grocery \
    && useradd --system --uid 10001 --gid grocery --home-dir /app grocery \
    && mkdir -p /data \
    && chown grocery:grocery /data

COPY --chown=grocery:grocery . .

EXPOSE 5000

USER grocery:grocery

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "1", "--worker-connections", "1000", "--timeout", "0", "--keep-alive", "75", "app:app"]
