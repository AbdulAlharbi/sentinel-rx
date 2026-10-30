"""Re-score an existing results file from its saved traces with the current scorer.

Writes ``<name>_rescored.json`` next to the original (the original is never modified) with
``meta.rescored = True`` and ``meta.rescored_from``. Usage: ``python -m bench.rescore <file>...``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bench.aggregate import aggregate
from bench.score import score_run
from env.perturb import RENAME_MAP
from scenarios.schema import load_split

ROOT = Path(__file__).resolve().parents[1]


def rescore(path: Path) -> Path:
    """Re-score one results file; return the path written."""
    d = json.loads(path.read_text())
    meta = d["meta"]
    scenarios = {s.id: s for s in load_split(meta["split"])}
    trace_dir = ROOT / meta["trace_dir"]
    scores = []
    for old in d["scores"]:
        sc = scenarios[old["scenario_id"]]
        t = json.loads((trace_dir / f"{sc.id}_s{old['seed_offset']}.json").read_text())
        name_map = RENAME_MAP if t.get("perturbation") == "renamed_services" else None
        new = score_run(t, sc, t["ground_truth"], name_map)
        new["seed_offset"] = old["seed_offset"]
        new["config"] = old["config"]
        scores.append(new)
    out = {
        "meta": {**meta, "rescored": True, "rescored_from": path.name},
        "summary": aggregate(scores),
        "scores": scores,
    }
    target = path.with_name(path.stem + "_rescored.json")
    target.write_text(json.dumps(out, indent=1))
    return target


def main() -> None:
    """CLI."""
    for arg in sys.argv[1:]:
        out = rescore(Path(arg))
        m = json.loads(out.read_text())["summary"]["metrics"]
        keys = ("top1", "recovered", "false_recovery", "harmful_actions", "action_precision")
        print(f"{out.name}: " + " ".join(f"{k}={m[k]['mean']:.2f}" for k in keys))


if __name__ == "__main__":
    main()
