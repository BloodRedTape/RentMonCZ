FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/rentmont ./src/rentmont
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data

ENTRYPOINT ["python", "-m", "rentmont"]
