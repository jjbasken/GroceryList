FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

RUN groupadd --system --gid 10001 grocery \
    && useradd --system --uid 10001 --gid grocery --home-dir /app grocery \
    && mkdir -p /data \
    && chown grocery:grocery /data

# Application code stays root-owned so the runtime user cannot modify it.
COPY . .

EXPOSE 5000

USER grocery:grocery

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "1", "--worker-connections", "1000", "--timeout", "0", "--keep-alive", "75", "app:app"]
