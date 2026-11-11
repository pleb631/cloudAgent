# Thin deployment image: the lifecycle API is upstream OpenSandbox, not local code.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY config.toml /etc/opensandbox/config.toml
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8090
ENTRYPOINT ["/app/.venv/bin/opensandbox-server"]
CMD ["--config", "/etc/opensandbox/config.toml"]
