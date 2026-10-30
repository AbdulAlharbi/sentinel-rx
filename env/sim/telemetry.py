"""Normal (healthy) telemetry: realistic log noise and background network flows."""

from __future__ import annotations

import numpy as np

from env.sim.world import EXTERNAL_ALLOWLIST, MONITOR, World
from env.types import Flow, LogLine

_NORMAL_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "gateway": [
        ("INFO", "GET /api/orders/{id} 200 {ms}ms"),
        ("INFO", "POST /api/checkout 201 {ms}ms"),
        ("INFO", "GET /healthz 200 1ms"),
        ("DEBUG", "tls session resumed for client {ip}"),
    ],
    "api": [
        ("INFO", "GET /orders/{id} 200 {ms}ms"),
        ("INFO", "cache hit for key order:{id}"),
        ("DEBUG", "db query SELECT ... WHERE id={id} took {ms}ms"),
        ("INFO", "GET /healthz 200 1ms"),
    ],
    "db": [
        ("INFO", "checkpoint complete: wrote {id} buffers"),
        ("DEBUG", "connection accepted from api ({ip})"),
        ("INFO", "autovacuum: table orders done"),
    ],
    "cache": [
        ("INFO", "hit ratio 0.{pct} over last 10s"),
        ("DEBUG", "evicted {id} expired keys"),
    ],
    "worker": [
        ("INFO", "job {id} completed in {ms}ms"),
        ("INFO", "polling queue: {small} jobs pending"),
        ("DEBUG", "heartbeat ok"),
    ],
}

_RANDOM_EXTERNAL_PAIRS: tuple[tuple[str, str, float], ...] = (("api", "payments.example", 1.5),)
_INTERNAL_PAIRS: tuple[tuple[str, str, float], ...] = (
    ("gateway", "api", 8.0),
    ("api", "db", 6.0),
    ("api", "cache", 10.0),
    ("worker", "db", 3.0),
    ("worker", "cache", 2.0),
)
UPDATE_POLL_INTERVAL = 300.0
HEALTH_CHECK_INTERVAL = 10.0


def _fill(template: str, rng: np.random.Generator) -> str:
    return (
        template.replace("{id}", str(int(rng.integers(1000, 999999))))
        .replace("{ms}", str(int(rng.integers(2, 180))))
        .replace("{ip}", f"10.0.{int(rng.integers(0, 8))}.{int(rng.integers(2, 250))}")
        .replace("{pct}", str(int(rng.integers(88, 99))))
        .replace("{small}", str(int(rng.integers(0, 9))))
    )


def normal_logs(world: World, tick: int, rng: np.random.Generator) -> list[LogLine]:
    """Three-ish benign log lines per service per tick."""
    t0 = world.time_of(tick)
    lines: list[LogLine] = []
    for svc in world.services:
        if world.state[svc].state != "up":
            continue
        n = int(rng.integers(2, 5))
        templates = _NORMAL_TEMPLATES[svc]
        for _ in range(n):
            level, tpl = templates[int(rng.integers(0, len(templates)))]
            lines.append(
                LogLine(
                    round(t0 + float(rng.uniform(0, world.tick_seconds)), 2),
                    svc,
                    level,
                    _fill(tpl, rng),
                )
            )
    return lines


def normal_flows(world: World, tick: int, rng: np.random.Generator) -> list[Flow]:
    """Background traffic: request flows, health checks and update polls."""
    t0 = world.time_of(tick)
    dt = world.tick_seconds
    flows: list[Flow] = []
    for src, dst, lam in _INTERNAL_PAIRS + _RANDOM_EXTERNAL_PAIRS:
        src_up = world.state[src].state == "up"
        dst_up = world.state[dst].state == "up" if dst in world.state else True
        if not (src_up and dst_up):
            continue
        if dst in EXTERNAL_ALLOWLIST and world.state[src].egress_blocked:
            continue
        n = int(rng.poisson(lam))
        for _ in range(n):
            flows.append(
                Flow(
                    round(t0 + float(rng.uniform(0, dt)), 3), src, dst, int(rng.integers(300, 5000))
                )
            )
    # legitimate periodic traffic: health checks (monitor -> svc) every 10s, tiny jitter
    for i, svc in enumerate(world.services):
        if world.state[svc].state == "up":
            flows.append(
                Flow(round(t0 + 0.2 * i + float(rng.normal(0, 0.03)), 3), MONITOR, svc, 120)
            )
    # update polling: every service polls the update mirror every 300s (+/- 5s)
    for i, svc in enumerate(world.services):
        phase = (i * 61.0) % UPDATE_POLL_INTERVAL
        k = np.ceil((t0 - phase) / UPDATE_POLL_INTERVAL)
        poll_t = phase + UPDATE_POLL_INTERVAL * k
        if t0 <= poll_t < t0 + dt and world.state[svc].state == "up":
            if not world.state[svc].egress_blocked:
                flows.append(
                    Flow(round(poll_t + float(rng.uniform(-3, 3)), 3), svc, "updates.internal", 850)
                )
    return flows
