# Sentinel-RX — one-command targets. All runs are local and free.
PY := .venv/bin/python
PIP := .venv/bin/pip
SEEDS ?= 3
MODEL ?= qwen2.5:7b
SPEED ?= 1

.PHONY: setup test lint scenarios smoke bench-dev bench-heldout bench-perturb ablate replay-check report demo-live demo-video all-final clean

setup:  ## create venv, install pinned deps, generate scenarios, train the beacon detector
	python3.12 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.lock
	$(PIP) install -e .
	$(PY) -m scenarios.generate
	$(PY) -m agent.detectors.beacon_train

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

test:
	$(PY) -m pytest -q

scenarios:
	$(PY) -m scenarios.generate

smoke:  ## 5 dev scenarios, replay cache only, no LLM needed
	$(PY) -m bench.runner --split dev --config agent --limit 5 --seeds 1 --replay --tag smoke

bench-dev:
	$(PY) -m bench.runner --split dev --config agent --seeds $(SEEDS) --model $(MODEL)

bench-heldout:  ## headline numbers; refuses to run without --final
	$(PY) -m bench.runner --split heldout --config agent,runbook,single_shot,llm_only --seeds $(SEEDS) --final --model $(MODEL)

bench-perturb:
	$(PY) -m bench.runner --split perturbations --config agent --seeds 1 --final --model $(MODEL)
	$(PY) -m bench.runner --split heldout --config agent --seeds 1 --final --model qwen2.5:1.5b --tag small_model

ablate:
	$(PY) -m bench.runner --split heldout --config no_verify,no_loop,no_beacon,no_safety --seeds 1 --final --model $(MODEL)

replay-check:  ## replay every committed held-out / perturbation result from the cache and diff it
	$(PY) -m bench.replay_check

report:
	$(PY) -m bench.report

demo-live:
	$(PY) -m env.live.demo --model $(MODEL)

demo-video:  ## ~90 s narrated walkthrough for a screen recording: the recorded live run + a live replay check (no LLM)
	$(PY) -m env.live.showcase --speed $(SPEED)

all-final: test bench-heldout bench-perturb ablate report

clean:
	rm -rf results/tmp .pytest_cache .ruff_cache
