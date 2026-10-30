"""Runbook baseline: static if-symptom-then-action rules, no LLM.

This is what a conventional auto-remediation runbook does: threshold rules on the latest
metrics and a few log greps. It loops until no rule fires, then declares recovery.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent.base import Agent
from agent.trace import Hypothesis, RunTrace
from env.base import Env
from env.types import Action

OBS_WINDOW = 120.0
LOG_WINDOW = 900.0
LATENCY_SLO_MS = 500.0


@dataclass
class _Obs:
    latest: dict[str, dict[str, float | None]]
    status: dict[str, str]
    pool: dict[str, int]
    logs: list
    flows: list
    services: list[str]


def _observe(env: Env) -> _Obs:
    latest: dict[str, dict[str, float | None]] = {}
    for s in env.get_metrics(OBS_WINDOW):
        latest[s.service] = s.values
    status = {k: v.state for k, v in env.get_status().items()}
    pool = {k: v.pool_size for k, v in env.get_status().items()}
    return _Obs(
        latest,
        status,
        pool,
        env.get_logs(None, OBS_WINDOW),
        env.get_flows(OBS_WINDOW),
        env.list_services(),
    )


def _rules(env: Env, obs: _Obs) -> list[tuple[Action, Hypothesis]]:
    """Return (action, hypothesis) pairs for every rule that fires."""
    fired: list[tuple[Action, Hypothesis]] = []
    known = set(obs.services) | env.external_allowlist() | {"monitor"}
    for svc in obs.services:
        m = obs.latest.get(svc, {})
        err = m.get("error_rate") or 0.0
        if obs.status.get(svc) == "down":
            fired.append(
                (
                    Action("restart_service", svc),
                    Hypothesis(svc, "process_crash", 0.9, "likely", "status down"),
                )
            )
            continue
        if (m.get("mem") or 0.0) > 85:
            fired.append(
                (
                    Action("restart_service", svc),
                    Hypothesis(svc, "memory_leak", 0.8, "likely", "mem > 85%"),
                )
            )
        if obs.pool.get(svc) and (m.get("conn_count") or 0.0) >= 0.95 * obs.pool[svc]:
            fired.append(
                (
                    Action("scale_connections", svc),
                    Hypothesis(svc, "db_conn_exhaustion", 0.8, "likely", "pool saturated"),
                )
            )
        if (m.get("disk_used") or 0.0) > 95:
            fired.append(
                (
                    Action("clear_disk", svc),
                    Hypothesis(svc, "disk_full", 0.8, "likely", "disk > 95%"),
                )
            )
        svc_logs = [line for line in obs.logs if line.service == svc]
        if err > 0.2 and any("config reload" in line.msg for line in svc_logs):
            fired.append(
                (
                    Action("rollback_config", svc),
                    Hypothesis(
                        svc, "bad_config_deploy", 0.7, "likely", "errors after config reload"
                    ),
                )
            )
        if any("WRONGTYPE" in line.msg for line in svc_logs):
            fired.append(
                (
                    Action("flush_cache", svc),
                    Hypothesis(svc, "cache_failure", 0.7, "likely", "cache errors"),
                )
            )
        if (m.get("latency_p95") or 0.0) > LATENCY_SLO_MS and err < 0.1:
            fired.append(
                (
                    Action("restart_service", svc),
                    Hypothesis(
                        svc, "latency_injection", 0.5, "likely", "p95 above SLO without errors"
                    ),
                )
            )
        if sum("authentication failed" in line.msg for line in svc_logs) > 20:
            fired.append(
                (
                    Action("rate_limit_auth", svc),
                    Hypothesis(svc, "credential_stuffing", 0.7, "likely", "auth failure burst"),
                )
            )
    # naive egress rule: any outbound flow to an unknown destination
    for f in obs.flows:
        if f.src in obs.services and f.dst not in known:
            fired.append(
                (
                    Action("block_egress", f.src),
                    Hypothesis(f.src, "c2_beacon", 0.5, "likely", f"unknown egress to {f.dst}"),
                )
            )
            break
    return fired


class RunbookAgent(Agent):
    """Deterministic rule-based remediation loop."""

    name = "runbook"

    def run(self, env: Env, budget: int = 8) -> RunTrace:
        """Fire rules until none fire or the budget is exhausted."""
        t0 = time.perf_counter()
        trace = RunTrace(config=self.name, scenario_id="", seed_offset=0)
        done: set[str] = set()
        hyps: dict[str, Hypothesis] = {}
        for i in range(budget):
            obs = _observe(env)
            fired = [(a, h) for a, h in _rules(env, obs) if a.key() not in done]
            trace.step("iteration", i=i, fired=[a.key() for a, _ in fired])
            if not fired:
                trace.claimed_recovered = env.health_score() > 0.9
                trace.escalated = not trace.claimed_recovered
                break
            for action, h in fired:
                hyps.setdefault(f"{h.service}:{h.type}", h)
                r = env.apply_action(action)
                done.add(action.key())
                trace.actions.append(
                    {
                        "action": action.to_dict(),
                        "applied": r.applied,
                        "message": r.message,
                        "t": r.time,
                    }
                )
        else:
            trace.escalated = True
        trace.final_hypotheses = sorted(hyps.values(), key=lambda h: -h.confidence)
        trace.wall_seconds = time.perf_counter() - t0
        return trace
