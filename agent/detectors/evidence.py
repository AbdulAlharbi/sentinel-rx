"""Evidence builder: runs every detector and produces the compact ranked evidence list.

This is the *only* thing the LLM sees about the system in the Observe step: never raw
telemetry, always a short, ranked, structured summary. The builder also remembers the
pre-incident baseline from its first observation so that the verification step can compare
post-remediation telemetry against *healthy* history rather than against the incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.detectors.beacon import BeaconDetector, BeaconFinding
from agent.detectors.logs import (
    LogBaseline,
    LogSignature,
    detect_log_signatures,
    log_baseline,
    service_log_scores,
)
from agent.detectors.metrics import (
    MetricAnomaly,
    detect_metric_anomalies,
    service_anomaly_scores,
    split_baseline,
)
from agent.detectors.ranker import RankedService, rank_services
from env.base import Env
from env.types import MetricSample

WINDOW = 900.0
TRANSIENT_S = 20.0


@dataclass
class Evidence:
    """Ranked, compact evidence for one observation."""

    t: float
    status: dict[str, str]
    ranked: list[RankedService]
    metrics: list[MetricAnomaly]
    logs: list[LogSignature]
    beacons: list[BeaconFinding]
    health: float
    blocked_attempts: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, max_metrics: int = 12, max_logs: int = 8) -> dict[str, Any]:
        """Compact JSON for the prompt and the trace."""
        d = {
            "t": self.t,
            "health": round(self.health, 3),
            "status": self.status,
            "ranked_services": [r.to_dict() for r in self.ranked if r.score > 0.05][:5],
            "metric_anomalies": [m.to_dict() for m in self.metrics[:max_metrics]],
            "log_signatures": [s.to_dict() for s in self.logs[:max_logs]],
            "beacons": [b.to_dict() for b in self.beacons],
            **self.extras,
        }
        if self.blocked_attempts:
            d["blocked_egress_attempts"] = self.blocked_attempts
        return d

    def problems(self) -> list[str]:
        """Human-readable reasons the system is not verified healthy (empty = quiet)."""
        out = []
        down = [s for s, st in self.status.items() if st != "up"]
        if down:
            out.append(f"services not up: {down}")
        if self.health < 0.9:
            out.append(f"health {self.health:.2f} < 0.9")
        for m in self.metrics[:4]:
            out.append(f"{m.service}.{m.metric} {m.direction} ({m.baseline:.3g} -> {m.recent:.3g})")
        for s in self.logs[:3]:
            if s.level in ("ERROR", "FATAL", "WARN"):
                out.append(f"{s.service} logs: {s.template[:60]} x{s.recent_count}")
        for b in self.beacons:
            out.append(
                f"beacon {b.src}->{b.dst} p={b.probability:.2f} blocked={b.blocked_fraction:.0%}"
            )
        if self.blocked_attempts and not self.beacons:
            out.append(f"{self.blocked_attempts} denied egress attempts (implant still active)")
        return out

    def is_quiet(self) -> bool:
        """True when nothing is flagged (used by the verification gate)."""
        return not self.problems()


class EvidenceBuilder:
    """Observe step of the MAPE-K loop, with a remembered healthy baseline."""

    def __init__(self, use_beacon: bool = True, window: float = WINDOW) -> None:
        self.window = window
        self.beacon = BeaconDetector() if use_beacon else None
        self._metric_baseline: list[MetricSample] | None = None
        self._log_baseline: LogBaseline | None = None

    def build(self, env: Env, window: float | None = None) -> Evidence:
        """Run all detectors on the last ``window`` seconds (incident view)."""
        w = window or self.window
        now = env.now()
        samples = env.get_metrics(w)
        lines = env.get_logs(None, w)
        if self._metric_baseline is None:
            self._metric_baseline = split_baseline(samples)
            self._log_baseline = log_baseline(lines, now, w)
        metrics = detect_metric_anomalies(samples)
        logs = detect_log_signatures(lines, now, w)
        return self._assemble(env, now, metrics, logs, env.get_flows(w))

    def verify(self, env: Env, since: float) -> Evidence:
        """Compare everything observed after ``since`` against the stored healthy baseline."""
        now = env.now()
        w = max(1.0, now - since - TRANSIENT_S)
        samples = env.get_metrics(w)
        lines = env.get_logs(None, w)
        if self._metric_baseline is None:  # verify without a prior build: fall back
            return self.build(env, window=w + TRANSIENT_S)
        metrics = detect_metric_anomalies(samples, baseline=self._metric_baseline)
        logs = detect_log_signatures(lines, now, w, baseline=self._log_baseline)
        return self._assemble(env, now, metrics, logs, env.get_flows(max(w, now - since)))

    def _assemble(self, env: Env, now: float, metrics, logs, flows) -> Evidence:
        status = {k: v.state for k, v in env.get_status().items()}
        beacons = (
            self.beacon.detect(flows, env.list_services(), env.external_allowlist())
            if self.beacon
            else []
        )
        ranked = rank_services(
            env.get_topology(),
            env.get_status(),
            service_anomaly_scores(metrics),
            service_log_scores(logs),
        )
        extras: dict[str, Any] = {}
        if self.beacon is None:
            extras["flow_pairs"] = _raw_flow_summary(flows, env.list_services())
        blocked = sum(1 for f in flows if f.blocked)
        return Evidence(
            now, status, ranked, metrics, logs, beacons, env.health_score(), blocked, extras
        )


def _raw_flow_summary(flows, services: list[str], top: int = 8) -> list[dict[str, Any]]:
    """Fallback for the no-beacon ablation: per-pair counts only."""
    counts: dict[tuple[str, str], int] = {}
    for f in flows:
        counts[(f.src, f.dst)] = counts.get((f.src, f.dst), 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    return [
        {"src": s, "dst": d, "count": c, "external": d not in services and d != "monitor"}
        for (s, d), c in rows[:top]
    ]
