"""Abstract environment interface shared by the simulator and the live system.

The agent only ever talks to an :class:`Env`; it never sees the simulator's hidden fault
state. Anything that is not on this interface is, by construction, unavailable to the agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from env.types import Action, ActionResult, Flow, LogLine, MetricSample, ServiceStatus


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
        """Observable health in [0, 1] computed from the latest metrics and status.

        This is deliberately *observable* information only: it is used by the safety layer
        to detect regressions after an action and by the verify step; it is not ground truth.
        """
        status = self.get_status()
        latest: dict[str, MetricSample] = {}
        for sample in self.get_metrics(window=30.0):
            latest[sample.service] = sample
        if not status:
            return 0.0
        total = 0.0
        for name, st in status.items():
            if st.state != "up":
                continue
            sample = latest.get(name)
            if sample is None:
                continue
            err = sample.values.get("error_rate") or 0.0
            p95 = sample.values.get("latency_p95") or 0.0
            lat_factor = 1.0 if p95 <= 400 else max(0.0, 1.0 - (p95 - 400) / 2000)
            total += max(0.0, 1.0 - min(1.0, err)) * lat_factor
        return total / len(status)
