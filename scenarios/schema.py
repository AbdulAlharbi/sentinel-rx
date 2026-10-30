"""Scenario schema: declarative YAML describing faults, ground truth and the recovery check."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from faults.base import FaultSpec

SCENARIO_ROOT = Path(__file__).resolve().parent


@dataclass
class Perturbation:
    """Optional observation-layer perturbation applied to a scenario."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    """One benchmark episode."""

    id: str
    split: str
    seed: int
    faults: list[FaultSpec]
    duration_ticks: int = 150
    agent_start_tick: int = 90
    tick_seconds: float = 10.0
    recovery_check: dict[str, Any] = field(
        default_factory=lambda: {"health_min": 0.9, "stable_ticks": 2}
    )
    perturbation: Perturbation | None = None
    base_id: str | None = None

    @property
    def n_faults(self) -> int:
        """Number of concurrent faults."""
        return len(self.faults)

    @property
    def fault_types(self) -> tuple[str, ...]:
        """Sorted multiset of fault types (used for split disjointness)."""
        return tuple(sorted(f.type for f in self.faults))

    @property
    def combo_key(self) -> str:
        """Key identifying the (type, target) combination."""
        return "+".join(sorted(f"{f.type}@{f.target}" for f in self.faults))

    def ground_truth(self) -> dict[str, Any]:
        """Ground-truth summary stored in YAML for readability (derived from faults)."""
        return {
            "root_causes": [{"service": f.target, "type": f.type} for f in self.faults],
            "has_security": any(
                f.type in ("c2_beacon", "credential_stuffing") for f in self.faults
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """YAML form."""
        d: dict[str, Any] = {
            "id": self.id,
            "split": self.split,
            "seed": self.seed,
            "duration_ticks": self.duration_ticks,
            "agent_start_tick": self.agent_start_tick,
            "tick_seconds": self.tick_seconds,
            "faults": [f.to_dict() for f in self.faults],
            "ground_truth": self.ground_truth(),
            "recovery_check": dict(self.recovery_check),
        }
        if self.perturbation is not None:
            d["perturbation"] = asdict(self.perturbation)
        if self.base_id is not None:
            d["base_id"] = self.base_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        """Parse YAML form."""
        pert = d.get("perturbation")
        return cls(
            id=d["id"],
            split=d["split"],
            seed=int(d["seed"]),
            faults=[
                FaultSpec(f["type"], f["target"], int(f["start"]), dict(f.get("params", {})))
                for f in d["faults"]
            ],
            duration_ticks=int(d.get("duration_ticks", 150)),
            agent_start_tick=int(d.get("agent_start_tick", 90)),
            tick_seconds=float(d.get("tick_seconds", 10.0)),
            recovery_check=dict(d.get("recovery_check", {"health_min": 0.9, "stable_ticks": 2})),
            perturbation=Perturbation(pert["name"], dict(pert.get("params", {}))) if pert else None,
            base_id=d.get("base_id"),
        )

    def save(self, path: Path) -> None:
        """Write YAML."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))

    @classmethod
    def load(cls, path: Path) -> Scenario:
        """Read YAML."""
        return cls.from_dict(yaml.safe_load(path.read_text()))


def load_split(split: str, root: Path = SCENARIO_ROOT) -> list[Scenario]:
    """Load all scenarios of a split (dev, heldout, perturbations/<name>)."""
    folder = root / split
    files = sorted(folder.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"no scenarios in {folder}; run `make scenarios`")
    return [Scenario.load(p) for p in files]
