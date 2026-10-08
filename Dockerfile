FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[postgres,otlp]"
RUN useradd --uid 10001 --create-home app
USER app
# Cloud Run injects PORT (default 8080).
ENV PORT=8080 AGENTRT_PERSISTENCE=postgres AGENTRT_OTEL=otlp
EXPOSE 8080
CMD ["sh", "-c", "uvicorn agent_runtime.api:create_app --factory --host 0.0.0.0 --port ${PORT}"]
