"""Train the beacon RandomForest on a synthetic set generated *independently* of the simulator.

Classes: beacon (jittered periodic, unknown external dst) vs. legitimate periodic traffic
(health checks, update polls), Poisson request traffic (internal and external), bursty batch
uploads and one-off connections. Writes ``models/beacon_rf.joblib`` and
``models/beacon_metrics.json`` (precision / recall / AP on a held-out synthetic test set).
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    precision_score,
    recall_score,
)

from agent.detectors.beacon import FEATURE_NAMES, MODEL_PATH, pair_features

METRICS_PATH = MODEL_PATH.with_name("beacon_metrics.json")


def _periodic(
    rng: np.random.Generator, interval: float, jitter: float, n: int, drop: float = 0.0
) -> np.ndarray:
    gaps = interval * (1 + rng.uniform(-jitter, jitter, size=n))
    ts = np.cumsum(gaps) + rng.uniform(0, interval)
    if drop > 0:
        ts = ts[rng.random(n) > drop]
    return ts


def _poisson(rng: np.random.Generator, rate: float, n: int) -> np.ndarray:
    return np.cumsum(rng.exponential(1.0 / rate, size=n))


def _bursty(rng: np.random.Generator, n_bursts: int, per_burst: int) -> np.ndarray:
    starts = np.cumsum(rng.uniform(30, 400, size=n_bursts))
    return np.sort(np.concatenate([s + rng.exponential(0.5, size=per_burst) for s in starts]))


def _retries(rng: np.random.Generator) -> np.ndarray:
    """Exponential-backoff retry loops: regular-looking but geometric gaps."""
    out, t = [], 0.0
    for _ in range(int(rng.integers(2, 6))):
        gap = float(rng.uniform(0.5, 2))
        for _ in range(int(rng.integers(4, 8))):
            t += gap
            out.append(t)
            gap *= 2
        t += float(rng.uniform(60, 300))
    return np.asarray(out)


def sample(rng: np.random.Generator) -> tuple[np.ndarray, int, str]:
    """One labelled (features, label, kind) sample."""
    kind = rng.choice(
        [
            "beacon",
            "health",
            "update",
            "poisson_int",
            "poisson_ext",
            "poisson_ext_short",
            "bursty_ext",
            "sparse_ext",
            "retry_ext",
        ],
        p=[0.28, 0.12, 0.09, 0.12, 0.12, 0.09, 0.06, 0.06, 0.06],
    )
    match kind:
        case "beacon":
            interval = float(np.exp(rng.uniform(np.log(2), np.log(200))))
            jitter = float(rng.uniform(0, 0.6))
            ts = _periodic(
                rng, interval, jitter, int(rng.integers(5, 80)), drop=float(rng.uniform(0, 0.3))
            )
            size = float(rng.uniform(150, 2000))
            nb = np.abs(rng.normal(size, size * float(rng.uniform(0.02, 0.6)), size=ts.size)) + 1
            allowlisted = bool(rng.random() < 0.1)  # domain fronting through a trusted CDN
            feats = pair_features(ts, nb, True, allowlisted, False)
            return feats, 1, kind
        case "health":
            ts = _periodic(
                rng,
                float(rng.uniform(1, 60)),
                float(rng.uniform(0, 0.03)),
                int(rng.integers(10, 200)),
            )
            feats = pair_features(ts, np.full(ts.size, 120.0), False, False, True)
        case "update":
            ts = _periodic(
                rng,
                float(rng.uniform(60, 900)),
                float(rng.uniform(0, 0.1)),
                int(rng.integers(4, 15)),
            )
            feats = pair_features(ts, rng.normal(850, 50, size=ts.size), True, True, False)
        case "poisson_int":
            ts = _poisson(rng, float(rng.uniform(0.05, 5)), int(rng.integers(8, 300)))
            feats = pair_features(ts, rng.uniform(300, 5000, size=ts.size), False, False, False)
        case "poisson_ext":
            ts = _poisson(rng, float(rng.uniform(0.02, 2)), int(rng.integers(5, 200)))
            feats = pair_features(
                ts, rng.uniform(300, 5000, size=ts.size), True, bool(rng.random() < 0.5), False
            )
        case "poisson_ext_short":  # short verification windows: few events, allow-listed
            ts = _poisson(rng, float(rng.uniform(0.05, 1)), int(rng.integers(5, 30)))
            feats = pair_features(ts, rng.uniform(300, 5000, size=ts.size), True, True, False)
        case "bursty_ext":
            ts = _bursty(rng, int(rng.integers(2, 8)), int(rng.integers(3, 20)))
            feats = pair_features(
                ts, rng.uniform(1000, 50000, size=ts.size), True, bool(rng.random() < 0.3), False
            )
        case "retry_ext":
            ts = _retries(rng)
            feats = pair_features(
                ts, rng.normal(400, 30, size=ts.size), True, bool(rng.random() < 0.5), False
            )
        case _:  # sparse_ext: a handful of unrelated connections
            ts = np.sort(rng.uniform(0, 900, size=int(rng.integers(4, 8))))
            feats = pair_features(ts, rng.uniform(300, 3000, size=ts.size), True, False, False)
    return feats, 0, str(kind)


def make_set(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Generate a labelled synthetic set."""
    rng = np.random.default_rng(seed)
    rows, labels, kinds = [], [], []
    for _ in range(n):
        f, y, k = sample(rng)
        rows.append(f)
        labels.append(y)
        kinds.append(k)
    return np.vstack(rows), np.asarray(labels), kinds


def train(n_train: int = 6000, n_test: int = 2000, out: Path = MODEL_PATH) -> dict:
    """Train, evaluate on a disjoint synthetic test set, save model + metrics."""
    x_tr, y_tr, _ = make_set(n_train, seed=1)
    x_te, y_te, kinds = make_set(n_test, seed=2)
    clf = RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=3, random_state=0, n_jobs=1
    )
    clf.fit(x_tr, y_tr)
    p = clf.predict_proba(x_te)[:, 1]
    pred = (p >= 0.5).astype(int)
    prec, rec, thr = precision_recall_curve(y_te, p)
    kinds_arr = np.asarray(kinds)
    fp_by_kind = {
        k: float(np.mean(pred[kinds_arr == k])) for k in sorted(set(kinds)) if k != "beacon"
    }
    metrics = {
        "n_train": n_train,
        "n_test": n_test,
        "precision": float(precision_score(y_te, pred)),
        "recall": float(recall_score(y_te, pred)),
        "average_precision": float(average_precision_score(y_te, p)),
        "false_positive_rate_by_kind": fp_by_kind,
        "pr_curve": {
            "precision": prec[:: max(1, len(prec) // 200)].tolist(),
            "recall": rec[:: max(1, len(rec) // 200)].tolist(),
        },
        "feature_importances": dict(
            zip(FEATURE_NAMES, [round(float(v), 4) for v in clf.feature_importances_], strict=True)
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, out, compress=3)
    metrics["simulator_dev_eval"] = evaluate_on_simulator(out)
    METRICS_PATH.write_text(json.dumps(metrics, indent=1))
    return metrics


def evaluate_on_simulator(model_path: Path = MODEL_PATH) -> dict:
    """Independent check: run the detector on every *dev* scenario's flows (never heldout).

    A pair is a true beacon iff it is (target, c2 dst) of an injected c2_beacon fault.
    """
    from agent.detectors.beacon import BeaconDetector
    from env.sim.sim_env import SimEnv
    from scenarios.schema import load_split

    det = BeaconDetector(model_path)
    tp = fp = fn = 0
    fp_pairs: list[str] = []
    for sc in load_split("dev"):
        env = SimEnv(sc)
        truth = {
            (f.target, f.params.get("dst", "203.0.113.47"))
            for f in sc.faults
            if f.type == "c2_beacon"
        }
        found = {
            (b.src, b.dst)
            for b in det.detect(env.get_flows(900), env.list_services(), env.external_allowlist())
        }
        tp += len(truth & found)
        fn += len(truth - found)
        fp += len(found - truth)
        fp_pairs += [f"{sc.id}:{s}->{d}" for s, d in found - truth]
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": prec,
        "recall": rec,
        "false_positive_pairs": fp_pairs[:10],
    }


if __name__ == "__main__":
    m = train()
    print(json.dumps({k: v for k, v in m.items() if k != "pr_curve"}, indent=1))
