FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY agents ./agents
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home agent
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "cloudagent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
