FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

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
