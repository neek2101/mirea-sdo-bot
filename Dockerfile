FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data && chown bot:bot /app/data

USER bot
ENV DATABASE_PATH=/app/data/bot.sqlite3
VOLUME ["/app/data"]

CMD ["sdo-bot"]
