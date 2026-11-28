# Rollout Marshal developer commands. Python runs through uv on Python 3.13.
.PHONY: help install test test-runtime lint fmt preflight names-json build

help:
	@echo "make install    create .venv (Python 3.13) with every dependency group"
	@echo "make test       unit tests; live tests are excluded (run them with: uv run pytest -m live)"
	@echo "make test-runtime the probe tests against the agent runtime's own pinned dependencies; after any change to agent/runtime/uv.lock"
	@echo "make lint       ruff check, ruff format --check, terraform fmt -check"
	@echo "make fmt        apply ruff fixes and formatting, and terraform fmt"
	@echo "make preflight  scripts/preflight.sh: one ok or FAIL line per check"
	@echo "make names-json regenerate contracts/names.json from contracts/names.py"
	@echo "make build      run every function's build.sh (not the spikes'); before every plan and apply"

install:
	uv sync

test:
	uv run pytest

# The two tests that run the shipped opentelemetry-instrumentation-botocore skip in the root
# environment; this runs them against agent/runtime's own lock (evidence/5.1-probe-otel.md).
test-runtime:
	uv run --project agent/runtime --frozen --with pytest python -m pytest agent/tests/test_untraced_invokes.py

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

# Each function's zip is built by a build.sh in its owner's directory, so no task
# edits this file (design.md section 2). The spikes' own scripts are left out.
build:
	@git rev-parse --is-inside-work-tree >/dev/null
	@set -e; for f in $$(git ls-files --cached --others --exclude-standard '*build.sh' | grep -v '/spike/'); do echo "== $$f"; bash "$$f"; done

names-json:
	uv run python -c 'from contracts import names; print(names.as_json(), end="")' > contracts/names.json
