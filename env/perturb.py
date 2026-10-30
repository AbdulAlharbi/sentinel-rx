"""Observation-layer perturbations used by the robustness suite.

Each perturbation wraps an :class:`Env` and distorts what the agent *sees* (never the hidden
ground truth), so the same scenario can be scored against the same answer key.
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np

from env.base import Env
from env.types import METRIC_NAMES, Action, ActionResult, Flow, LogLine, MetricSample, ServiceStatus

RENAME_MAP: dict[str, str] = {
    "gateway": "edge-proxy-01",
    "api": "orders-svc",
    "db": "pg-primary",
    "cache": "redis-main",
    "worker": "batch-runner",
}

_NOISE_TEMPLATES = (
    ("INFO", "metrics batch flushed ({n} points)"),
    ("DEBUG", "trace id {hex} sampled"),
    ("WARN", "slow log rotation took {n}ms"),
    ("ERROR", "failed to send metrics batch to collector (retrying)"),
    ("INFO", "feature flag refresh ok ({n} flags)"),
    ("DEBUG", "gc minor collection {n}ms"),
)


class PerturbedEnv(Env):
    """Base wrapper delegating everything to an inner env."""

    def __init__(self, inner: Env, seed: int) -> None:
        self.inner = inner
        self.rng = np.random.default_rng(seed + 777)

    def list_services(self) -> list[str]:
        """Delegate."""
        return self.inner.list_services()

    def get_topology(self) -> dict[str, list[str]]:
        """Delegate."""
        return self.inner.get_topology()

    def get_status(self) -> dict[str, ServiceStatus]:
        """Delegate."""
        return self.inner.get_status()

    def get_metrics(self, window: float) -> list[MetricSample]:
        """Delegate."""
        return self.inner.get_metrics(window)

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Delegate."""
        return self.inner.get_logs(service, window, grep)

    def get_flows(self, window: float) -> list[Flow]:
        """Delegate."""
        return self.inner.get_flows(window)

    def apply_action(self, action: Action) -> ActionResult:
        """Delegate."""
        return self.inner.apply_action(action)

    def wait(self, seconds: float) -> None:
        """Delegate."""
        self.inner.wait(seconds)

    def undo_action(self, action: Action) -> bool:
        """Delegate."""
        return self.inner.undo_action(action)

    def reset(self) -> None:
        """Delegate."""
        self.inner.reset()

    def snapshot(self) -> dict[str, Any]:
        """Delegate."""
        return self.inner.snapshot()

    def now(self) -> float:
        """Delegate."""
        return self.inner.now()

    def external_allowlist(self) -> set[str]:
        """Delegate."""
        return self.inner.external_allowlist()

    def health_score(self) -> float:
        """Delegate."""
        return self.inner.health_score()

    def __getattr__(self, name: str) -> Any:
        # hidden ground-truth helpers (gt_*) stay reachable for the runner only
        return getattr(self.inner, name)


class NoisyLogs(PerturbedEnv):
    """Drop 30% of real log lines and add twice as many irrelevant ones (incl. spurious ERRORs)."""

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Noisy view of the logs (deterministic per query)."""
        real = self.inner.get_logs(service, window, None)
        rng = np.random.default_rng(int(self.inner.now() * 10) + len(real))
        keep = [line for line in real if rng.random() > 0.3]
        services = [service] if service else self.list_services()
        n_noise = int(len(real) * 0.8)
        t_now = self.now()
        noise = []
        for _ in range(n_noise):
            level, tpl = _NOISE_TEMPLATES[int(rng.integers(0, len(_NOISE_TEMPLATES)))]
            msg = tpl.replace("{n}", str(int(rng.integers(1, 900)))).replace(
                "{hex}", f"{int(rng.integers(0, 2**32)):08x}"
            )
            noise.append(
                LogLine(
                    round(t_now - float(rng.uniform(0, window)), 2),
                    services[int(rng.integers(0, len(services)))],
                    level,
                    msg,
                )
            )
        out = sorted(keep + noise, key=lambda line: line.t)
        if grep:
            g = grep.lower()
            out = [line for line in out if g in line.msg.lower()]
        return out


class MissingMetrics(PerturbedEnv):
    """A fixed random third of (service, metric) series is unavailable (None)."""

    def __init__(self, inner: Env, seed: int) -> None:
        super().__init__(inner, seed)
        pairs = [(s, m) for s in inner.list_services() for m in METRIC_NAMES]
        idx = self.rng.permutation(len(pairs))[: len(pairs) // 3]
        self.missing = {pairs[i] for i in idx}

    def get_metrics(self, window: float) -> list[MetricSample]:
        """Metrics with holes."""
        out = []
        for s in self.inner.get_metrics(window):
            vals = {k: (None if (s.service, k) in self.missing else v) for k, v in s.values.items()}
            out.append(MetricSample(s.t, s.service, vals))
        return out


class RenamedServices(PerturbedEnv):
    """Services carry unfamiliar production names; actions are mapped back."""

    def __init__(self, inner: Env, seed: int) -> None:
        super().__init__(inner, seed)
        self.fwd = {s: RENAME_MAP.get(s, s) for s in inner.list_services()}
        self.back = {v: k for k, v in self.fwd.items()}

    def _f(self, name: str) -> str:
        return self.fwd.get(name, name)

    def list_services(self) -> list[str]:
        """Renamed services."""
        return [self._f(s) for s in self.inner.list_services()]

    def get_topology(self) -> dict[str, list[str]]:
        """Renamed topology."""
        return {self._f(k): [self._f(x) for x in v] for k, v in self.inner.get_topology().items()}

    def get_status(self) -> dict[str, ServiceStatus]:
        """Renamed status."""
        out = {}
        for k, st in self.inner.get_status().items():
            d = st.to_dict()
            d["service"] = self._f(k)
            out[self._f(k)] = ServiceStatus(**d)
        return out

    def get_metrics(self, window: float) -> list[MetricSample]:
        """Renamed metrics."""
        return [
            MetricSample(s.t, self._f(s.service), s.values) for s in self.inner.get_metrics(window)
        ]

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Renamed logs."""
        inner_service = self.back.get(service, service) if service else None
        return [
            LogLine(line.t, self._f(line.service), line.level, line.msg)
            for line in self.inner.get_logs(inner_service, window, grep)
        ]

    def get_flows(self, window: float) -> list[Flow]:
        """Renamed flows."""
        return [
            Flow(f.t, self._f(f.src), self._f(f.dst), f.nbytes)
            for f in self.inner.get_flows(window)
        ]

    def apply_action(self, action: Action) -> ActionResult:
        """Map the target back to the inner name."""
        inner_action = Action(
            action.name, self.back.get(action.target, action.target), action.params
        )
        r = self.inner.apply_action(inner_action)
        return ActionResult(action, r.applied, r.message, r.time)

    def undo_action(self, action: Action) -> bool:
        """Map the target back to the inner name."""
        return self.inner.undo_action(
            Action(action.name, self.back.get(action.target, action.target), action.params)
        )

    def snapshot(self) -> dict[str, Any]:
        """Renamed snapshot."""
        snap = self.inner.snapshot()
        for key in ("status", "latest"):
            if key in snap:
                snap[key] = {self._f(k): v for k, v in snap[key].items()}
        return snap


def _stable_salt(src: str, dst: str) -> int:
    """Per-pair salt that is identical in every process (``hash()`` of a str is randomised)."""
    return zlib.crc32(f"{src}->{dst}".encode()) & 0xFFFF


class TimestampJitter(PerturbedEnv):
    """Flow and log timestamps carry +/- 2 s Gaussian jitter (clock skew between collectors)."""

    def _jit(self, t: float, salt: int) -> float:
        rng = np.random.default_rng(int(t * 1000) + salt)
        return round(t + float(rng.normal(0, 2.0)), 3)

    def get_flows(self, window: float) -> list[Flow]:
        """Jittered flows."""
        out = [
            Flow(self._jit(f.t, _stable_salt(f.src, f.dst)), f.src, f.dst, f.nbytes)
            for f in self.inner.get_flows(window)
        ]
        return sorted(out, key=lambda f: f.t)

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Jittered logs."""
        out = [
            LogLine(self._jit(line.t, 3), line.service, line.level, line.msg)
            for line in self.inner.get_logs(service, window, grep)
        ]
        return sorted(out, key=lambda line: line.t)


PERTURBATION_CLASSES: dict[str, type[PerturbedEnv]] = {
    "noisy_logs": NoisyLogs,
    "missing_metrics": MissingMetrics,
    "renamed_services": RenamedServices,
    "timestamp_jitter": TimestampJitter,
}


def wrap(env: Env, name: str | None, seed: int) -> Env:
    """Apply a named perturbation (``unseen_fault`` is a scenario-level change, so no wrapper)."""
    if name is None or name == "unseen_fault":
        return env
    return PERTURBATION_CLASSES[name](env, seed)
