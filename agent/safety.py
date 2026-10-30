"""Safety layer between the planner and the environment.

* allowlist: only known actions on known services;
* evidence gate: never contain (isolate / block egress) a host that no security hypothesis or
  beacon finding implicates;
* ordering: while a beacon hypothesis is open, a restart of that host is blocked until the host
  is contained (block_egress / isolate_host); plans are reordered so containment comes first;
* dry-run mode: log instead of act;
* transactional no-regression: observable health is measured before and after every action and
  the action is undone (``env.undo_action``) when health drops by more than a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.trace import Hypothesis
from env.base import Env
from env.types import ACTION_ALLOWLIST, CONTAINMENT_ACTIONS, Action

REGRESSION_DROP = 0.15


@dataclass
class SafetyDecision:
    """Outcome of screening one planned action."""

    action: Action
    allowed: bool
    reason: str = ""


@dataclass
class SafetyLayer:
    """Screens and executes plans; records every decision."""

    enabled: bool = True
    dry_run: bool = False
    contained: set[str] = field(default_factory=set)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def _security_targets(self, hypotheses: list[Hypothesis], beacon_hosts: set[str]) -> set[str]:
        open_hyps = {
            h.service for h in hypotheses if h.type == "c2_beacon" and h.status != "refuted"
        }
        return open_hyps | beacon_hosts

    def screen(
        self,
        plan: list[Action],
        hypotheses: list[Hypothesis],
        beacon_hosts: set[str],
        services: list[str],
        status: dict[str, str],
    ) -> list[SafetyDecision]:
        """Validate and reorder a plan. Returns decisions in execution order."""
        for st_name, st in status.items():
            if st in ("isolated",):
                self.contained.add(st_name)
        if not self.enabled:
            return [SafetyDecision(a, True, "safety layer disabled") for a in plan]
        security = self._security_targets(hypotheses, beacon_hosts)
        security_hyps = {
            h.service
            for h in hypotheses
            if h.type in ("c2_beacon", "credential_stuffing") and h.status != "refuted"
        }
        ordered = sorted(plan, key=lambda a: 0 if a.name in CONTAINMENT_ACTIONS else 1)
        out: list[SafetyDecision] = []
        will_contain = set(self.contained)
        for a in ordered:
            if a.name not in ACTION_ALLOWLIST:
                out.append(SafetyDecision(a, False, "action not in allowlist"))
            elif a.target not in services:
                out.append(SafetyDecision(a, False, f"unknown service {a.target!r}"))
            elif a.name == "isolate_host" and a.target not in security_hyps | beacon_hosts:
                out.append(
                    SafetyDecision(
                        a, False, "no security hypothesis implicates this host; refusing quarantine"
                    )
                )
            elif (
                a.name == "restart_service"
                and a.target in security
                and a.target not in will_contain
            ):
                out.append(
                    SafetyDecision(
                        a,
                        False,
                        "beacon hypothesis open: contain (block_egress/isolate) before restart",
                    )
                )
            else:
                if a.name in CONTAINMENT_ACTIONS:
                    will_contain.add(a.target)
                out.append(SafetyDecision(a, True))
        return out

    def execute(self, env: Env, decision: SafetyDecision) -> dict[str, Any]:
        """Apply an allowed action transactionally; return a trace record."""
        a = decision.action
        rec: dict[str, Any] = {
            "action": a.to_dict(),
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
        if not decision.allowed:
            rec["result"] = "blocked"
            self.decisions.append(rec)
            return rec
        if self.dry_run:
            rec["result"] = "dry-run"
            self.decisions.append(rec)
            return rec
        exclude = a.target if a.name in CONTAINMENT_ACTIONS else None
        before = observable_health(env, exclude)
        r = env.apply_action(a)
        after = observable_health(env, exclude)
        rec.update(
            {
                "applied": r.applied,
                "message": r.message,
                "health_before": round(before, 3),
                "health_after": round(after, 3),
            }
        )
        if r.applied and a.name in CONTAINMENT_ACTIONS:
            self.contained.add(a.target)
        if self.enabled and r.applied and after < before - REGRESSION_DROP:
            undone = env.undo_action(a)
            rec["result"] = "rolled_back" if undone else "regression (no undo available)"
            rec["health_after_undo"] = round(observable_health(env, exclude), 3)
            if undone and a.name in CONTAINMENT_ACTIONS:
                self.contained.discard(a.target)
        else:
            rec["result"] = "applied" if r.applied else "rejected"
        self.decisions.append(rec)
        return rec


def observable_health(env: Env, exclude: str | None = None) -> float:
    """Health from observable status + latest metrics, optionally ignoring one service."""
    status = env.get_status()
    latest = {}
    for s in env.get_metrics(30.0):
        latest[s.service] = s.values
    names = [n for n in status if n != exclude]
    if not names:
        return 1.0
    total = 0.0
    for n in names:
        if status[n].state != "up":
            continue
        m = latest.get(n, {})
        err = m.get("error_rate") or 0.0
        p95 = m.get("latency_p95") or 0.0
        lat = 1.0 if p95 <= 400 else max(0.0, 1.0 - (p95 - 400) / 2000)
        total += max(0.0, 1.0 - min(1.0, err)) * lat
    return total / len(names)
