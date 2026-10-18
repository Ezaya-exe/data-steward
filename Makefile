# Thin wrappers only (AGENTS.md §5) — every target maps to a dqs command so the
# project also works on Windows without make.

VENV_PY ?= .venv/Scripts/python.exe
DQS ?= $(VENV_PY) -m dqs

up:
	$(DQS) dev up

down:
	$(DQS) dev down

install:
	py -3.12 -m venv .venv && $(VENV_PY) -m pip install -e ".[dev]"

lint:
	$(DQS) dev lint

test:
	$(DQS) dev test

test-int:
	$(DQS) dev test-int

check:
	$(DQS) dev check

# available from Phase 6 (docs/PHASES.md)
demo:
	$(DQS) demo
