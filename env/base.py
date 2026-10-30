"""Abstract environment interface shared by the simulator and the live system.

The agent only ever talks to an :class:`Env`; it never sees the simulator's hidden fault
state. Anything that is not on this interface is, by construction, unavailable to the agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from env.types import Action, ActionResult, Flow, LogLine, MetricSample, ServiceStatus


def service_health(error_rate: float, latency_p95: float) -> float:
    """Per-service health in [0, 1] from error rate and p95 latency."""
    lat_factor = 1.0 if latency_p95 <= 400 else max(0.0, 1.0 - (latency_p95 - 400) / 2000)
    return max(0.0, 1.0 - min(1.0, error_rate)) * lat_factor


def combine_health(scores: list[float]) -> float:
    """System health = 0.5 * mean + 0.5 * min of per-service scores."""
    if not scores:
        return 0.0
    return 0.5 * (sum(scores) / len(scores)) + 0.5 * min(scores)


class Env(ABC):
    """Observation + actuation interface of a running (possibly failing) system."""

    @abstractmethod
    def list_services(self) -> list[str]:
        """Names of all managed services."""

    @abstractmethod
    def get_topology(self) -> dict[str, list[str]]:
        """Dependency graph: service -> services it calls."""

    @abstractmethod
    def get_status(self) -> dict[str, ServiceStatus]:
        """Current externally visible status per service."""

    @abstractmethod
    def get_metrics(self, window: float) -> list[MetricSample]:
        """All metric samples in the last ``window`` seconds, oldest first."""

    @abstractmethod
    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Log lines in the last ``window`` seconds, optionally filtered by service / substring."""

    @abstractmethod
    def get_flows(self, window: float) -> list[Flow]:
        """Network flow records in the last ``window`` seconds."""

    @abstractmethod
    def apply_action(self, action: Action) -> ActionResult:
        """Apply a remediation action; the environment advances time so effects become visible."""

    def undo_action(self, action: Action) -> bool:
        """Best-effort inverse of an action (used by the safety layer's rollback). False = none."""
        return False

    @abstractmethod
    def wait(self, seconds: float) -> None:
        """Let the system run for ``seconds`` without acting."""

    @abstractmethod
    def reset(self) -> None:
        """Reset the environment to its initial state."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Serialisable snapshot of observable state (used for rollback decisions and traces)."""

    @abstractmethod
    def now(self) -> float:
        """Current environment time in seconds."""

    def external_allowlist(self) -> set[str]:
        """Known-good external destinations (update mirrors, payment API, ...)."""
        return set()

    def health_score(self) -> float:
        """Observable health in [0, 1] from the latest metrics and status.

        ``0.5 * mean + 0.5 * min`` over per-service scores, so one badly degraded service
        cannot hide behind four healthy ones. Observable information only: it is used by the
        safety layer to detect regressions and by naive baselines to claim recovery; it is never
        ground truth.
        """
        status = self.get_status()
        latest: dict[str, MetricSample] = {}
        for sample in self.get_metrics(window=30.0):
            latest[sample.service] = sample
        if not status:
            return 0.0
        scores = []
        for name, st in status.items():
            sample = latest.get(name)
            if st.state != "up" or sample is None:
                scores.append(0.0)
                continue
            err = sample.values.get("error_rate") or 0.0
            p95 = sample.values.get("latency_p95") or 0.0
            scores.append(service_health(err, p95))
        return combine_health(scores)
