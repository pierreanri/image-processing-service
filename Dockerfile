FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STORAGE_DIR=/data/storage

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/storage \
    && chown appuser /data/storage
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
