"""Shared fixtures."""

from __future__ import annotations

import pytest

from env.sim.sim_env import SimEnv
from faults.base import FaultSpec
from scenarios.schema import Scenario


def make_scenario(
    faults: list[tuple[str, str, int, dict]], seed: int = 7, sid: str = "t"
) -> Scenario:
    """Build an ad-hoc scenario."""
    return Scenario(
        id=sid,
        split="test",
        seed=seed,
        faults=[FaultSpec(t, tgt, st, p) for t, tgt, st, p in faults],
    )


@pytest.fixture
def three_fault_env() -> SimEnv:
    """memory leak + db exhaustion + beacon, the demo scenario."""
    sc = make_scenario(
        [
            ("memory_leak", "api", 50, {"rate": 1.5}),
            ("db_conn_exhaustion", "db", 55, {}),
            ("c2_beacon", "worker", 45, {"interval": 40, "jitter": 0.2}),
        ]
    )
    return SimEnv(sc)
