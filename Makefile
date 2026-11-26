# Rollout Marshal developer commands. Python runs through uv on Python 3.13.
.PHONY: help install test lint fmt preflight

help:
	@echo "make install    create .venv (Python 3.13) with every dependency group"
	@echo "make test       unit tests; live tests are excluded (run them with: uv run pytest -m live)"
	@echo "make lint       ruff check, ruff format --check, terraform fmt -check"
	@echo "make fmt        apply ruff fixes and formatting, and terraform fmt"
	@echo "make preflight  scripts/preflight.sh: one ok or FAIL line per check"

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	terraform fmt -check -recursive infra

fmt:
	uv run ruff check --fix .
	uv run ruff format .
	terraform fmt -recursive infra

preflight:
	./scripts/preflight.sh
