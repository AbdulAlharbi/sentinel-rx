"""Safety layer between the planner and the environment.

* allowlist: only known actions on known services;
* evidence gate: contain (isolate / block egress) a host only on outbound-compromise evidence:
  a beacon finding when the detector runs, an open c2_beacon hypothesis only when it is off;
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
from env.base import Env, combine_health, service_health
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
        detector_enabled: bool = True,
    ) -> list[SafetyDecision]:
        """Validate and reorder a plan. Returns decisions in execution order.

        Containment needs *outbound-compromise* evidence: a beacon finding from the detector
        when the detector is running, otherwise an open ``c2_beacon`` hypothesis. An inbound
        attack (credential stuffing) never unlocks containment.
        """
        for st_name, st in status.items():
            if st in ("isolated",):
                self.contained.add(st_name)
        if not self.enabled:
            return [SafetyDecision(a, True, "safety layer disabled") for a in plan]
        security = self._security_targets(hypotheses, beacon_hosts)
        c2_hyps = {h.service for h in hypotheses if h.type == "c2_beacon" and h.status != "refuted"}
        containable = beacon_hosts if detector_enabled else (c2_hyps | beacon_hosts)
        ordered = sorted(plan, key=lambda a: 0 if a.name in CONTAINMENT_ACTIONS else 1)
        out: list[SafetyDecision] = []
        will_contain = set(self.contained)
        for a in ordered:
            if a.name not in ACTION_ALLOWLIST:
                out.append(SafetyDecision(a, False, "action not in allowlist"))
            elif a.target not in services:
                out.append(SafetyDecision(a, False, f"unknown service {a.target!r}"))
            elif a.name in CONTAINMENT_ACTIONS and a.target not in containable:
                out.append(
                    SafetyDecision(
                        a, False, "no beacon evidence on this host; refusing containment"
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
    scores = []
    for n in names:
        m = latest.get(n, {})
        if status[n].state != "up" or not m:
            scores.append(0.0)
            continue
        scores.append(service_health(m.get("error_rate") or 0.0, m.get("latency_p95") or 0.0))
    return combine_health(scores)
