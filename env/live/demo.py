"""make demo-live: scripted, narrated demo of Sentinel-RX on the real local system.

Injects a memory leak (api) + DB connection exhaustion (db) + C2 beacon (worker) at once,
then runs the agent and narrates every step. Ends with the environment's own ground truth.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agent.detectors.evidence import EvidenceBuilder
from agent.llm import LLMClient
from agent.loop import SentinelAgent
from env.live.live_env import LiveEnv


def say(msg: str) -> None:
    """Narration line with a timestamp."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    """Run the demo."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--baseline-s", type=float, default=35.0)
    ap.add_argument("--incident-s", type=float, default=45.0)
    ap.add_argument("--out", default="results/demo_live_trace.json")
    args = ap.parse_args()

    say("Starting 5 real local services (gateway, api, db, cache, worker) + a C2 sinkhole ...")
    env = LiveEnv()
    say(
        f"Services up on ports {env.ports}. "
        f"Generating normal traffic for {args.baseline_s:.0f}s (baseline)."
    )
    time.sleep(args.baseline_s)
    say(f"Baseline health = {env.health_score():.2f}")

    say("INJECTING three concurrent faults:")
    say("  1. memory_leak      -> api     (heap grows ~6 MB/s, handlers slow down)")
    say("  2. db_conn_exhaustion -> db    (idle connections hold the whole pool)")
    say(
        "  3. c2_beacon        -> worker  (phones home every ~4s +/-20% to 203.0.113.47, "
        "a local sinkhole)"
    )
    env.inject("memory_leak", "api", {"mb_per_s": 6})
    env.inject("db_conn_exhaustion", "db")
    env.inject("c2_beacon", "worker", {"interval": 4.0, "jitter": 0.2})
    time.sleep(args.incident_s)
    say(f"Incident in progress: health = {env.health_score():.2f}")
    ev = EvidenceBuilder(window=env.now()).build(env)
    say("Detector view (what the LLM will see):")
    print(json.dumps(ev.to_dict(), indent=1)[:2500])

    say(f"Running Sentinel-RX (model={args.model}{', replay' if args.replay else ''}) ...")
    agent = SentinelAgent(LLMClient(model=args.model, replay=args.replay))
    trace = agent.run(env, budget=4)
    for st in trace.steps:
        if st["phase"] != "iteration":
            continue
        say(f"--- iteration {st['i']} ---")
        say(
            "hypotheses: "
            + ", ".join(
                f"{h['service']}:{h['type']}({h['confidence']:.2f})"
                for h in st.get("hypotheses", [])
            )
        )
        say(
            "checks: "
            + ", ".join(
                f"{c['tool']}({c.get('service')},{c.get('grep') or ''})"
                for c in st.get("checks", [])
            )
        )
        say("plan: " + ", ".join(st.get("plan", [])))
        for r in st.get("actions", []):
            say(
                f"  safety: {r['action']['name']}({r['action']['target']}) -> {r['result']} "
                f"{r.get('reason', '')} {r.get('message', '')}"
            )
        v = st.get("verification", {})
        say(
            f"verify after {v.get('waited_s', 0):.0f}s: "
            f"{'RECOVERED' if not v.get('problems') else v.get('problems')}"
        )
    say(
        f"Agent claims recovered = {trace.claimed_recovered}; escalated = {trace.escalated}; "
        f"LLM calls = {trace.llm_calls}, tokens = {trace.tokens_in + trace.tokens_out}"
    )
    gt = env.gt_summary()
    say(
        f"GROUND TRUTH from the live system: active faults = {gt['active_faults']}; "
        f"restart-before-containment = {gt['restart_before_containment']}; "
        f"sinkhole hits = {gt['sinkhole_hits']}"
    )
    say(f"Final health = {env.health_score():.2f}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    d = trace.to_dict()
    d["ground_truth"] = gt
    out.write_text(json.dumps(d, indent=1, default=str))
    say(f"Trace written to {out}")
    env.close()


if __name__ == "__main__":
    main()
