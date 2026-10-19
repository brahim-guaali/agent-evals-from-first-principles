PY ?= .venv/bin/python

setup:            ## create .venv and install everything
	python3 -m venv .venv
	$(PY) -m pip install -q -e '.[dev]'

platform:         ## start the food-delivery platform API on :8710
	$(PY) -m uvicorn services.app:app --port 8710

collector:        ## start the OpenTelemetry collector (OTLP/HTTP) on :4318 (stage 10 on)
	$(PY) -m uvicorn services.collector:app --port 4318

test:             ## run the test suite (no API key needed)
	$(PY) -m pytest -q

stage-%:          ## run one stage on the real model, e.g. make stage-01
	$(PY) $(wildcard $*-*/run.py)

record:           ## re-run every stage and export the traced runs (viz/record_runs.sh)
	viz/record_runs.sh

.PHONY: setup platform collector test record
