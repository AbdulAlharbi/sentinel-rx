"""Metric anomaly detector: robust z-score of the recent window against an early baseline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from env.types import METRIC_NAMES, MetricSample

#: Metrics that cannot be explained by a dependency being unhealthy.
INTRINSIC_METRICS: frozenset[str] = frozenset({"mem", "disk_used", "conn_count", "cpu"})
#: Metrics that propagate upstream along the dependency graph (a queue backs up when a
#: dependency is slow, so it is a symptom, not a cause).
PROPAGATED_METRICS: frozenset[str] = frozenset(
    {"latency_p50", "latency_p95", "error_rate", "queue_depth"}
)

Z_FLAG = 6.0
MIN_ABS_DELTA = {
    "error_rate": 0.05,
    "latency_p50": 20.0,
    "latency_p95": 50.0,
    "cpu": 8.0,
    "mem": 8.0,
    "conn_count": 8.0,
    "disk_used": 8.0,
    "queue_depth": 3.0,
}


@dataclass
class MetricAnomaly:
    """One flagged (service, metric) series."""

    service: str
    metric: str
    z: float
    baseline: float
    recent: float
    direction: str

    def to_dict(self) -> dict:
        """Compact form for prompts/traces."""
        return {
            "service": self.service,
            "metric": self.metric,
            "z": round(self.z, 1),
            "baseline": round(self.baseline, 3),
            "recent": round(self.recent, 3),
            "direction": self.direction,
        }


def robust_z(baseline: np.ndarray, recent: np.ndarray) -> float:
    """|median(recent) - median(baseline)| / (1.4826 * MAD(baseline) + eps)."""
    if baseline.size < 3 or recent.size < 1:
        return 0.0
    med = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - med))) * 1.4826
    scale = max(mad, 0.02 * abs(med), 1e-6)
    return float(abs(np.median(recent) - med) / scale)


def split_baseline(samples: list[MetricSample], baseline_frac: float = 0.3) -> list[MetricSample]:
    """The earliest ``baseline_frac`` of a window (pre-incident reference)."""
    if not samples:
        return []
    t0 = min(s.t for s in samples)
    t1 = max(s.t for s in samples)
    cut = t0 + baseline_frac * (t1 - t0)
    return [s for s in samples if s.t <= cut]


def detect_metric_anomalies(
    samples: list[MetricSample],
    baseline: list[MetricSample] | None = None,
    baseline_frac: float = 0.3,
    recent_frac: float = 0.2,
) -> list[MetricAnomaly]:
    """Flag (service, metric) series whose recent level departs from the baseline.

    If ``baseline`` is given it is used as the reference (stored pre-incident samples) and the
    whole of ``samples`` counts as "recent"; otherwise the first ``baseline_frac`` of the window
    is the reference and the last ``recent_frac`` is compared against it.
    Missing values (``None``) are ignored; a series with too few points is skipped.
    """
    by_service: dict[str, list[MetricSample]] = {}
    for s in samples:
        by_service.setdefault(s.service, []).append(s)
    base_by_service: dict[str, list[MetricSample]] = {}
    for s in baseline or []:
        base_by_service.setdefault(s.service, []).append(s)
    out: list[MetricAnomaly] = []
    for service, rows in by_service.items():
        rows.sort(key=lambda r: r.t)
        n = len(rows)
        if baseline is None:
            if n < 10:
                continue
            nb, nr = max(3, int(n * baseline_frac)), max(2, int(n * recent_frac))
            base_rows, rec_rows = rows[:nb], rows[-nr:]
        else:
            base_rows, rec_rows = base_by_service.get(service, []), rows
        for metric in METRIC_NAMES:
            base = np.asarray(
                [r.values.get(metric) for r in base_rows if r.values.get(metric) is not None],
                dtype=float,
            )
            rec = np.asarray(
                [r.values.get(metric) for r in rec_rows if r.values.get(metric) is not None],
                dtype=float,
            )
            if base.size < 3 or rec.size < 1:
                continue
            z = robust_z(base, rec)
            delta = float(np.median(rec) - np.median(base))
            if z >= Z_FLAG and abs(delta) >= MIN_ABS_DELTA.get(metric, 0.0):
                out.append(
                    MetricAnomaly(
                        service,
                        metric,
                        z,
                        float(np.median(base)),
                        float(np.median(rec)),
                        "up" if delta > 0 else "down",
                    )
                )
    out.sort(key=lambda a: -a.z)
    return out


def service_anomaly_scores(anomalies: list[MetricAnomaly]) -> dict[str, dict[str, float]]:
    """Per-service intrinsic / propagated anomaly strength in [0, 1]."""
    scores: dict[str, dict[str, float]] = {}
    for a in anomalies:
        d = scores.setdefault(a.service, {"intrinsic": 0.0, "propagated": 0.0})
        strength = min(1.0, a.z / 30.0)
        key = "intrinsic" if a.metric in INTRINSIC_METRICS else "propagated"
        d[key] = max(d[key], strength)
    return scores
