.PHONY: test lint fmt check scan install clean

VENV ?= /opt/wheatley/venv
PYTHON ?= $(VENV)/bin/python3
PYTEST ?= $(VENV)/bin/pytest
RUFF ?= $(VENV)/bin/ruff

test:
	$(PYTEST) tests/

lint:
	$(RUFF) check .

fmt:
	$(RUFF) format .

check: lint test scan

scan:
	bash scripts/verify_sanitized.sh

install:
	pip install -e ".[dev]"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache
