FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["/app/.venv/bin/uvicorn", "sandbox_service.manager:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
