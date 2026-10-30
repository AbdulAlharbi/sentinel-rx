# SLIDES.md — 9 slides with speaker notes

All numbers below are held-out (`results/summary.md`; agent/runbook/single_shot 3 seeds, llm_only/ablations/perturbations 1 seed).

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
*Notes:* agent: top-1 92% [83, 98], per-fault recall 82%, recovered 100%, false claims 0%, harmful 0.00, 6.1k tokens. Runbook: top-1 87%, recovered 45%, false claims 5%. Single-shot LLM: recovered 30%, false claims 70%, harmful 0.27/run. LLM-only loop: recovered 35%, false claims 15%, harmful 0.55/run, 35k tokens (5.7× the hybrid). do_nothing / random: 0% recovered (random: 3.45 harmful/run). Emphasise the false-recovery and harmful columns: that is where the verification gate and the safety layer show up.

## 8. Robustness and ablations
*Visual:* `robustness-drop.png`, `ablation-deltas.png`, `cost-vs-accuracy.png`.
*Notes:* recovered stays 95–100% under noisy logs, missing metrics, renamed services, timestamp jitter and the unseen fault type (unseen type: top-1 62%, the agent labels it 'other' but still fixes it). 1.5B model: recovered 50%, top-1 40%, still 0% false claims and 0 harmful. Ablations: no verify → false claims 80%; no loop → recovered 20%; no beacon detector → recovered 50%, false claims 35%; no safety → 0.60 harmful actions/run. Cost: 4.6 calls / 6.1k tokens per incident vs 18.4 calls / 35k for llm_only.

## 9. Limitations, path to production, links
*Visual:* three bullets each; repo / Pages / literature review links.
*Notes:* Limitations: hand-written simulator, synthetic beacon training, 1 seed for
perturbations/ablations, 7B model needs guard-rails. Production: NetFlow/Zeek features,
real runbook actions behind the same allowlist, human approval for irreversible actions,
retraining the detector on real captures. Roadmap: lateral movement, multi-cluster topology.
