.PHONY: help install style test

help:
	@echo "Makefile for proof-bench"
	@echo "Usage:"
	@echo "  make install    Install dependencies"
	@echo "  make style      Lint & format"
	@echo "  make test       Run unit tests"

install:
	uv sync --dev

venv_check:
	@if [ ! -f .venv/bin/activate ]; then \
		echo "Virtualenv not found. Run 'make install' first."; \
		exit 1; \
	fi

format: venv_check
	uv run ruff format .
lint: venv_check
	uv run ruff check --fix .
style: format lint

test: venv_check
	uv run pytest tests/ -x -q
