FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/rentmont ./src/rentmont
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 rentmont \
    && useradd --uid 10001 --gid rentmont --no-create-home rentmont \
    && mkdir -p /app/data \
    && chown rentmont:rentmont /app/data

USER rentmont

ENTRYPOINT ["python", "-m", "rentmont"]
