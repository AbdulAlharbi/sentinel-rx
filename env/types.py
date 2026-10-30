"""Shared data types for environments, agents and the benchmark."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

METRIC_NAMES: tuple[str, ...] = (
    "latency_p50",
    "latency_p95",
    "error_rate",
    "cpu",
    "mem",
    "conn_count",
    "disk_used",
    "queue_depth",
)

ACTION_ALLOWLIST: tuple[str, ...] = (
    "restart_service",
    "rollback_config",
    "scale_connections",
    "clear_disk",
    "isolate_host",
    "block_egress",
    "flush_cache",
    "rate_limit_auth",
)

SECURITY_ACTIONS: frozenset[str] = frozenset({"isolate_host", "block_egress", "rate_limit_auth"})
CONTAINMENT_ACTIONS: frozenset[str] = frozenset({"isolate_host", "block_egress"})


@dataclass(frozen=True)
class Action:
    """A remediation action requested by an agent."""

    name: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        """Compact string form used in traces and scoring."""
        return f"{self.name}({self.target})"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


@dataclass
class ActionResult:
    """Outcome of applying an action to an environment."""

    action: Action
    applied: bool
    message: str
    time: float


@dataclass
class MetricSample:
    """One metrics observation for one service at one time."""

    t: float
    service: str
    values: dict[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {"t": self.t, "service": self.service, **self.values}


@dataclass
class LogLine:
    """A structured log line."""

    t: float
    service: str
    level: str
    msg: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


@dataclass
class Flow:
    """A network flow record (one connection attempt)."""

    t: float
    src: str
    dst: str
    nbytes: int
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


@dataclass
class ServiceStatus:
    """Externally visible status of a service."""

    service: str
    state: str  # up | down | isolated
    egress_blocked: bool = False
    rate_limited: bool = False
    config_version: str = "v1"
    pool_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


@dataclass(frozen=True)
class RootCause:
    """Ground-truth (or hypothesised) root cause."""

    service: str
    type: str

    def to_dict(self) -> dict[str, str]:
        """JSON-serialisable form."""
        return {"service": self.service, "type": self.type}
