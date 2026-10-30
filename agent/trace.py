"""Run trace: everything an agent observed, hypothesised, checked and did, as JSON."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Hypothesis:
    """A ranked root-cause hypothesis."""

    service: str
    type: str
    confidence: float
    status: str = "open"  # open | likely | confirmed | refuted
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return asdict(self)


@dataclass
class RunTrace:
    """Complete record of one agent episode."""

    config: str
    scenario_id: str
    seed_offset: int
    model: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_hypotheses: list[Hypothesis] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    claimed_recovered: bool = False
    escalated: bool = False
    verification: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_seconds: float = 0.0
    env_calls: int = 0
    wall_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def step(self, phase: str, **payload: Any) -> dict[str, Any]:
        """Append a step record and return it."""
        rec = {"i": len(self.steps), "phase": phase, **payload}
        self.steps.append(rec)
        return rec

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        d = asdict(self)
        d["final_hypotheses"] = [h.to_dict() for h in self.final_hypotheses]
        return d

    def save(self, path: Path) -> None:
        """Write JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1, default=str))
