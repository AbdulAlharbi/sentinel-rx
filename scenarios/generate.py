"""Generate the dev / held-out / perturbation scenario sets.

Split discipline (enforced here and checked by ``tests/test_scenarios.py``):

* every held-out scenario uses a *fault combination* (sorted ``type@target`` key) that does
  not occur in dev, and for multi-fault scenarios even the *multiset of fault types* is new;
* held-out seeds come from a disjoint range;
* ``dns_failure`` never appears outside ``perturbations/unseen_fault``.

Usage: ``python -m scenarios.generate [--root scenarios]``.
"""

from __future__ import annotations

import argparse
import itertools
import shutil
from pathlib import Path

import numpy as np

from faults.base import FaultSpec
from faults.registry import KNOWN_FAULT_TYPES, VALID_TARGETS
from scenarios.schema import SCENARIO_ROOT, Perturbation, Scenario

DEV_SEED_BASE = 1000
HELDOUT_SEED_BASE = 5000
PERTURBATIONS = ("noisy_logs", "missing_metrics", "renamed_services", "timestamp_jitter")
BEACON_DSTS = ("203.0.113.47", "198.51.100.23", "192.0.2.199", "203.0.113.88")

DEV_COUNTS = {1: 10, 2: 12, 3: 8}
HELDOUT_COUNTS = {1: 4, 2: 9, 3: 7}


def _params(ftype: str, rng: np.random.Generator) -> dict:
    match ftype:
        case "memory_leak":
            return {"rate": round(float(rng.uniform(1.2, 1.8)), 2)}
        case "db_conn_exhaustion":
            return {"ramp": round(float(rng.uniform(6, 10)), 1)}
        case "disk_full":
            return {"rate": round(float(rng.uniform(4, 6)), 1)}
        case "latency_injection":
            return {"ms": int(rng.integers(250, 600))}
        case "bad_config_deploy":
            return {"error": round(float(rng.uniform(0.3, 0.6)), 2)}
        case "c2_beacon":
            return {
                "interval": int(rng.integers(25, 60)),
                "jitter": round(float(rng.uniform(0.1, 0.35)), 2),
                "dst": str(rng.choice(BEACON_DSTS)),
            }
        case "credential_stuffing":
            return {"burst": int(rng.integers(4, 9))}
        case "dns_failure":
            return {}
        case _:
            return {}


def _start(ftype: str, rng: np.random.Generator) -> int:
    hi = 55 if ftype in ("memory_leak", "disk_full", "c2_beacon") else 65
    return int(rng.integers(40, hi + 1))


def all_combos(
    n: int, types: tuple[str, ...] = KNOWN_FAULT_TYPES
) -> list[tuple[tuple[str, str], ...]]:
    """All valid combinations of ``n`` (type, target) pairs: distinct types, distinct targets."""
    pairs = [(t, tgt) for t in types for tgt in VALID_TARGETS[t]]
    combos = []
    for combo in itertools.combinations(pairs, n):
        types_ = [c[0] for c in combo]
        targets = [c[1] for c in combo]
        if len(set(types_)) < n or len(set(targets)) < n:
            continue
        combos.append(tuple(sorted(combo)))
    return combos


def _make(
    sid: str, split: str, seed: int, combo: tuple[tuple[str, str], ...], rng: np.random.Generator
) -> Scenario:
    faults = [FaultSpec(t, tgt, _start(t, rng), _params(t, rng)) for t, tgt in combo]
    return Scenario(id=sid, split=split, seed=seed, faults=faults)


def generate(root: Path = SCENARIO_ROOT, master_seed: int = 20261029) -> dict[str, list[Scenario]]:
    """Generate all splits deterministically and write them under ``root``."""
    rng = np.random.default_rng(master_seed)
    out: dict[str, list[Scenario]] = {"dev": [], "heldout": []}
    used_combo_keys: set[str] = set()
    used_type_sets: set[tuple[str, ...]] = set()

    # ---- dev --------------------------------------------------------------------------------
    seed = DEV_SEED_BASE
    for n, count in DEV_COUNTS.items():
        combos = all_combos(n)
        if n == 1:  # one scenario per fault type first, then extra targets
            by_type: dict[str, list] = {}
            for c in combos:
                by_type.setdefault(c[0][0], []).append(c)
            chosen = [by_type[t][int(rng.integers(0, len(by_type[t])))] for t in KNOWN_FAULT_TYPES]
            rest = [c for c in combos if c not in chosen]
            rng.shuffle(rest)  # type: ignore[arg-type]
            chosen += rest[: count - len(chosen)]
        else:
            idx = rng.permutation(len(combos))
            chosen = [combos[i] for i in idx[:count]]
        for i, combo in enumerate(chosen):
            sid = f"dev_{n}f_{i:02d}"
            sc = _make(sid, "dev", seed, combo, rng)
            seed += 1
            out["dev"].append(sc)
            used_combo_keys.add(sc.combo_key)
            used_type_sets.add(sc.fault_types)

    # ---- heldout: combos (and, for n>=2, type multisets) never seen in dev ----------------
    seed = HELDOUT_SEED_BASE
    for n, count in HELDOUT_COUNTS.items():
        combos = all_combos(n)
        idx = rng.permutation(len(combos))
        chosen = []
        for i in idx:
            c = combos[i]
            key = "+".join(sorted(f"{t}@{tgt}" for t, tgt in c))
            tset = tuple(sorted(t for t, _ in c))
            if key in used_combo_keys:
                continue
            if n >= 2 and tset in used_type_sets:
                continue
            chosen.append(c)
            used_combo_keys.add(key)
            used_type_sets.add(tset)
            if len(chosen) == count:
                break
        if len(chosen) < count:
            raise RuntimeError(
                f"not enough unseen {n}-fault combos for heldout ({len(chosen)}/{count})"
            )
        for i, combo in enumerate(chosen):
            sid = f"heldout_{n}f_{i:02d}"
            out["heldout"].append(_make(sid, "heldout", seed, combo, rng))
            seed += 1

    # ---- perturbations: variants of the heldout set --------------------------------------
    for name in PERTURBATIONS:
        key = f"perturbations/{name}"
        out[key] = []
        for sc in out["heldout"]:
            variant = Scenario.from_dict(sc.to_dict())
            variant.id = f"{sc.id}__{name}"
            variant.split = key
            variant.perturbation = Perturbation(name, {})
            variant.base_id = sc.id
            out[key].append(variant)
    # unseen fault type: dns_failure alone and paired with known faults, fresh seeds
    key = "perturbations/unseen_fault"
    out[key] = []
    seed = HELDOUT_SEED_BASE + 500
    dns_targets = VALID_TARGETS["dns_failure"]
    partners = [
        ("memory_leak", "worker"),
        ("db_conn_exhaustion", "db"),
        ("c2_beacon", "worker"),
        ("cache_failure", "cache"),
        ("credential_stuffing", "gateway"),
        ("disk_full", "db"),
    ]
    combos_unseen: list[tuple[tuple[str, str], ...]] = [(("dns_failure", t),) for t in dns_targets]
    for p in partners:
        tgt = "api" if p[1] != "api" else "worker"
        combos_unseen.append(tuple(sorted((("dns_failure", tgt), p))))
    for i, combo in enumerate(combos_unseen):
        sc = _make(f"unseen_{i:02d}", key, seed, combo, rng)
        sc.perturbation = Perturbation("unseen_fault", {})
        out[key].append(sc)
        seed += 1

    # ---- write ----------------------------------------------------------------------------
    for split, scs in out.items():
        folder = root / split
        if folder.exists():
            shutil.rmtree(folder)
        for sc in scs:
            sc.save(folder / f"{sc.id}.yaml")
    return out


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=SCENARIO_ROOT)
    args = ap.parse_args()
    out = generate(args.root)
    for split, scs in out.items():
        print(f"{split}: {len(scs)} scenarios")


if __name__ == "__main__":
    main()
