# LIMITATIONS.md — what this does not show (honest list)

Each item names the file that embodies the limitation so it can be checked.

## Benchmark realism

* **The simulator is small and hand-written.** Five services, one dependency graph, nine known
  fault types with scripted symptom signatures (`env/sim/world.py`, `faults/`). Real incidents
  have messier, partially-overlapping signatures. The held-out split tests unseen *combinations*
  and one unseen *type*, not unseen symptom vocabularies.
* **The prompt carries the simulator's fix table.** `agent/prompts.py::fault_taxonomy` builds
  each fault's "Fix:" line by calling that fault class's own `correct_sequences()`, the same
  sequences the simulator accepts as resolving the fault, and describes each fault's symptoms.
  This is what an SRE runbook encodes, and the rule-based runbook baseline is hand-coded with the
  same symptom-to-fix knowledge (`agent/runbook.py::_rules`), which is why it is the main
  comparison: with the remedies
  known, the benchmark measures *diagnosis under concurrent, confounded faults, safe ordering and
  verified recovery*, not the discovery of new remedies. On held-out the runbook diagnoses almost
  as well (top-1 87 %) but recovers 45 %; the gap is the loop, the ordering and the verifier.
* **The unseen fault type is recovered, not identified.** `dns_failure` is not in the taxonomy,
  so the agent cannot name it: per-fault recall on it is 0/8 and the 62 % top-1 on that split
  comes from the *known* co-fault. It does localize the faulty service (localization 94 %) and
  recovers 8/8, partly because `restart_service` is one of its accepted fixes
  (`faults/security.py::DnsFailure.correct_sequences`), as it is for 6 of the 10 fault types.
  Read it as "degrades safely on an unknown fault", not "diagnoses unknown faults".
* **Top-1 counts a hit on any injected fault.** `bench/score.py` scores the top hypothesis as
  correct when it matches *any* of the scenario's faults (service + type), so top-1 gets easier
  as faults stack (agent 75 / 93 / 100 % at 1 / 2 / 3 faults). Per-fault recall (82 %), which
  needs every fault found, is the stricter diagnosis number.
* **Ground-truth recovery is defined by the simulator** (`SimEnv.gt_recovered`): all injected
  faults resolved, all services up, observable health ≥ 0.9 for 2 ticks. Partial mitigations
  (e.g. containing a beacon without eradicating it) count as *not recovered* by design.
* **Timing is compressed.** 10 s ticks, 25-minute incidents, beacons every 25–60 s. Real C2
  beacons often use minute-to-hour intervals; the detector features are scale-invariant
  (`tests/test_detectors.py::test_beacon_features_are_scale_invariant`) but this was only
  demonstrated down to 1.5 s (live) and up to 60 s (simulator).

## Detectors and model

* **Beacon detector trained on synthetic data** (`agent/detectors/beacon_train.py`). Its
  reported precision/recall/AP are on a synthetic test set plus the simulator's *dev*
  scenarios; no real network captures were used. A real deployment would need retraining on
  NetFlow/Zeek data and would face evasion (very long intervals, traffic blending).
* **The allow-list is a prior.** Allow-listed and internal destinations need stronger evidence;
  a beacon fronted through a trusted CDN is in the training positives but gets a higher bar.
* **Local 7B model.** qwen2.5:7b mis-attributes facts between services and ignores status
  fields unless they are spelled out (see docs/RESULTS.md v0 → v1). Prompt grouping and the
  repeated-plan guard mitigate this; a stronger model would need fewer guard-rails.
* **One perturbation was re-recorded.** The timestamp-jitter wrapper salted its noise with
  Python's `hash()` of a string, which is randomised per process, so its prompts (and the LLM
  cache keys) changed on every run and the published jitter result could not be replayed. It
  now uses a stable CRC32 salt (`env/perturb.py::_stable_salt`,
  `tests/test_runner.py::test_jitter_is_identical_across_processes`) and that one 20-run split
  was recorded again; the superseded file stays in `results/` and both runs are in
  `results/heldout_runs.log`. CI now replays every committed held-out and perturbation result
  and fails on any difference (`bench/replay_check.py`), which is how this was found.
* **`meta.git` in a results file is the commit at the time the file was written**, i.e. at the
  end of the run; `results/heldout_runs.log` records the commit at the start.
* **Replay is exact, re-recording is not.** Cached responses make every number reproducible
  with `--replay`. Re-running against Ollama can produce slightly different completions
  (batched decoding is not bit-stable even at temperature 0), so a fresh recording is a new
  experiment, not a reproduction.

## Evaluation budget (laptop, fanless)

* **Seeds.** Held-out headline numbers for agent, runbook, single_shot, do_nothing and random
  use 3 seed offsets per scenario (60 runs each). **llm_only, the perturbation runs, the
  ablations and the smaller-model run use 1 seed** (20 runs each; 8 for the unseen fault type)
  to keep the laptop time budget; their CIs are wide and their deltas should be read as
  directional. Their deltas are paired: each is compared with the full agent on the same seed
  (`bench/report.py::_paired`), not with the 3-seed mean. llm_only's 3-seed pass was started twice and stopped (10/60 both times; see
  `results/heldout_runs.log`) because its raw-telemetry prompts make it ~5× slower than the
  agent. The exact wall-clock of every command is recorded in `results/*.json`
  (`meta.elapsed_s`): agent held-out 63 min, single_shot 29 min, llm_only (1 seed) 95 min,
  ablations 39 min in total (no_beacon 32, no_safety 6.5; no_verify and no_loop replay the
  agent's first-iteration cache entries in seconds), smaller model 45 min, perturbations 78 min
  in total (19 / 22 / 20 / 9 / 8), dev iterations 25–53 min each.
* **Recording time.** Our own build constraint was benchmark commands under 15 minutes; with a 7B model
  at ~20 tokens/s the held-out commands exceed that and we chose fewer seeds over fewer
  scenarios. `make smoke` and CI run from the cache in under a minute.
* **Dev baselines.** Only the runbook was run on dev with 3 seeds; LLM baselines were measured
  on held-out only (single-shot and llm_only are not tuned, so dev numbers would add cost
  without changing anything).

## Live environment

* **Emulated where the real thing would hurt the laptop** (`env/live/service.py`): disk "full"
  is a 120 MB quota on a sandbox directory; memory *pressure* slows handlers in proportion to
  the leaked bytes (the leak itself is real); the C2 server and update mirror are one local
  sinkhole; service names are fixed.
* **Not used for benchmark numbers.** The live system is the demo and the "same agent, two
  environments" evidence (`tests/test_live_env.py`), not a second benchmark: its timing is
  non-deterministic, so the LLM cache does not apply and every demo run is a fresh recording.
* **Single host, loopback only.** No real network isolation: `isolate_host` is a flag inside the
  process; `block_egress` is enforced by the service's own outbound code path.

## Safety layer

* **Rollback is best-effort.** Containment, rate limiting and pool scaling are reversible;
  restarts, config rollbacks, disk clears and cache flushes are not, and the record says so
  (`agent/safety.py::SafetyLayer.execute`).
* **The evidence gate trusts the beacon detector.** With the detector on, containment
  (`isolate_host`, `block_egress`) needs a detector beacon finding on that host
  (`agent/safety.py::SafetyLayer.screen`); an LLM hypothesis alone is not enough. A detector
  false positive would therefore unlock containment of an innocent host. The regression check
  catches the availability loss of an isolation
  (`tests/test_agent.py::test_safety_rolls_back_regressions`) but not a silent `block_egress` on
  a host that needs no egress. With the detector off (`llm_only`, `no_beacon`), an open
  `c2_beacon` hypothesis is accepted instead.

## Scope

* No multi-tenant / multi-cluster topology, no autoscaling, no persistent storage faults beyond
  disk-full, no network partitions, no multi-step attacks (lateral movement, exfiltration).
* The agent cannot invent actions outside the eight-item allowlist; a fault whose fix is not in
  the allowlist is unrecoverable by design (it escalates with the trace).
