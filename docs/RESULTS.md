# RESULTS.md — iteration log (DEV split only)

Every entry records a change, the command that measured it, and before/after numbers on the
**dev** split. Held-out numbers are never written here; they live in `results/` and the README
and are produced once, at the end, by `make bench-heldout` (`--final`).

Metric definitions: `bench/score.py`. CIs: 95% percentile bootstrap over (scenario x seed) runs,
`bench/aggregate.py`.

## 2026-10-30 — P2: first end-to-end numbers, runbook baseline (no LLM)

Command: `python -m bench.runner --split dev --config runbook --seeds 3 --tag p2`
Result file: `results/20261030T002014Z_dev_runbook_p2.json` (n = 30 scenarios x 3 seeds = 90)

| metric | runbook |
|---|---|
| top-1 root cause | 84% [77, 92] |
| top-3 root cause | 93% [88, 98] |
| per-fault recall | 84% [78, 90] |
| recovered (ground truth) | 60% [49, 70] |
| false "recovered" claims | 27% [18, 37] |
| harmful actions / run | 0.03 [0.00, 0.07] |
| unnecessary actions / run | 0.66 [0.50, 0.81] |
| isolated-before-restart (security runs) | 80% [60, 100] |

By concurrency: 1 fault → recovered 70%, 2 faults → 58%, 3 faults → 50%.

Observations that shaped the next phases:

* The runbook diagnoses well (its thresholds are literally the fault definitions) but **recovers
  only 60%** and **claims recovery falsely 27%** of the time: it blocks beacon egress but never
  eradicates the implant, and it cannot tell "symptom" from "cause" (restarts upstream services
  when a downstream dependency is slow → 0.66 unnecessary actions per run).
* A contained implant must stay observable (denied egress attempts are now emitted as
  `blocked` flows + a firewall log line, `faults/security.py`), otherwise *no* verifier could
  separate "contained" from "eradicated".
