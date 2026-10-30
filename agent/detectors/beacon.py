"""Beacon detector: timing features per (src, dst) flow pair scored by a RandomForest.

Features are scale-invariant (computed on inter-arrival times relative to their median) so
the same model works for a simulator beacon every 45 s and a live demo beacon every 4 s.
Destination knowledge (internal / allow-listed / unknown external) is a feature too: a
perfectly periodic health check to an internal service is *not* a beacon.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np

from env.types import Flow

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "beacon_rf.joblib"
FEATURE_NAMES: tuple[str, ...] = (
    "n_events",
    "iat_cv",
    "iat_mad_ratio",
    "within_30pct",
    "spectral_concentration",
    "autocorr_peak",
    "subwindow_stability",
    "bytes_cv",
    "log_median_iat",
    "dst_external",
    "dst_allowlisted",
    "src_is_monitor",
)
MIN_EVENTS = 4


def timing_features(ts: np.ndarray, nbytes: np.ndarray) -> dict[str, float]:
    """Scale-invariant periodicity features of one event sequence."""
    ts = np.sort(np.asarray(ts, dtype=float))
    n = ts.size
    f = {k: 0.0 for k in FEATURE_NAMES[:9]}
    f["n_events"] = float(n)
    if n < MIN_EVENTS:
        return f
    iat = np.diff(ts)
    iat = iat[iat > 0] if np.any(iat > 0) else iat + 1e-3
    med = float(np.median(iat))
    mean = float(np.mean(iat))
    f["iat_cv"] = float(np.std(iat) / mean) if mean > 0 else 0.0
    f["iat_mad_ratio"] = float(np.median(np.abs(iat - med)) / med) if med > 0 else 0.0
    f["within_30pct"] = float(np.mean(np.abs(iat - med) <= 0.3 * med)) if med > 0 else 0.0
    f["log_median_iat"] = float(np.log1p(med))
    # binned series for spectral / autocorrelation features
    bin_w = max(med / 8.0, 1e-3)
    span = ts[-1] - ts[0]
    nbins = int(min(4096, max(16, np.ceil(span / bin_w) + 1)))
    counts, _ = np.histogram(ts, bins=nbins, range=(ts[0], ts[0] + nbins * bin_w))
    x = counts - counts.mean()
    power = np.abs(np.fft.rfft(x)) ** 2
    if power.size > 1 and power[1:].sum() > 0:
        f["spectral_concentration"] = float(power[1:].max() / power[1:].sum())
    lag = int(round(med / bin_w))
    if 0 < lag < x.size:
        a, b = x[:-lag], x[lag:]
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        f["autocorr_peak"] = max(0.0, float((a * b).sum() / denom)) if denom > 0 else 0.0
    q = np.histogram(ts, bins=4, range=(ts[0], ts[-1] + 1e-6))[0].astype(float)
    f["subwindow_stability"] = float(max(0.0, 1.0 - q.std() / q.mean())) if q.mean() > 0 else 0.0
    nb = np.asarray(nbytes, dtype=float)
    f["bytes_cv"] = float(nb.std() / nb.mean()) if nb.size and nb.mean() > 0 else 0.0
    return f


def pair_features(
    ts: np.ndarray,
    nbytes: np.ndarray,
    dst_external: bool,
    dst_allowlisted: bool,
    src_is_monitor: bool,
) -> np.ndarray:
    """Full feature vector in :data:`FEATURE_NAMES` order."""
    f = timing_features(ts, nbytes)
    f["dst_external"] = float(dst_external)
    f["dst_allowlisted"] = float(dst_allowlisted)
    f["src_is_monitor"] = float(src_is_monitor)
    return np.asarray([f[k] for k in FEATURE_NAMES], dtype=float)


@dataclass
class BeaconFinding:
    """A flow pair that looks like command-and-control beaconing."""

    src: str
    dst: str
    probability: float
    n_events: int
    median_interval_s: float
    jitter: float
    blocked_fraction: float

    def to_dict(self) -> dict:
        """Compact form for prompts/traces."""
        return {
            "src": self.src,
            "dst": self.dst,
            "p_beacon": round(self.probability, 2),
            "events": self.n_events,
            "interval_s": round(self.median_interval_s, 1),
            "jitter": round(self.jitter, 2),
            "blocked_fraction": round(self.blocked_fraction, 2),
        }


class BeaconDetector:
    """Scores every (src, dst) pair in a flow window."""

    def __init__(self, model_path: Path = MODEL_PATH, threshold: float = 0.5) -> None:
        if not model_path.exists():
            raise FileNotFoundError(
                f"{model_path} missing; run `python -m agent.detectors.beacon_train`"
            )
        self.model = joblib.load(model_path)
        self.threshold = threshold

    def detect(
        self, flows: list[Flow], services: list[str], allowlist: set[str], monitor: str = "monitor"
    ) -> list[BeaconFinding]:
        """Return pairs with P(beacon) >= threshold, most suspicious first."""
        pairs: dict[tuple[str, str], list[Flow]] = {}
        for f in flows:
            pairs.setdefault((f.src, f.dst), []).append(f)
        internal = set(services) | {monitor}
        rows, keys = [], []
        for (src, dst), fl in pairs.items():
            if len(fl) < MIN_EVENTS:
                continue
            ts = np.asarray([x.t for x in fl])
            nb = np.asarray([x.nbytes for x in fl])
            rows.append(
                pair_features(ts, nb, dst not in internal, dst in allowlist, src == monitor)
            )
            keys.append((src, dst, ts, fl))
        if not rows:
            return []
        probs = self.model.predict_proba(np.vstack(rows))[:, 1]
        out = []
        for p, (src, dst, ts, fl) in zip(probs, keys, strict=True):
            if p < self.threshold:
                continue
            iat = np.diff(np.sort(ts))
            med = float(np.median(iat)) if iat.size else 0.0
            jit = float(np.median(np.abs(iat - med)) / med) if med > 0 else 0.0
            out.append(
                BeaconFinding(
                    src, dst, float(p), len(fl), med, jit, float(np.mean([x.blocked for x in fl]))
                )
            )
        out.sort(key=lambda b: -b.probability)
        return out
