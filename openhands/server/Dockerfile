FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM ghcr.io/openhands/agent-server:1.62.0-python
# The published image contains a bundled server, not /agent-server/.venv.
# Build a locked Python environment so the policy shim can import the SDK.
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/cloudagent/python \
    UV_PROJECT_ENVIRONMENT=/opt/cloudagent/venv
WORKDIR /opt/cloudagent
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY server/server.py ./server.py
RUN uv sync --frozen --group dev --python 3.12 --managed-python --no-cache \
    && chmod -R a+rX /opt/cloudagent
USER openhands
WORKDIR /
ENTRYPOINT ["tini", "--", "/opt/cloudagent/venv/bin/python", "/opt/cloudagent/server.py"]
