FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/agent/.local/bin:$PATH"

RUN useradd --create-home --uid 10001 agent

WORKDIR /app
COPY pyproject.toml README.md ./
COPY telegram_admin_agent ./telegram_admin_agent
RUN pip install --no-cache-dir .

USER agent
EXPOSE 9100

ENTRYPOINT ["python", "-m", "telegram_admin_agent"]
