.PHONY: install test lint format smoke test-docker openhands-install openhands-test openhands-lint
install:
	cd openhands && uv sync --frozen --group dev

test:
	cd openhands && uv run --frozen pytest -q

lint:
	cd openhands && uv run --frozen ruff check . && uv run --frozen ruff format --check .

format:
	cd openhands && uv run --frozen ruff check --fix . && uv run --frozen ruff format .

# Real SDK + local agent-server, scripted model; no Docker or paid model calls.
smoke:
	cd openhands && uv run --frozen pytest tests/test_sdk_integration.py -m 'not docker' -v

# Requires a test Docker host and the built agent-server image.
test-docker:
	cd openhands && RUN_OPENHANDS_DOCKER_TESTS=1 uv run --frozen pytest tests/test_sdk_integration.py -m docker -v

# Compatibility aliases for CI and existing developer commands.
openhands-install: install
openhands-test: test
openhands-lint: lint
