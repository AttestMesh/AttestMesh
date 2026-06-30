FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/agent/.local/bin:$PATH"

RUN useradd --create-home --uid 10001 agent

WORKDIR /app
COPY pyproject.toml README.md ./
COPY postgres_admin_agent ./postgres_admin_agent
RUN pip install --no-cache-dir .

USER agent
EXPOSE 9100

ENTRYPOINT ["python", "-m", "postgres_admin_agent"]
