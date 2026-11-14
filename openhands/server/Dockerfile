FROM ghcr.io/openhands/agent-server:1.62.0-python
COPY server.py /opt/cloudagent/server.py
ENTRYPOINT ["tini", "--", "/agent-server/.venv/bin/python", "/opt/cloudagent/server.py"]
