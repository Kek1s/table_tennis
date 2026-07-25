FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    DATABASE_PATH=/data/table_tennis.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY table_tennis_bot ./table_tennis_bot

RUN mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "-m", "table_tennis_bot.main"]

