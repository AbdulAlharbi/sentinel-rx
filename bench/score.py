"""Per-scenario scoring of a run trace against hidden ground truth.

All metrics are defined here, in one place, so docs and the site can cite them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from env.types import RootCause
from faults.registry import make_fault
from scenarios.schema import Scenario

SECURITY_TYPES = ("c2_beacon", "credential_stuffing")


def _hyp_matches(h: dict[str, Any], gt: RootCause) -> bool:
    return h["service"] == gt.service and h["type"] == gt.type


def score_run(
    trace: dict[str, Any],
    scenario: Scenario,
    gt: dict[str, Any],
    name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute all per-scenario metrics.

    Args:
        trace: ``RunTrace.to_dict()``.
        scenario: the scenario that was run.
        gt: ``SimEnv.gt_summary()`` taken after the run.
        name_map: inner->visible service renames (perturbation), so hypotheses in the agent's
            vocabulary are compared with ground truth in the simulator's vocabulary.
    """
    fwd = name_map or {}
    truths = [RootCause(fwd.get(f.target, f.target), f.type) for f in scenario.faults]
    hyps = [h for h in trace.get("final_hypotheses", []) if h.get("status") != "refuted"]
    ranked = sorted(hyps, key=lambda h: -float(h.get("confidence", 0.0)))

    top1 = bool(ranked) and any(_hyp_matches(ranked[0], g) for g in truths)
    top3 = any(_hyp_matches(h, g) for h in ranked[:3] for g in truths)
    diagnosed = [h for h in ranked if h.get("status") in ("confirmed", "likely")] or ranked[
        : len(truths)
    ]
    matched = {g for g in truths if any(_hyp_matches(h, g) for h in diagnosed)}
    localized = {
        g for g in truths if any(h["service"] == g.service for h in ranked[: max(3, len(truths))])
    }
    fault_recall = len(matched) / len(truths) if truths else 1.0
    precision = len(matched) / len(diagnosed) if diagnosed else 0.0

    gt_actions = gt.get("actions", [])
    applied_keys = [
        f"{a['action']['name']}({fwd.get(a['action']['target'], a['action']['target'])})"
        for a in gt_actions
    ]
    correct_keys: set[str] = set()
    fault_infos = {(f["target"], f["type"]): f for f in gt.get("faults", [])}
    for spec in scenario.faults:
        f = make_fault(spec, np.random.default_rng(0))
        for seq in f.correct_sequences():
            for key in seq:
                name, tgt = key[:-1].split("(")
                correct_keys.add(f"{name}({fwd.get(tgt, tgt)})")
    harmful = sum(1 for a in gt_actions if a.get("harmful"))
    compromised = {fwd.get(f.target, f.target) for f in scenario.faults if f.type == "c2_beacon"}
    for a in gt_actions:
        tgt = fwd.get(a["action"]["target"], a["action"]["target"])
        if a["action"]["name"] == "isolate_host" and tgt not in compromised:
            harmful += 1
    unnecessary = sum(1 for k in applied_keys if k not in correct_keys)

    beacons = [f for f in gt.get("faults", []) if f["type"] == "c2_beacon"]
    isolated_before_restart: bool | None = None
    if beacons:
        isolated_before_restart = all(b.get("reinfections", 0) == 0 for b in beacons)

    recovered = bool(gt.get("recovered", False))
    claimed = bool(trace.get("claimed_recovered", False))
    return {
        "scenario_id": scenario.id,
        "n_faults": scenario.n_faults,
        "has_security": any(f.type in SECURITY_TYPES for f in scenario.faults),
        "top1": float(top1),
        "top3": float(top3),
        "fault_recall": fault_recall,
        "fault_precision": precision,
        "localized": len(localized) / len(truths) if truths else 1.0,
        "recovered": float(recovered),
        "false_recovery": float(claimed and not recovered),
        "claimed_recovered": float(claimed),
        "escalated": float(trace.get("escalated", False)),
        "harmful_actions": float(harmful),
        "unnecessary_actions": float(unnecessary),
        "n_actions": float(len(gt_actions)),
        "isolated_before_restart": None
        if isolated_before_restart is None
        else float(isolated_before_restart),
        "steps": float(len([s for s in trace.get("steps", []) if s.get("phase") == "iteration"])),
        "time_to_recovery_s": trace.get("time_to_recovery_s"),
        "llm_calls": float(trace.get("llm_calls", 0)),
        "tokens": float(trace.get("tokens_in", 0) + trace.get("tokens_out", 0)),
        "tokens_in": float(trace.get("tokens_in", 0)),
        "tokens_out": float(trace.get("tokens_out", 0)),
        "llm_seconds": float(trace.get("llm_seconds", 0.0)),
        "wall_seconds": float(trace.get("wall_seconds", 0.0)),
        "env_calls": float(trace.get("env_calls", 0)),
        "fault_infos": list(fault_infos.keys()),
    }
