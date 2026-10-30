# LIMITATIONS.md — what this does not show (honest list)

Each item names the file that embodies the limitation so it can be checked.

## Benchmark realism

* **The simulator is small and hand-written.** Five services, one dependency graph, nine known
  fault types with scripted symptom signatures (`env/sim/world.py`, `faults/`). Real incidents
  have messier, partially-overlapping signatures. The held-out split tests unseen *combinations*
  and one unseen *type*, not unseen symptom vocabularies.
* **Fault symptoms are consistent with the taxonomy in the prompt.** The agent is told what a
  memory leak or a beacon looks like (`agent/prompts.py::fault_taxonomy`). This is the same
  knowledge an SRE runbook encodes, but it means the benchmark measures evidence integration and
  planning more than open-world discovery. The `unseen_fault` perturbation (dns_failure) is the
  only test of a fault the taxonomy does not describe.
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
* **Replay is exact, re-recording is not.** Cached responses make every number reproducible
  with `--replay`. Re-running against Ollama can produce slightly different completions
  (batched decoding is not bit-stable even at temperature 0), so a fresh recording is a new
  experiment, not a reproduction.

## Evaluation budget (laptop, fanless)

* **Seeds.** Held-out headline numbers use 3 seed offsets per scenario (60 runs per
  configuration). Perturbation runs, ablations and the smaller-model run use **1 seed** (20 runs
  each) to keep every benchmark command within the laptop time budget; their CIs are wide and
  their deltas should be read as directional. The exact wall-clock of every command is recorded
  in `results/*.json` (`meta.elapsed_s`).
* **Recording time.** One full dev run of the agent is ~25–50 min of model time (2–3 Ollama
  workers). All held-out, perturbation and ablation recordings together take several hours;
  that is why `make smoke` and CI run from the cache.
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
* **The evidence gate trusts the LLM's hypothesis list.** A wrong `c2_beacon` hypothesis on an
  innocent host *would* unlock containment of that host; the regression check catches the
  availability loss (`tests/test_agent.py::test_safety_rolls_back_regressions`) but not a
  silent `block_egress` on a host that needs no egress.

## Scope

* No multi-tenant / multi-cluster topology, no autoscaling, no persistent storage faults beyond
  disk-full, no network partitions, no multi-step attacks (lateral movement, exfiltration).
* The agent cannot invent actions outside the eight-item allowlist; a fault whose fix is not in
  the allowlist is unrecoverable by design (it escalates with the trace).
