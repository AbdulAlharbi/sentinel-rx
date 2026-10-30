"""Fault base class and the per-tick effect container used by the simulator.

A fault knows how to inject itself into the simulated world, what its symptoms look like,
its ground-truth root cause, which actions resolve it and which actions are harmful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from env.types import Action, Flow, LogLine, RootCause

if TYPE_CHECKING:
    from env.sim.world import World


@dataclass
class TickEffects:
    """Metric perturbations a fault applies to one service during one tick."""

    latency_mult: float = 1.0
    latency_add: float = 0.0
    error_add: float = 0.0
    cpu_add: float = 0.0
    mem_set: float | None = None
    conn_set: float | None = None
    conn_add: float = 0.0
    disk_set: float | None = None
    queue_add: float = 0.0
    down: bool = False


@dataclass
class FaultSpec:
    """Declarative description of one fault in a scenario file."""

    type: str
    target: str
    start: int
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """YAML/JSON form."""
        return {
            "type": self.type,
            "target": self.target,
            "start": self.start,
            "params": dict(self.params),
        }


class Fault:
    """Base class of all faults. Subclasses override the hooks they need."""

    type: str = "abstract"
    security: bool = False
    description: str = ""
    symptoms: str = ""

    def __init__(self, spec: FaultSpec, rng: np.random.Generator) -> None:
        self.spec = spec
        self.target = spec.target
        self.start = spec.start
        self.params = dict(spec.params)
        self.rng = rng
        self.resolved_at: int | None = None
        self.notes: list[str] = []

    # ---- metadata -------------------------------------------------------------------------
    @property
    def root_cause(self) -> RootCause:
        """Ground-truth root cause."""
        return RootCause(self.target, self.type)

    def correct_sequences(self) -> list[list[str]]:
        """Alternative action sequences (action keys) that resolve the fault."""
        raise NotImplementedError

    def harmful_actions(self, world: World) -> dict[str, str]:
        """Action key -> reason, for actions that are harmful *given current state*."""
        return {}

    # ---- lifecycle ------------------------------------------------------------------------
    def is_active(self, tick: int) -> bool:
        """True while the fault has started and is not resolved."""
        return tick >= self.start and self.resolved_at is None

    def resolve(self, tick: int, note: str) -> None:
        """Mark the fault as resolved."""
        self.resolved_at = tick
        self.notes.append(f"t={tick}: {note}")

    def tick(self, world: World, tick: int) -> None:
        """Apply this tick's effects, logs and flows to the world (only if active)."""

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """React to an action. Return a human-readable note if it changed the fault."""
        return None

    # ---- helpers --------------------------------------------------------------------------
    def log(
        self, world: World, tick: int, level: str, msg: str, service: str | None = None
    ) -> None:
        """Emit an extra log line for this tick."""
        world.extra_logs.append(LogLine(world.time_of(tick), service or self.target, level, msg))

    def flow(
        self, world: World, t: float, src: str, dst: str, nbytes: int, blocked: bool = False
    ) -> None:
        """Emit an extra flow record."""
        world.extra_flows.append(Flow(t, src, dst, nbytes, blocked))

    def to_dict(self) -> dict[str, Any]:
        """Serialisable summary (ground truth; never shown to the agent)."""
        return {
            "type": self.type,
            "target": self.target,
            "start": self.start,
            "params": self.params,
            "security": self.security,
            "resolved_at": self.resolved_at,
            "notes": self.notes,
        }
