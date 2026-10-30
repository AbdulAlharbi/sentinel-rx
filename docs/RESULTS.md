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

## 2026-10-30 — P5 v0: first full agent run on dev (qwen2.5:7b, 1 seed)

Command: `python -m bench.runner --split dev --config agent --seeds 1 --workers 2 --tag v0`
Result file: `results/20261030T003921Z_dev_agent_v0.json` (n = 30; 53 min wall-clock, 2 workers)

| metric | runbook (3 seeds) | agent v0 |
|---|---|---|
| top-1 root cause | 84% [77, 92] | 70% [53, 83] |
| per-fault recall | 84% [78, 90] | 67% [50, 81] |
| recovered | 60% [49, 70] | **73% [57, 87]** |
| false "recovered" claims | 27% [18, 37] | **0% [0, 0]** |
| harmful actions / run | 0.03 | **0.00** |
| unnecessary actions / run | 0.66 | 5.13 |
| isolated-before-restart | 80% | **100%** |
| LLM calls / run | 0 | 7.4 |
| tokens / run | 0 | 10 695 |

By concurrency (top-1 / recovered): 1 fault 100% / 100%; 2 faults 50% / 58%; 3 faults 62% / 62%.

Failure analysis (traces in `results/traces/20261030T003921Z_dev_agent_v0/`):

1. **Down services misdiagnosed** (`dev_2f_01`, `dev_2f_02`, `dev_2f_07`, `dev_2f_08`, `dev_3f_01`):
   the status said `cache: down` but the 7B model answered `cache_failure -> flush_cache`
   (or `bad_config_deploy -> rollback_config` on a crashed api) and then **repeated the identical
   plan up to 7 times** until the budget ran out — the source of the 5.1 unnecessary actions/run
   and of the 16-call runs.
2. **Cross-service attribution** (`dev_3f_01`: "worker disk_full" although the disk errors were
   on db; `dev_3f_02`: "worker db_conn_exhaustion"): the evidence was presented as flat lists
   (all metric anomalies, then all log signatures), so facts from different services got mixed.
3. **Beacon false positives in short verification windows** (`worker->cache p=0.53`,
   `api->payments.example p=0.85`): with ~10 Poisson events a pair can look periodic by chance;
   the verifier then reported a phantom beacon and the loop kept going.
4. `dev_3f_02`: the 128 `authentication failed ... from <ip>` lines on gateway were never
   mapped to `credential_stuffing` (same flat-list problem; the signature was the 2nd log item).

Changes for v1 (commits `4d90332`, `17797cd`):

* `Evidence.to_prompt()` groups everything **per service**, ranked by root-cause score, intrinsic
  metrics first; `down_services` is an explicit top-level field; the system prompt states that a
  down service is `process_crash` and only `restart_service` brings it back.
* **Repeated-plan guard** (`agent/loop.py`): if the plan is a subset of actions already applied
  without recovery, the model is re-asked once with an explicit warning; the history block lists
  "already applied without full recovery" actions.
* **Strict verification thresholds** for the beacon detector: new pairs need p ≥ 0.75 (unknown
  external) / ≥ 0.95 (internal or allow-listed) inside the short post-action window.

## 2026-10-30 — P5 v1: per-service evidence, down-service rule, repeated-plan guard

Command: `python -m bench.runner --split dev --config agent --seeds 1 --workers 3 --tag v1`
Result file: `results/20261030T013252Z_dev_agent_v1.json` (n = 30; 31 min wall-clock, 3 workers)

| metric | agent v0 | agent v1 | change |
|---|---|---|---|
| top-1 root cause | 70% [53, 83] | **90% [80, 100]** | +20 pts |
| per-fault recall | 67% [50, 81] | **78% [66, 89]** | +11 pts |
| recovered | 73% [57, 87] | **100% [100, 100]** | +27 pts |
| false "recovered" claims | 0% | 0% | = |
| harmful actions / run | 0.00 | 0.00 | = |
| unnecessary actions / run | 5.13 | **1.47** | −3.7 |
| LLM calls / run | 7.4 | **4.0** | −46% |
| tokens / run | 10 695 | **5 357** | −50% |

By concurrency (top-1 / recovered): 1 fault 90% / 100%; 2 faults 83% / 100%; 3 faults 100% / 100%.
All five previously budget-exhausted scenarios now recover in 2–6 calls.

Remaining failure analysis (`results/traces/20261030T013252Z_dev_agent_v1/`):

1. **Actions on the wrong target after dependency-log misattribution** (`dev_1f_06`:
   `flush_cache(api)`, `flush_cache(worker)` because api/worker log "cache miss ratio
   (fallthrough to db)"; `dev_3f_03` same). Costs 2–4 extra calls and unnecessary actions.
2. **Containment unlocked by an inbound attack** (`dev_3f_03`: `block_egress(gateway)` for
   credential stuffing) — the evidence gate accepted any security hypothesis, and in fact only
   gated `isolate_host`, not `block_egress` (an earlier patch had silently not applied).
3. **Verify false positive** `api->payments.example p=0.80` (`dev_1f_06`): the strict
   verification threshold was also not wired through (same cause). One wasted iteration.
4. Fault-type errors that still recover (`dev_1f_03`: cache `latency_injection` labelled
   `cache_failure` but fixed by `restart_service(cache)`) count as top-1 misses but not as
   recovery misses — the metric definitions keep these separate on purpose.

Changes for v2 (commit `94737f0`):

* Containment (`isolate_host`, `block_egress`) requires a **beacon finding** from the detector;
  only when the detector is disabled (ablation / llm_only) does a `c2_beacon` hypothesis suffice;
  `credential_stuffing` never unlocks containment (`tests/test_agent.py`).
* Strict verification thresholds actually wired (`EvidenceBuilder._assemble(strict=True)`).
* System prompt rule: a log line naming a dependency is the dependency's symptom.

## 2026-10-30 — P5 v2 (pre-audit scorer): containment gate, strict verify, log attribution

Command: `python -m bench.runner --split dev --config agent --seeds 1 --workers 3 --tag v2`
Result file: `results/20261030T020656Z_dev_agent_v2.json` (n = 30; 29 min)

| metric (pre-audit scorer) | agent v1 | agent v2 |
|---|---|---|
| top-1 root cause | 90% | 93% [83, 100] |
| per-fault recall | 78% | 84% [73, 94] |
| recovered | 100% | 93% [83, 100] |
| false "recovered" claims | 0% | **7% [0, 17]** |
| harmful actions / run | 0.00 | 0.00 |
| LLM calls / run | 4.0 | 3.7 |

Two **false recovery claims** appeared (`dev_2f_06`, `dev_3f_07`): the beacon on api was in the
initial evidence (p = 0.80 / 1.00), the model never acted on it, and the *strict* verification
threshold introduced in v2 then missed the same pair in the short post-action window (4–5
events). The gate failed exactly where it must not. Fix (in v3): pairs flagged during the
incident are *known*; in verification any un-blocked flow on a known pair fails the check,
whatever the short-window score. Strict thresholds only apply to *new* pairs.

## 2026-10-30 — Scorer audit (requested mid-P5)

**Question 1 — why did `dev_1f_01` (db_conn_exhaustion on db) count as recovered although its first
plan was `block_egress(worker)`, `restart_service(worker)`, `rollback_config(api)`?**
Because recovery is scored on the *final* ground-truth state, not on the first plan: verification
rejected the state after iterations 0 and 1 ("too many connections x5"), the model re-planned,
and iteration 2 applied `scale_connections(db)`, which resolved the fault
(`results/traces/20261030T013252Z_dev_agent_v1/dev_1f_01_s0.json`, ground_truth.actions: only
the 7th of 8 actions carries a fault note). The leniency was in the *other* metrics: 7 of the 8
actions did nothing and `unnecessary_actions` (key-based) counted them inconsistently, and
`block_egress(worker)` on an uncompromised host was not counted as harmful.

**Question 2 — do faults clear on their own, or via the wrong target?** No. Audit script and new
tests (`tests/test_faults.py::test_faults_persist_without_remediation`,
`test_wrong_target_or_wrong_action_never_resolves`): every fault type is still active after 300
ticks of inaction and after every allowlisted action on every other service and every wrong
action on the right service (8 actions × 5 services × 10 types). The only cross-service effect is
dependency propagation (`World.compute_metrics`; cache_failure additionally loads db — documented
in DESIGN.md §2), which never *resolves* anything. One real leniency was found: with
`health = mean(service health)`, four fault types (latency_injection, bad_config_deploy,
c2_beacon, credential_stuffing) left observable health ≥ 0.9 while active, so naive baselines
could *claim* recovery; ground truth was never fooled because `gt_recovered` requires every
fault resolved. Fixed: `health = 0.5·mean + 0.5·min` (`env/base.py::combine_health`), credential
stuffing now saturates the auth backend (error +0.2).

**Question 3 — sanity baselines on dev (3 seeds, n = 90 each):**

| baseline | recovered | false "recovered" | harmful / run | action precision |
|---|---|---|---|---|
| do_nothing | **0% [0, 0]** | 3% [0, 8] | 0.00 | n/a |
| random (1–3 allowlisted actions / iteration) | **1% [0, 3]** | 7% [2, 12] | 3.58 | 4% |
| runbook | 60% [49, 70] | 0% [0, 0] | 0.03 | 72% |

Neither sanity baseline recovers anything: the recovery check is not broken. Both stay in the
benchmark (`bench/configs.py`). Files: `results/20261030T024307Z_dev_{runbook,do_nothing,random}_audit.json`.

**Question 4 — scorer changes (`bench/score.py`, tests in `tests/test_score.py`):**

* `action_precision` = effective actions / applied actions, where *effective* means the
  simulator recorded a fault state change (resolved or contained) — `SimEnv.apply_action` now
  stores an `effective` flag; for older traces it is derived from the fault notes.
* `unnecessary_actions` = applied − effective (stricter: repeated correct actions count).
* `block_egress` / `isolate_host` on a service with no c2 compromise is **harmful**.
* `rolled_back_actions` counts safety-layer undos; undone actions still count as applied.
* Simulator realism to match: blocking egress of a service with legitimate external calls (api →
  payments) now adds 0.3 error rate while blocked, and a restart after containment rebuilds the
  host and lifts the block (`tests/test_sim.py::test_restart_after_containment_rebuilds_and_lifts_the_block`).

**Question 5 — corrected dev numbers (re-scored from saved traces, `python -m bench.rescore`):**

| metric (audited scorer) | v0 | v1 | v2 |
|---|---|---|---|
| top-1 root cause | 70% [53, 83] | 90% [80, 100] | 93% [83, 100] |
| per-fault recall | 67% [50, 81] | 78% [66, 89] | 84% [73, 94] |
| recovered | 73% [57, 87] | 100% [100, 100] | 93% [83, 100] |
| false "recovered" claims | 0% | 0% | 7% [0, 17] |
| harmful actions / run | **0.27 [0.10, 0.50]** | **0.23 [0.10, 0.43]** | 0.00 |
| unnecessary actions / run | 5.37 | 1.53 | 1.13 |
| action precision | 52% [38, 66] | 70% [60, 81] | 80% [69, 89] |
| actions / run | 7.1 | 3.6 | 3.1 |

What changed: v0 and v1 were **not** harmless — they blocked egress on uncompromised hosts
0.27 / 0.23 times per run (pre-audit tables above say 0.00 and are kept as "pre-audit").
v2's containment gate (beacon evidence required) removed that. Files:
`results/*_rescored.json`. v2's three `block_egress(api)` runs targeted a genuinely compromised
api, so the new egress cost does not change their outcome, but under the new simulator a
rebuild now lifts the block; v3 (next entry) is a full re-run on the audited simulator + scorer.
