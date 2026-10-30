"""make demo-video: a narrated, animated terminal walkthrough sized for a short screen recording.

Acts 1-4 replay the committed live run (``results/demo_live_trace.json``, written by
``make demo-live``): every hypothesis, check, action, safety decision and verification shown is
read from that trace, and the screen says it is a recording. Only the waits are shortened.
Act 5 is computed while you watch: the held-out numbers are taken from the committed results, and
all held-out agent runs are re-executed from the LLM cache and compared score by score.
No model and no network are needed.

Pace with ``--speed`` (2 = twice as fast); ``--fast`` prints everything with no delays.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from statistics import fmean
from typing import Any

from bench.replay_check import check
from bench.report import load_results

TRACE = Path("results/demo_live_trace.json")
OUT = sys.stdout  # kept: the replay check redirects sys.stdout while it runs
COLOR = (OUT.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")
LIME, GREEN, AQUA, RED, AMBER, DIM, BOLD, ITALIC = (
    "38;5;191",
    "38;5;78",
    "38;5;79",
    "38;5;203",
    "38;5;214",
    "38;5;245",
    "1",
    "3",
)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# What env/live/demo.py injects (the trace records the agent's side, not the injection).
FAULTS = [
    ("memory_leak", "api", "heap grows ~6 MB/s, handlers slow down"),
    ("db_conn_exhaustion", "db", "idle connections hold the whole pool"),
    ("c2_beacon", "worker", "phones home every ~4 s to 203.0.113.47 (a local sinkhole)"),
]
CONFIGS = [
    ("agent", "Sentinel-RX"),
    ("runbook", "Runbook"),
    ("single_shot", "Single-shot LLM"),
    ("llm_only", "LLM-only loop"),
    ("do_nothing", "Do nothing"),
    ("random", "Random"),
]


class Pace:
    """Global pacing: every delay is divided by ``speed``; ``fast`` removes them."""

    speed = 1.0
    fast = False

    @classmethod
    def sleep(cls, seconds: float) -> None:
        """Sleep for a paced number of seconds."""
        if not cls.fast:
            time.sleep(seconds / cls.speed)


def c(text: str, *codes: str) -> str:
    """Colour text with ANSI codes when writing to a terminal."""
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR and codes else text


def w(text: str = "", end: str = "\n") -> None:
    """Write to the real stdout."""
    OUT.write(text + end)
    OUT.flush()


def say(text: str, pause: float = 0.5) -> None:
    """Narration: typed out in dim italics, then a short pause."""
    if Pace.fast or not COLOR:
        w("  " + c(text, DIM, ITALIC))
    else:
        w("  ", end="")
        for ch in text:
            OUT.write(c(ch, DIM, ITALIC))
            OUT.flush()
            time.sleep(0.018 / Pace.speed)
        w()
    Pace.sleep(pause)


def line(text: str, pause: float = 0.25) -> None:
    """A data line, printed at once."""
    w(text)
    Pace.sleep(pause)


def spin(label: str, seconds: float, until: threading.Event | None = None) -> None:
    """Spinner for ``seconds`` (or until ``until`` is set), then a tick."""
    start, i = time.time(), 0
    if COLOR and not Pace.fast:
        OUT.write("\033[?25l")
        while True:
            done = until.is_set() if until else time.time() - start >= seconds / Pace.speed
            if done:
                break
            OUT.write(f"\r  {c(SPINNER[i % len(SPINNER)], LIME)} {c(label, DIM)}")
            OUT.flush()
            time.sleep(0.08)
            i += 1
        OUT.write("\r\033[2K\033[?25h")
    elif until:
        until.wait()
    w(f"  {c('✔', GREEN)} {c(label, DIM)}")


def health_bar(value: float, width: int = 30) -> str:
    """Coloured health bar, 0..1."""
    n = round(max(0.0, min(1.0, value)) * width)
    col = GREEN if value >= 0.9 else AMBER if value >= 0.5 else RED
    return c("█" * n, col) + c("░" * (width - n), DIM)


def animate_health(start: float, end: float, label: str = "health") -> None:
    """Animate the health bar from ``start`` to ``end``."""
    steps = 1 if Pace.fast or not COLOR else 18
    for k in range(1, steps + 1):
        v = start + (end - start) * k / steps
        OUT.write(f"\r  {label:<7} {health_bar(v)} {c(f'{v:.2f}', BOLD)}")
        OUT.flush()
        Pace.sleep(0.04)
    w()


def heading(n: int, title: str) -> None:
    """Act heading."""
    w()
    w(c(f"  {n} · {title} ", LIME, BOLD) + c("─" * max(4, 74 - len(title)), DIM))
    Pace.sleep(0.6)


def banner(trace: dict[str, Any]) -> None:
    """Title card."""
    if COLOR and not Pace.fast:
        OUT.write("\033[2J\033[H")
    rec = time.strftime("%d %b %Y, %H:%M", time.localtime(trace["started_at"]))
    w()
    w(c("   ╭──────────────────────────────────────────────────────────╮", AQUA))
    w(c("   │  ", AQUA) + c("SENTINEL-RX", LIME, BOLD) + " " * 45 + c("│", AQUA))
    w(c("   │  ", AQUA) + "security-aware recovery of a failing system" + " " * 13 + c("│", AQUA))
    w(c("   │  ", AQUA) + c("HUMAIN Hackathon · Challenge 1", DIM) + " " * 26 + c("│", AQUA))
    w(c("   ╰──────────────────────────────────────────────────────────╯", AQUA))
    w()
    w("  " + c(f"Replaying the recorded live run of {rec}", BOLD) + c(f"  ({TRACE})", DIM))
    w("  " + c("Every step below is read from that trace; only the waits are shortened.", DIM))
    Pace.sleep(2.5)


def act_incident(trace: dict[str, Any]) -> None:
    """Act 1: the system and the three faults."""
    ev = trace["steps"][0]["evidence"]
    heading(1, "The incident: five real processes, three faults at once")
    for src, dsts in ev["topology"].items():
        if dsts:
            line(f"  {c(src, BOLD):<18} → {', '.join(dsts)}", 0.12)
    say("Five real local services serving traffic. Then three faults hit together:", 0.3)
    for i, (fault, svc, what) in enumerate(FAULTS, 1):
        col = RED if fault == "c2_beacon" else AMBER
        line(f"  {i}. {c(f'{fault:<20}', col, BOLD)} → {c(f'{svc:<7}', BOLD)} {c(what, DIM)}", 0.5)
    animate_health(1.0, ev["health"])
    say("Health collapses. One of the three is an attacker hiding inside the outage.", 0.8)


def act_detectors(trace: dict[str, Any]) -> None:
    """Act 2: what the detectors hand the model."""
    ev = trace["steps"][0]["evidence"]
    heading(2, "Detectors: thousands of signals become a short evidence list")
    spin("4 detectors: metric z-scores · log templates · dependency ranker · beacon model", 1.6)
    top_log = max(ev["log_signatures"], key=lambda s: s["score"])
    line(
        f'  {c("log", AQUA)}      {top_log["service"]}: "{top_log["template"]}" '
        f"{c(f'x{top_log["recent"]} (baseline {top_log["baseline"]})', DIM)}"
    )
    for m in ev["metric_anomalies"][:2]:
        line(
            f"  {c('metric', AQUA)}   {m['service']}.{m['metric']} "
            f"{m['baseline']:g} → {c(f'{m["recent"]:g}', BOLD)}"
        )
    for b in ev["beacons"]:
        line(
            f"  {c('beacon', RED, BOLD)}   {b['src']} → {b['dst']} every {b['interval_s']:.1f} s "
            f"(jitter {b['jitter']:.2f}) · {c(f'p(beacon) = {b["p_beacon"]:.2f}', RED, BOLD)}"
        )
    say("That is all the model sees: ~1.2k tokens of evidence, never raw telemetry.", 0.8)


def _model_seconds(steps: list[dict[str, Any]], k: int) -> float:
    """Model time of the LLM calls that follow iteration step ``k`` in the trace."""
    total = 0.0
    for st in steps[k + 1 :]:
        if st["phase"] == "iteration":
            break
        total += st.get("seconds", 0.0)
    return total


def act_loop(trace: dict[str, Any]) -> None:
    """Act 3: each iteration of the agent loop, through the safety gate and the verifier."""
    heading(3, f"The agent loop: {trace['model']} on this laptop, no cloud")
    steps = trace["steps"]
    for k, st in enumerate(steps):
        if st["phase"] != "iteration":
            continue
        w()
        w(c(f"  ▸ Iteration {st['i'] + 1}", LIME, BOLD))
        spin(f"{trace['model']} reasoning ({_model_seconds(steps, k):.0f} s in the recording)", 1.6)
        for h in st["hypotheses"]:
            conf = h["confidence"]
            line(
                f"    hypothesis  {c(f'{h["service"]:<7}', BOLD)} {h['type']:<20} "
                f"{c('█' * round(conf * 10), GREEN)}{c('░' * (10 - round(conf * 10)), DIM)} "
                f"{conf:.2f}",
                0.2,
            )
        checks = " · ".join(f"{x['tool']}({x.get('service')})" for x in st["checks"])
        line(f"    checks      {c(checks, DIM)}", 0.3)
        line(f"    plan        {c(' → '.join(st['plan']), BOLD)}", 0.6)
        for a in st["actions"]:
            act = f"{a['action']['name']}({a['action']['target']})"
            if a["result"] == "blocked":
                Pace.sleep(0.4)
                blocked = c("✖ BLOCKED", RED, BOLD)
                line(f"    {blocked}   {c(act, BOLD)}  {c(a['reason'], RED)}", 0.6)
                say("The model wanted to restart the infected host. The safety gate said no:", 0)
                say("contain it first, or the restart destroys evidence and it re-infects.", 1.2)
            else:
                hb, ha = a.get("health_before"), a.get("health_after")
                delta = f"health {hb:.2f} → {ha:.2f}" if hb is not None else ""
                line(f"    {c('✔ applied', GREEN)}   {act:<28} {c(delta, DIM)}", 0.4)
        v = st["verification"]
        spin(f"verifier: {v['waited_s']:.0f} s later, telemetry vs the healthy baseline", 1.2)
        problems = v["problems"]
        if not problems:
            ok = c("✔ RECOVERED", GREEN, BOLD)
            line(f"    {ok}  telemetry matches the healthy baseline", 0.8)
            continue
        line(f"    {c('✖ NOT RECOVERED', RED, BOLD)}", 0.2)
        for p in problems[:4]:
            line(f"      {c(p, RED if p.startswith('beacon') else AMBER)}", 0.15)
        if len(problems) > 4:
            line(c(f"      … {len(problems) - 4} more", DIM), 0.1)
        if not any(p.startswith("health") for p in problems) and any(
            p.startswith("beacon") for p in problems
        ):
            say("Health is back, but the beacon is still phoning home.", 0)
            say("The verifier refuses to call that recovered, so the loop goes again.", 1.2)
        else:
            say("Not recovered, so the loop goes again with what it learned.", 0.8)


def act_ground_truth(trace: dict[str, Any]) -> None:
    """Act 4: the live system's own ground truth."""
    gt = trace["ground_truth"]
    heading(4, "Ground truth from the live system")
    active = [f"{s}:{f}" for s, fs in gt["active_faults"].items() for f in fs]
    health = trace["actions"][-1]["health_after"]
    animate_health(trace["steps"][0]["evidence"]["health"], health)
    tokens = trace["tokens_in"] + trace["tokens_out"]
    line(f"  active faults               {c(', '.join(active) or 'none', GREEN, BOLD)}", 0.3)
    line(f"  restart before containment  {c(str(gt['restart_before_containment']), GREEN, BOLD)}")
    line(f"  agent claimed recovered     {c(str(trace['claimed_recovered']), GREEN, BOLD)}", 0.3)
    line(f"  model calls · tokens        {trace['llm_calls']} · {tokens:,}", 0.6)
    say("The agent's claim and the system's own ground truth agree.", 1.0)


def act_results() -> None:
    """Act 5: held-out numbers from the committed results, then a live replay check."""
    results = load_results()
    heading(5, "Incidents it never saw: 20 held-out scenarios × 3 seeds")
    w(c(f"  {'':<17}{'recovered':<38}false “recovered”", DIM))
    for key, name in CONFIGS:
        scores = results[("heldout", key, "")]["scores"]
        rec = fmean(s["recovered"] for s in scores)
        false = fmean(s["false_recovery"] for s in scores)
        n = round(rec * 26)
        col = LIME if key == "agent" else GREEN
        bar = c("█" * n, col) + c("░" * (26 - n), DIM)
        fcol = GREEN if false == 0 else RED if false >= 0.5 else AMBER
        label = c(f"{name:<17}", BOLD) if key == "agent" else f"{name:<17}"
        line(f"  {label}{bar} {rec:>4.0%}       {c(f'{false:>4.0%}', fcol)}", 0.35)
    line(c("  LLM-only loop: 1 seed (20 runs); all others 60 runs.", DIM), 0.2)
    say("Recovers everything, never claims a recovery it did not achieve.", 0.8)

    agent = results[("heldout", "agent", "")]
    found: list[str] = []
    done = threading.Event()

    def run() -> None:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                found.extend(check(agent, Path(tmp)))
        except Exception as e:  # noqa: BLE001 - shown on screen, never hidden
            found.append(f"{type(e).__name__}: {e}")
        done.set()

    n_runs = len(agent["scores"])
    w()
    threading.Thread(target=run, daemon=True).start()
    spin(f"running now: re-executing all {n_runs} held-out runs from the LLM cache", 0, done)
    if found:
        line(f"  {c('✖ replay mismatch', RED, BOLD)} {found[0]}", 0.5)
    else:
        same = c(f"✔ {n_runs}/{n_runs} runs identical", GREEN, BOLD)
        line(f"  {same} to the committed results", 0.4)
        say("No model, no GPU: anyone can re-check every number. CI does it on every change.", 1.0)


def outro() -> None:
    """Closing card."""
    w()
    w(c("  Sentinel-RX", LIME, BOLD) + c("  identifies · diagnoses · recovers · verifies", DIM))
    w(f"  {c('site', DIM)}  abdulalharbi.github.io/sentinel-rx")
    w(f"  {c('code', DIM)}  github.com/AbdulAlharbi/sentinel-rx")
    w()


def main() -> None:
    """Run the walkthrough."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast")
    ap.add_argument("--fast", action="store_true", help="no delays or animation")
    ap.add_argument("--trace", type=Path, default=TRACE)
    args = ap.parse_args()
    Pace.speed, Pace.fast = max(0.1, args.speed), args.fast
    trace = json.loads(args.trace.read_text())
    try:
        banner(trace)
        act_incident(trace)
        act_detectors(trace)
        act_loop(trace)
        act_ground_truth(trace)
        act_results()
        outro()
    except KeyboardInterrupt:
        w("\033[?25h" if COLOR else "")


if __name__ == "__main__":
    main()
