.PHONY: setup test e2e run case serve health examples expected cases

setup:            ## create .venv and install dependencies
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	@test -f .env || cp .env.example .env

test:             ## all tests: contracts, hand-offs, workflow state, UNCERTAIN, failures, overrides, e2e, HTTP, examples
	.venv/bin/python -m pytest

e2e:              ## just the end-to-end tests
	.venv/bin/python -m pytest tests/e2e

run:              ## run every sample workflow; state -> out/workflows/*.json, evidence -> out/evidence/*.json
	LOG_LEVEL=WARNING .venv/bin/python -m orchestration.run --all

case:             ## one workflow, full JSON:  make case UNIT=UNIT-0014 ORG=org_demo_alpha
	@.venv/bin/python -m orchestration.run --unit $(UNIT) --org $(ORG)

serve:            ## orchestrator API on :8100  (POST /workflows, GET /workflows/{id}, GET /health)
	.venv/bin/uvicorn orchestration.api:app --port 8100

health:           ## health of the orchestrator and every agent
	curl -s localhost:8100/health | python3 -m json.tool

cases:            ## rebuild data/sample/cases.json from the sample CSVs
	.venv/bin/python scripts/build_sample_cases.py

expected:         ## rebuild data/expected/ (golden outcomes for the organiser STUBS + standard flow)
	.venv/bin/python scripts/build_expected.py

examples:         ## regenerate examples/ from real runs of the stubs
	.venv/bin/python scripts/make_examples.py
