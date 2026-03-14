.PHONY: test lint format install dev clean

test:
	python -m pytest tests/ -v

lint:
	python -m ruff check sesh/ tests/

format:
	python -m ruff format sesh/ tests/

install:
	pip install -e .

dev:
	pip install -e ".[all]" ruff pytest

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
