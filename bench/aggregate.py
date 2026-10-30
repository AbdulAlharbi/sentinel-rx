"""Aggregate per-run scores: means, 95% bootstrap CIs, and breakdowns."""

from __future__ import annotations

from typing import Any

import numpy as np

HEADLINE_METRICS = (
    "top1",
    "top3",
    "fault_recall",
    "recovered",
    "false_recovery",
    "harmful_actions",
    "unnecessary_actions",
    "isolated_before_restart",
    "llm_calls",
    "tokens",
    "wall_seconds",
    "steps",
    "time_to_recovery_s",
)


def bootstrap_ci(
    values: list[float], n_boot: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean and 95% percentile-bootstrap CI (deterministic)."""
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan"), float("nan")
    if arr.size == 1:
        return float(arr[0]), float(arr[0]), float(arr[0])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    means = arr[idx].mean(axis=1)
    return float(arr.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary over runs: per-metric mean+CI, plus breakdown by number of faults."""
    summary: dict[str, Any] = {
        "n_runs": len(scores),
        "metrics": {},
        "by_n_faults": {},
        "security": {},
    }
    for m in HEADLINE_METRICS:
        vals = [s.get(m) for s in scores if s.get(m) is not None]
        mean, lo, hi = bootstrap_ci(vals)
        summary["metrics"][m] = {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n": len(vals)}
    for n in sorted({s["n_faults"] for s in scores}):
        sub = [s for s in scores if s["n_faults"] == n]
        summary["by_n_faults"][str(n)] = {
            m: dict(
                zip(
                    ("mean", "ci_lo", "ci_hi"),
                    bootstrap_ci([s[m] for s in sub if s.get(m) is not None]),
                    strict=True,
                )
            )
            for m in ("top1", "fault_recall", "recovered", "false_recovery")
        }
        summary["by_n_faults"][str(n)]["n_runs"] = len(sub)
    sec = [s for s in scores if s.get("has_security")]
    if sec:
        summary["security"] = {
            "n_runs": len(sec),
            "recovered": bootstrap_ci([s["recovered"] for s in sec])[0],
            "isolated_before_restart": bootstrap_ci(
                [
                    s["isolated_before_restart"]
                    for s in sec
                    if s.get("isolated_before_restart") is not None
                ]
            )[0],
            "harmful_actions": bootstrap_ci([s["harmful_actions"] for s in sec])[0],
        }
    return summary


def fmt(metric: dict[str, float], pct: bool = True) -> str:
    """Format mean [lo, hi] for tables."""
    if metric is None or np.isnan(metric.get("mean", float("nan"))):
        return "n/a"
    if pct:
        lo, hi = 100 * metric["ci_lo"], 100 * metric["ci_hi"]
        return f"{100 * metric['mean']:.0f}% [{lo:.0f}, {hi:.0f}]"
    return f"{metric['mean']:.2f} [{metric['ci_lo']:.2f}, {metric['ci_hi']:.2f}]"
