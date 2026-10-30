"""Benchmark runner: scenarios x configs x seeds -> scored results JSON + traces.

Held-out discipline: any split other than ``dev`` refuses to run unless ``--final`` is given,
and every such run is appended to ``results/heldout_runs.log`` (committed) so the number of
held-out evaluations is auditable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.aggregate import aggregate
from bench.configs import ALL_CONFIGS, build_agent
from bench.score import score_run
from env.perturb import PerturbedEnv, RenamedServices, wrap
from env.sim.sim_env import SimEnv
from env.types import Action, ActionResult
from scenarios.schema import SCENARIO_ROOT, Scenario, load_split

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
HELDOUT_LOG = RESULTS_DIR / "heldout_runs.log"
PERTURBATION_SPLITS = (
    "perturbations/noisy_logs",
    "perturbations/missing_metrics",
    "perturbations/renamed_services",
    "perturbations/timestamp_jitter",
    "perturbations/unseen_fault",
)


class ProbeEnv(PerturbedEnv):
    """Transparent wrapper that counts env calls and records the ground-truth recovery time."""

    def __init__(self, inner, seed: int, sim: SimEnv) -> None:
        super().__init__(inner, seed)
        self.sim = sim
        self.calls = 0
        self.recovered_at: float | None = None
        self._check()

    def _check(self) -> None:
        if self.recovered_at is None and self.sim.gt_recovered():
            self.recovered_at = self.sim.now()

    def _count(self) -> None:
        self.calls += 1

    def get_status(self):  # noqa: D102
        self._count()
        return self.inner.get_status()

    def get_metrics(self, window: float):  # noqa: D102
        self._count()
        return self.inner.get_metrics(window)

    def get_logs(self, service, window, grep=None):  # noqa: D102
        self._count()
        return self.inner.get_logs(service, window, grep)

    def get_flows(self, window: float):  # noqa: D102
        self._count()
        return self.inner.get_flows(window)

    def apply_action(self, action: Action) -> ActionResult:  # noqa: D102
        self._count()
        r = self.inner.apply_action(action)
        self._check()
        return r

    def wait(self, seconds: float) -> None:  # noqa: D102
        self.inner.wait(seconds)
        self._check()


def git_sha() -> str:
    """Current commit (or 'unknown')."""
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_one(
    scenario: Scenario, config: str, seed_offset: int, model: str, replay: bool, budget: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one episode and score it."""
    sim = SimEnv(scenario, seed_offset)
    pert_name = scenario.perturbation.name if scenario.perturbation else None
    env = wrap(sim, pert_name, scenario.seed)
    name_map = env.fwd if isinstance(env, RenamedServices) else None
    probe = ProbeEnv(env, scenario.seed, sim)
    agent = build_agent(config, model=model, replay=replay)
    t_start = sim.now()
    t0 = time.perf_counter()
    trace = agent.run(probe, budget)
    trace.wall_seconds = time.perf_counter() - t0
    trace.scenario_id = scenario.id
    trace.seed_offset = seed_offset
    trace.model = agent.model
    trace.env_calls = probe.calls
    tdict = trace.to_dict()
    tdict["time_to_recovery_s"] = (
        None if probe.recovered_at is None else probe.recovered_at - t_start
    )
    gt = sim.gt_summary()
    tdict["ground_truth"] = gt
    tdict["perturbation"] = pert_name
    score = score_run(tdict, scenario, gt, name_map)
    score["seed_offset"] = seed_offset
    score["config"] = config
    return score, tdict


def run_split(
    split: str,
    configs: list[str],
    seeds: int,
    model: str,
    replay: bool,
    budget: int,
    limit: int | None,
    tag: str,
    workers: int,
    root: Path = SCENARIO_ROOT,
    out_dir: Path = RESULTS_DIR,
) -> list[Path]:
    """Run every config on a split; write one results file per config."""
    scenarios = load_split(split, root)
    if limit:
        scenarios = scenarios[:limit]
    written: list[Path] = []
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for config in configs:
        jobs = [(sc, so) for sc in scenarios for so in range(seeds)]
        name = f"{stamp}_{split.replace('/', '-')}_{config}{('_' + tag) if tag else ''}"
        trace_dir = out_dir / "traces" / name
        scores: list[dict[str, Any]] = []
        t0 = time.perf_counter()

        def work(job: tuple[Scenario, int], config=config, trace_dir=trace_dir):
            sc, so = job
            score, tdict = run_one(sc, config, so, model, replay, budget)
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / f"{sc.id}_s{so}.json").write_text(json.dumps(tdict, default=str))
            return score

        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as ex:
            for k, score in enumerate(ex.map(work, jobs), 1):
                scores.append(score)
                print(
                    f"[{config}] {k}/{len(jobs)} {score['scenario_id']} s{score['seed_offset']} "
                    f"top1={score['top1']:.0f} rec={score['recovered']:.0f} "
                    f"false_rec={score['false_recovery']:.0f} harm={score['harmful_actions']:.0f} "
                    f"llm={score['llm_calls']:.0f}",
                    flush=True,
                )
        summary = aggregate(scores)
        result = {
            "meta": {
                "split": split,
                "config": config,
                "model": model,
                "replay": replay,
                "seeds": seeds,
                "budget": budget,
                "tag": tag,
                "git": git_sha(),
                "timestamp": stamp,
                "n_scenarios": len(scenarios),
                "elapsed_s": round(time.perf_counter() - t0, 1),
                "trace_dir": str(trace_dir.relative_to(out_dir.parent))
                if trace_dir.is_relative_to(out_dir.parent)
                else str(trace_dir),
            },
            "summary": summary,
            "scores": scores,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(result, indent=1))
        written.append(path)
        m = summary["metrics"]
        print(
            f"== {split} / {config}: n={summary['n_runs']} top1={m['top1']['mean']:.2f} "
            f"recall={m['fault_recall']['mean']:.2f} recovered={m['recovered']['mean']:.2f} "
            f"false_rec={m['false_recovery']['mean']:.2f} "
            f"harmful={m['harmful_actions']['mean']:.2f} -> {path.name}",
            flush=True,
        )
    return written


def guard_heldout(split: str, final: bool, configs: list[str], model: str, tag: str) -> None:
    """Refuse to touch non-dev splits without --final; log every such run."""
    if split == "dev":
        return
    if not final:
        sys.exit(
            f"refusing to run split {split!r} without --final (held-out discipline). "
            "Tune on dev only."
        )
    HELDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HELDOUT_LOG.open("a") as fh:
        fh.write(
            f"{datetime.now(UTC).isoformat()} split={split} configs={','.join(configs)} "
            f"model={model} tag={tag} git={git_sha()}\n"
        )


def main(argv: list[str] | None = None) -> None:
    """CLI."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--config", default="agent", help="comma-separated: " + ",".join(ALL_CONFIGS))
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument(
        "--replay", action="store_true", help="never call the LLM; use the committed cache only"
    )
    ap.add_argument(
        "--final", action="store_true", help="required for heldout / perturbation splits"
    )
    ap.add_argument("--budget", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args(argv)
    configs = [c.strip() for c in args.config.split(",") if c.strip()]
    splits = list(PERTURBATION_SPLITS) if args.split == "perturbations" else [args.split]
    for split in splits:
        guard_heldout(split, args.final, configs, args.model, args.tag)
        run_split(
            split,
            configs,
            args.seeds,
            args.model,
            args.replay,
            args.budget,
            args.limit,
            args.tag,
            args.workers,
        )


if __name__ == "__main__":
    main()
