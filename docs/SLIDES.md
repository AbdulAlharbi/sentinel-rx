# SLIDES.md — 9 slides with speaker notes

Numbers in brackets are filled from `results/summary.md` (held-out) — never from dev.

## 1. Title — Sentinel-RX: security-aware agentic recovery of a failing system
*Visual:* name, one-line pitch, "100% local & free: qwen2.5:7b via Ollama, no Docker".
*Notes:* Challenge: "Agentic Recovery of a Failing System". We built the agent **and** the
benchmark that proves it on unseen incidents, plus a live demo on real processes.

## 2. State of the field and the gap
*Visual:* OpenRCA / ITBench ~11% solve rate; table of gaps G1–G5 from the literature review.
*Notes:* Existing benchmarks inject one fault at a time, never hide a security incident inside an
ops incident, and accept the agent's own "fixed" claim. Our five targets: concurrent faults,
hidden security incidents, false recovery claims, agent cost, robustness to unseen/degraded input.

## 3. Thesis
*Visual:* three pillars — hybrid detectors + LLM; contain-before-restart; verification gate.
*Notes:* A small local model is enough **if** it reasons over ranked evidence instead of raw
telemetry, is fenced by a safety layer, and is not allowed to grade its own work.

## 4. Architecture
*Visual:* README flowchart; "one agent interface, two environments" (SimEnv, LiveEnv).
*Notes:* Env interface of nine calls. Simulator: 5 services, dependency propagation, realistic
noise, legitimate periodic traffic for false-positive pressure. Live: same five services as real
processes with real fault injection and a sinkhole for the beacon.

## 5. Agent loop and safety
*Visual:* MAPE-K loop; evidence JSON snippet; safety decisions (allowlist, evidence gate,
contain-before-restart, dry-run, transactional rollback).
*Notes:* Two LLM calls per iteration. The beacon detector is a RandomForest on scale-invariant
timing features + destination knowledge; it must not flag health checks or update polls.
A restart of a beaconing host before containment is blocked by the safety layer and counted as
harmful by the scorer.

## 6. Evaluation method
*Visual:* dev/held-out split diagram; perturbation list; baselines & ablations; metric list.
*Notes:* Held-out combinations and seeds never appear in dev (checked by tests); the runner
refuses held-out without `--final` and logs every run. Three seeds, bootstrap CIs. Baselines:
runbook, single-shot LLM, LLM-only loop. Ablations: no verify, no loop, no beacon, no safety.
Record/replay cache: the full benchmark re-runs with no LLM.

## 7. Held-out results
*Visual:* leaderboard table + `results-by-config.png` + `accuracy-by-number-of-concurrent-faults.png`.
*Notes:* [agent vs runbook vs single_shot vs llm_only: top-1, recall, recovered, false recovery,
harmful, tokens]. Emphasise false-recovery and harmful-action columns: that is where the
verification gate and the safety layer show up.

## 8. Robustness and ablations
*Visual:* `robustness-drop.png`, `ablation-deltas.png`, `cost-vs-accuracy.png`.
*Notes:* [per-perturbation drop]; smaller model run; which component contributes what;
cost per incident vs recovery.

## 9. Limitations, path to production, links
*Visual:* three bullets each; repo / Pages / literature review links.
*Notes:* Limitations: hand-written simulator, synthetic beacon training, 1 seed for
perturbations/ablations, 7B model needs guard-rails. Production: NetFlow/Zeek features,
real runbook actions behind the same allowlist, human approval for irreversible actions,
retraining the detector on real captures. Roadmap: lateral movement, multi-cluster topology.
