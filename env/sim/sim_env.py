"""Deterministic discrete-time simulator of a small enterprise system.

One scenario runs in well under a second. All randomness flows from the scenario seed, so a
given (scenario, seed) pair always produces identical telemetry, which is what makes the
LLM record/replay cache reproducible.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from env.base import Env, combine_health, service_health
from env.sim.telemetry import normal_flows, normal_logs
from env.sim.world import EXTERNAL_ALLOWLIST, EXTERNAL_DEPS, World
from env.types import (
    ACTION_ALLOWLIST,
    Action,
    ActionResult,
    Flow,
    LogLine,
    MetricSample,
    ServiceStatus,
)
from faults.base import Fault
from faults.registry import make_fault
from scenarios.schema import Scenario

ACTION_TICKS = 3


class SimEnv(Env):
    """Simulated environment driven by a :class:`Scenario`."""

    def __init__(self, scenario: Scenario, seed_offset: int = 0) -> None:
        self.scenario = scenario
        self.seed_offset = seed_offset
        self.reset()

    # ---- lifecycle ------------------------------------------------------------------------
    def reset(self) -> None:
        """Rebuild the world from the scenario and run it up to the agent start tick."""
        seed = self.scenario.seed + 1000 * self.seed_offset
        self.world = World(seed=seed, tick_seconds=self.scenario.tick_seconds)
        self._tele_rng = np.random.default_rng(seed + 1)
        self.faults: list[Fault] = [
            make_fault(spec, np.random.default_rng(seed + 100 + i))
            for i, spec in enumerate(self.scenario.faults)
        ]
        self.tick = -1
        self._metrics: list[MetricSample] = []
        self._logs: list[LogLine] = []
        self._flows: list[Flow] = []
        self.action_log: list[dict[str, Any]] = []
        self.health_history: list[float] = []
        for _ in range(self.scenario.agent_start_tick + 1):
            self._step()

    def _step(self) -> None:
        self.tick += 1
        w = self.world
        w.begin_tick()
        for f in self.faults:
            f.tick(w, self.tick)
        metrics = w.compute_metrics(self.tick)
        t = w.time_of(self.tick)
        for svc, vals in metrics.items():
            self._metrics.append(MetricSample(t, svc, dict(vals)))
        self._logs.extend(normal_logs(w, self.tick, self._tele_rng))
        self._logs.extend(w.extra_logs)
        self._flows.extend(normal_flows(w, self.tick, self._tele_rng))
        self._flows.extend(w.extra_flows)
        self._sorted = False
        self.health_history.append(self.health_score())

    def _ensure_sorted(self) -> None:
        if not self._sorted:
            self._logs.sort(key=lambda line: line.t)
            self._flows.sort(key=lambda fl: fl.t)
            self._sorted = True

    def health_score(self) -> float:
        """Observable health from the latest tick (same formula as :meth:`Env.health_score`)."""
        scores = []
        for s in self.world.services:
            st = self.world.state[s]
            if st.state != "up" or self.tick < st.down_until or self.world.effects[s].down:
                scores.append(0.0)
                continue
            m = self.world.latest[s]
            scores.append(service_health(m["error_rate"], m["latency_p95"]))
        return combine_health(scores)

    def advance(self, ticks: int) -> None:
        """Advance the simulation by ``ticks``."""
        for _ in range(ticks):
            self._step()

    # ---- observation ----------------------------------------------------------------------
    def now(self) -> float:
        """Simulated time in seconds."""
        return self.world.time_of(self.tick)

    def list_services(self) -> list[str]:
        """Service names."""
        return list(self.world.services)

    def get_topology(self) -> dict[str, list[str]]:
        """Dependency graph."""
        return {k: list(v) for k, v in self.world.topology.items()}

    def get_status(self) -> dict[str, ServiceStatus]:
        """Per-service status."""
        out = {}
        for s in self.world.services:
            st = self.world.state[s]
            state = st.state
            if state == "up" and (self.tick < st.down_until or self.world.effects[s].down):
                state = "down"
            out[s] = ServiceStatus(
                service=s,
                state=state,
                egress_blocked=st.egress_blocked,
                rate_limited=self.world.rate_limited[s],
                config_version=self.world.config_version[s],
                pool_size=self.world.pool_size[s],
            )
        return out

    def get_metrics(self, window: float) -> list[MetricSample]:
        """Metric samples in the last ``window`` seconds."""
        cutoff = self.now() - window
        return [m for m in self._metrics if m.t >= cutoff]

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Log lines in the last ``window`` seconds."""
        self._ensure_sorted()
        cutoff = self.now() - window
        out = [line for line in self._logs if line.t >= cutoff]
        if service:
            out = [line for line in out if line.service == service]
        if grep:
            g = grep.lower()
            out = [line for line in out if g in line.msg.lower()]
        return out

    def get_flows(self, window: float) -> list[Flow]:
        """Flow records in the last ``window`` seconds."""
        self._ensure_sorted()
        cutoff = self.now() - window
        return [f for f in self._flows if f.t >= cutoff]

    def external_allowlist(self) -> set[str]:
        """Known-good external destinations."""
        return set(EXTERNAL_ALLOWLIST)

    def snapshot(self) -> dict[str, Any]:
        """Observable snapshot."""
        return {
            "t": self.now(),
            "tick": self.tick,
            "status": {k: v.to_dict() for k, v in self.get_status().items()},
            "latest": {k: dict(v) for k, v in self.world.latest.items()},
            "health": self.health_score(),
        }

    # ---- actuation ------------------------------------------------------------------------
    def wait(self, seconds: float) -> None:
        """Let time pass."""
        self.advance(max(1, math.ceil(seconds / self.world.tick_seconds)))

    def apply_action(self, action: Action) -> ActionResult:
        """Apply an allowlisted action, notify faults, and advance a few ticks."""
        t = self.now()
        if action.name not in ACTION_ALLOWLIST:
            return ActionResult(action, False, f"unknown action {action.name!r}", t)
        if action.target not in self.world.services:
            return ActionResult(action, False, f"unknown service {action.target!r}", t)
        w = self.world
        st = w.state[action.target]
        notes: list[str] = []
        harmful_now = {k: v for f in self.faults for k, v in f.harmful_actions(w).items()}
        harm_reason = harmful_now.get(action.key())
        state_before = [(f.resolved_at, getattr(f, "contained", None)) for f in self.faults]
        for f in self.faults:
            note = f.on_action(action, w, self.tick)
            if note:
                notes.append(note)
        state_after = [(f.resolved_at, getattr(f, "contained", None)) for f in self.faults]
        effective = state_before != state_after  # resolved or contained something
        match action.name:
            case "restart_service":
                st.down_until = self.tick + 2
                if st.state == "isolated" or st.egress_blocked:
                    # a restart after containment is a rebuild: the clean host rejoins the network
                    st.state = "up"
                    st.egress_blocked = False
                    notes.append("host rebuilt and rejoined the network")
                st.conn_mult = 1.0
                w.extra_logs.append(
                    LogLine(t, action.target, "INFO", "service restarted by operator")
                )
            case "rollback_config":
                w.config_version[action.target] = "v1"
            case "scale_connections":
                w.pool_size[action.target] = int(w.pool_size[action.target] * 2)
            case "isolate_host":
                st.state = "isolated"
                st.egress_blocked = True
            case "block_egress":
                st.egress_blocked = True
            case "rate_limit_auth":
                w.rate_limited[action.target] = True
            case "clear_disk" | "flush_cache":
                pass
        msg = "; ".join(notes) if notes else "applied (no visible effect)"
        self.action_log.append(
            {
                "tick": self.tick,
                "t": t,
                "action": action.to_dict(),
                "notes": notes,
                "harmful": harm_reason,
                "effective": effective,
            }
        )
        if action.name == "block_egress" and action.target in EXTERNAL_DEPS:
            dep = EXTERNAL_DEPS[action.target]
            self._logs.append(
                LogLine(t, action.target, "ERROR", f"{dep} unreachable: egress blocked by policy")
            )
            self._sorted = False
        self.advance(ACTION_TICKS)
        return ActionResult(action, True, msg, t)

    def undo_action(self, action: Action) -> bool:
        """Inverse of reversible actions (containment, rate limit, pool scaling)."""
        w = self.world
        if action.target not in w.services:
            return False
        st = w.state[action.target]
        match action.name:
            case "isolate_host":
                st.state = "up"
                st.egress_blocked = False
            case "block_egress":
                st.egress_blocked = False
            case "rate_limit_auth":
                w.rate_limited[action.target] = False
            case "scale_connections":
                w.pool_size[action.target] = max(1, w.pool_size[action.target] // 2)
            case _:
                return False
        for f in self.faults:
            if (
                f.type == "c2_beacon"
                and f.target == action.target
                and action.name in ("isolate_host", "block_egress")
            ):
                f.contained = False
        self.action_log.append(
            {
                "tick": self.tick,
                "t": self.now(),
                "action": action.to_dict(),
                "notes": ["undo"],
                "harmful": None,
                "undo": True,
            }
        )
        self.advance(1)
        return True

    # ---- hidden ground truth (runner / scorer only; NOT part of the agent interface) -----
    def gt_all_faults_resolved(self) -> bool:
        """True when every injected fault has been resolved."""
        return all(f.resolved_at is not None for f in self.faults)

    def gt_recovered(self) -> bool:
        """Ground-truth recovery: faults resolved, all services serving, health stable."""
        rc = self.scenario.recovery_check
        n = int(rc.get("stable_ticks", 2))
        hmin = float(rc.get("health_min", 0.9))
        if not self.gt_all_faults_resolved():
            return False
        if any(s.state != "up" for s in self.get_status().values()):
            return False
        recent = self.health_history[-n:]
        return len(recent) == n and all(h >= hmin for h in recent)

    def gt_summary(self) -> dict[str, Any]:
        """Serialisable hidden state for traces/scoring."""
        return {
            "faults": [f.to_dict() for f in self.faults],
            "recovered": self.gt_recovered(),
            "all_faults_resolved": self.gt_all_faults_resolved(),
            "actions": list(self.action_log),
            "final_tick": self.tick,
            "health": self.health_history[-1] if self.health_history else None,
        }
