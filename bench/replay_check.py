"""CI reproducibility check: every committed held-out and perturbation result must replay exactly.

For the latest results file of each (split, config, tag) outside dev, the same scenarios are
re-run from the committed LLM cache (``--replay``: no model, no network) into a temporary
directory, and every per-run score is compared with the committed one. Only ``wall_seconds``
may differ. A cache miss, a changed prompt or a changed score fails the build, so a published
number can never silently stop being reproducible.

This only re-scores runs that already exist; it never runs a new held-out experiment, so it is
not logged in ``results/heldout_runs.log``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from bench.report import load_results
from bench.runner import run_split

IGNORED = {"wall_seconds"}


def _key(score: dict[str, Any]) -> tuple[str, int]:
    return score["scenario_id"], score["seed_offset"]


def check(result: dict[str, Any], out_dir: Path) -> list[str]:
    """Replay one committed result; return a list of mismatches (empty = identical)."""
    m = result["meta"]
    with contextlib.redirect_stdout(io.StringIO()):  # per-run progress lines are noise here
        [path] = run_split(
            m["split"],
            [m["config"]],
            m["seeds"],
            m["model"],
            replay=True,
            budget=m["budget"],
            limit=None,
            tag=m.get("tag", ""),
            workers=4,
            out_dir=out_dir,
        )
    fresh = {_key(s): s for s in json.loads(path.read_text())["scores"]}
    problems = []
    if len(fresh) != len(result["scores"]):
        problems.append(f"{len(fresh)} runs replayed, {len(result['scores'])} committed")
    for s in result["scores"]:
        r = fresh.get(_key(s))
        if r is None:
            problems.append(f"{_key(s)} missing")
            continue
        for k, v in s.items():
            if k not in IGNORED and r.get(k) != v:
                problems.append(f"{_key(s)} {k}: committed {v!r}, replayed {r.get(k)!r}")
    return problems


def main() -> None:
    """Exit non-zero if any committed held-out or perturbation result fails to replay."""
    targets = {k: v for k, v in load_results().items() if k[0] != "dev"}
    failed: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for (split, config, tag), result in sorted(targets.items()):
            name = f"{split}/{config}{('/' + tag) if tag else ''}"
            try:
                problems = check(result, Path(tmp))
            except Exception as e:  # noqa: BLE001 - a cache miss must fail the check, not crash it
                problems = [f"{type(e).__name__}: {e}"]
            if problems:
                failed[name] = problems
            print(f"replay {'FAIL' if problems else 'ok  '} {name} ({result['meta']['file']})")
    if failed:
        for name, problems in failed.items():
            print(f"\n{name}:\n  " + "\n  ".join(problems[:10]))
        sys.exit(f"{len(failed)} committed result(s) do not replay exactly")
    print(f"all {len(targets)} committed held-out / perturbation results replay exactly")


if __name__ == "__main__":
    main()
