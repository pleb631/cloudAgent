FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache
COPY agents ./agents
ENV PATH="/app/.venv/bin:$PATH" OPENHANDS_SUPPRESS_BANNER=1 LITELLM_LOCAL_MODEL_COST_MAP=True
CMD ["uvicorn", "cloudagent_oh.api:app", "--host", "127.0.0.1", "--port", "8001", "--workers", "1"]
