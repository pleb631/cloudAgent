.PHONY: install test lint format smoke
install:
	cd agent && uv sync --frozen --group dev
	cd sandbox && uv sync --frozen --group dev

test:
	cd agent && uv run --frozen pytest -q
	cd sandbox && uv run --frozen pytest -q

lint:
	cd agent && uv run --frozen ruff check . && uv run --frozen ruff format --check .
	cd sandbox && uv run --frozen ruff check . && uv run --frozen ruff format --check .

smoke:
	python scripts/smoke_test.py

format:
	cd agent && uv run --frozen ruff check --fix . && uv run --frozen ruff format .
	cd sandbox && uv run --frozen ruff check --fix . && uv run --frozen ruff format .
