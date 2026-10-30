"""Hidden state of the simulated enterprise: services, dependency graph, fault effects.

Nothing in this module is visible to the agent directly; :class:`env.sim.sim_env.SimEnv`
exposes only what the :class:`env.base.Env` interface allows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from env.types import METRIC_NAMES, Flow, LogLine
from faults.base import TickEffects

SERVICES: tuple[str, ...] = ("gateway", "api", "db", "cache", "worker")

#: service -> services it calls (edges point downstream).
TOPOLOGY: dict[str, list[str]] = {
    "gateway": ["api"],
    "api": ["db", "cache"],
    "worker": ["db", "cache"],
    "db": [],
    "cache": [],
}

#: Baseline (healthy) metric values per service.
BASELINES: dict[str, dict[str, float]] = {
    "gateway": {
        "latency_p50": 80,
        "latency_p95": 200,
        "error_rate": 0.005,
        "cpu": 30,
        "mem": 40,
        "conn_count": 200,
        "disk_used": 30,
        "queue_depth": 0,
    },
    "api": {
        "latency_p50": 60,
        "latency_p95": 150,
        "error_rate": 0.005,
        "cpu": 40,
        "mem": 45,
        "conn_count": 80,
        "disk_used": 35,
        "queue_depth": 0,
    },
    "db": {
        "latency_p50": 5,
        "latency_p95": 20,
        "error_rate": 0.001,
        "cpu": 35,
        "mem": 60,
        "conn_count": 40,
        "disk_used": 55,
        "queue_depth": 0,
    },
    "cache": {
        "latency_p50": 1,
        "latency_p95": 3,
        "error_rate": 0.001,
        "cpu": 15,
        "mem": 50,
        "conn_count": 60,
        "disk_used": 10,
        "queue_depth": 0,
    },
    "worker": {
        "latency_p50": 200,
        "latency_p95": 500,
        "error_rate": 0.005,
        "cpu": 50,
        "mem": 40,
        "conn_count": 10,
        "disk_used": 20,
        "queue_depth": 5,
    },
}

POOL_SIZES: dict[str, int] = {"gateway": 1000, "api": 400, "db": 100, "cache": 500, "worker": 50}

EXTERNAL_ALLOWLIST: frozenset[str] = frozenset({"updates.internal", "payments.example"})
#: Services whose normal operation needs an external dependency (blocking their egress hurts).
EXTERNAL_DEPS: dict[str, str] = {"api": "payments.example"}
EGRESS_BLOCK_ERROR = 0.3
MONITOR = "monitor"


@dataclass
class ServiceState:
    """Mutable hidden state of one service."""

    state: str = "up"
    egress_blocked: bool = False
    down_until: int = -1  # restart transient
    conn_mult: float = 1.0


@dataclass
class World:
    """Hidden simulator state and per-tick scratch space for faults."""

    seed: int
    tick_seconds: float = 10.0
    services: tuple[str, ...] = SERVICES
    topology: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in TOPOLOGY.items()}
    )
    rng: np.random.Generator = field(init=False)
    state: dict[str, ServiceState] = field(init=False)
    effects: dict[str, TickEffects] = field(init=False)
    extra_logs: list[LogLine] = field(default_factory=list)
    extra_flows: list[Flow] = field(default_factory=list)
    config_version: dict[str, str] = field(init=False)
    pool_size: dict[str, int] = field(init=False)
    rate_limited: dict[str, bool] = field(init=False)
    latest: dict[str, dict[str, float]] = field(init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        self.state = {s: ServiceState() for s in self.services}
        self.effects = {s: TickEffects() for s in self.services}
        self.config_version = {s: "v1" for s in self.services}
        self.pool_size = dict(POOL_SIZES)
        self.rate_limited = {s: False for s in self.services}
        self.latest = {s: dict(BASELINES[s]) for s in self.services}

    # ---- helpers used by faults ----------------------------------------------------------
    def time_of(self, tick: int) -> float:
        """Simulated time (s) at the start of a tick."""
        return tick * self.tick_seconds

    def baseline(self, service: str) -> dict[str, float]:
        """Healthy metric baseline for a service."""
        return BASELINES[service]

    def dependents_of(self, service: str) -> list[str]:
        """Services that call ``service``."""
        return [s for s, deps in self.topology.items() if service in deps]

    def status_state(self, service: str) -> str:
        """Current state string (up/down/isolated)."""
        return self.state[service].state

    def topo_order(self) -> list[str]:
        """Dependencies before dependents."""
        order: list[str] = []
        seen: set[str] = set()

        def visit(s: str) -> None:
            if s in seen:
                return
            seen.add(s)
            for d in self.topology.get(s, []):
                visit(d)
            order.append(s)

        for s in self.services:
            visit(s)
        return order

    def begin_tick(self) -> None:
        """Reset per-tick scratch effects."""
        self.effects = {s: TickEffects() for s in self.services}
        self.extra_logs = []
        self.extra_flows = []

    # ---- metric computation ---------------------------------------------------------------
    def compute_metrics(self, tick: int) -> dict[str, dict[str, float]]:
        """Combine baselines, fault effects and dependency propagation into this tick's metrics."""
        out: dict[str, dict[str, float]] = {}
        for svc in self.topo_order():
            base = BASELINES[svc]
            eff = self.effects[svc]
            st = self.state[svc]
            noise = 1.0 + self.rng.normal(0, 0.05, size=len(METRIC_NAMES))
            m = {k: float(base[k]) * float(n) for k, n in zip(METRIC_NAMES, noise, strict=True)}
            m["error_rate"] = max(0.0, float(base["error_rate"]) * float(abs(noise[2])))
            down = eff.down or st.state != "up" or tick < st.down_until
            if down:
                m.update(
                    {
                        "latency_p50": 0.0,
                        "latency_p95": 0.0,
                        "error_rate": 1.0,
                        "cpu": 0.0,
                        "conn_count": 0.0,
                        "queue_depth": m["queue_depth"],
                    }
                )
                if eff.mem_set is not None:
                    m["mem"] = eff.mem_set
                if eff.disk_set is not None:
                    m["disk_used"] = eff.disk_set
                out[svc] = m
                continue
            # propagate from dependencies
            for dep in self.topology.get(svc, []):
                dm = out[dep]
                dbase = BASELINES[dep]
                if dm["error_rate"] >= 0.99 or self.state[dep].state != "up":
                    m["error_rate"] += 0.9
                    m["latency_p95"] += 800.0
                    m["latency_p50"] += 300.0
                else:
                    m["error_rate"] += 0.7 * max(0.0, dm["error_rate"] - dbase["error_rate"])
                    m["latency_p50"] += 0.8 * max(0.0, dm["latency_p50"] - dbase["latency_p50"])
                    m["latency_p95"] += 0.8 * max(0.0, dm["latency_p95"] - dbase["latency_p95"])
            if st.egress_blocked and svc in EXTERNAL_DEPS:
                # legitimate outbound calls (payments) now fail: blocking egress is not free
                m["error_rate"] += EGRESS_BLOCK_ERROR
            m["latency_p50"] = m["latency_p50"] * eff.latency_mult + eff.latency_add
            m["latency_p95"] = m["latency_p95"] * eff.latency_mult + eff.latency_add * 1.3
            m["error_rate"] = min(1.0, m["error_rate"] + eff.error_add)
            m["cpu"] = min(100.0, m["cpu"] + eff.cpu_add)
            if eff.mem_set is not None:
                m["mem"] = eff.mem_set
            if eff.conn_set is not None:
                m["conn_count"] = eff.conn_set
            m["conn_count"] = max(0.0, m["conn_count"] * st.conn_mult + eff.conn_add)
            if eff.disk_set is not None:
                m["disk_used"] = eff.disk_set
            m["queue_depth"] = max(0.0, m["queue_depth"] + eff.queue_add)
            # a slow dependency makes the worker queue grow
            if svc == "worker" and m["latency_p95"] > 2 * base["latency_p95"]:
                m["queue_depth"] += 40.0
            out[svc] = {k: round(v, 4) for k, v in m.items()}
        self.latest = out
        return out
