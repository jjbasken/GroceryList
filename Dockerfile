FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

RUN groupadd --system --gid 10001 grocery \
    && useradd --system --uid 10001 --gid grocery --home-dir /app grocery \
    && mkdir -p /data \
    && chown grocery:grocery /data

# Application code stays root-owned so the runtime user cannot modify it.
COPY . .

EXPOSE 5000

USER grocery:grocery

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "1", "--worker-connections", "1000", "--timeout", "0", "--keep-alive", "75", "--no-control-socket", "app:app"]
