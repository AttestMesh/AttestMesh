FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/mcp/.local/bin:$PATH"

RUN useradd --create-home --uid 10002 mcp

WORKDIR /app
COPY pyproject.toml README.md ./
COPY telegram_fts_mcp ./telegram_fts_mcp
RUN pip install --no-cache-dir .

USER mcp
EXPOSE 8085

ENTRYPOINT ["python", "-m", "telegram_fts_mcp"]
