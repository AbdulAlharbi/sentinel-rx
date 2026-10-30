"""make report: results/*.json -> results/summary.md, docs/figures/*.png, site/data.json.

Picks the most recent results file per (split, config, tag). Never runs anything itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bench.aggregate import fmt  # noqa: E402
from bench.configs import ABLATIONS, DESCRIPTIONS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
FIGURES = ROOT / "docs" / "figures"
SITE_DATA = ROOT / "site" / "data.json"

# validated categorical palette (fixed order per config; see docs/DESIGN.md)
PALETTE = {
    "agent": "#2a78d6",
    "runbook": "#eb6834",
    "single_shot": "#1baf7a",
    "llm_only": "#eda100",
    "no_verify": "#e87ba4",
    "no_loop": "#008300",
    "no_beacon": "#6250d6",
    "no_safety": "#e34948",
}
TEXT = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e6e5e1"
MAIN_CONFIGS = ("agent", "runbook", "single_shot", "llm_only")
PERTURBATIONS = (
    "noisy_logs",
    "missing_metrics",
    "renamed_services",
    "timestamp_jitter",
    "unseen_fault",
)


def load_results() -> dict[tuple[str, str, str], dict[str, Any]]:
    """Latest results file per (split, config, tag)."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in sorted(RESULTS.glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "meta" not in d:
            continue
        m = d["meta"]
        key = (m["split"], m["config"], m.get("tag", ""))
        d["meta"]["file"] = path.name
        out[key] = d  # sorted by name = timestamp, so the last wins
    return out


def _style(ax, title: str, ylabel: str = "") -> None:
    ax.set_title(title, color=TEXT, fontsize=12, loc="left", pad=10)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_facecolor("#fcfcfb")


def _save(fig, name: str) -> str:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.patch.set_facecolor("#fcfcfb")
    fig.tight_layout()
    fig.savefig(FIGURES / name, dpi=150)
    plt.close(fig)
    return name


def _m(res: dict[str, Any], metric: str) -> dict[str, float]:
    return res["summary"]["metrics"][metric]


def fig_results_by_config(
    res: dict, metrics=("top1", "fault_recall", "recovered", "false_recovery")
) -> str | None:
    """Grouped bars with 95% CI error bars, held-out split."""
    configs = [c for c in MAIN_CONFIGS if ("heldout", c, "") in res]
    if not configs:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.2))
    width = 0.8 / len(configs)
    labels = {
        "top1": "top-1 root cause",
        "fault_recall": "per-fault recall",
        "recovered": "recovered",
        "false_recovery": "false 'recovered' claims",
    }
    for i, c in enumerate(configs):
        r = res[("heldout", c, "")]
        xs = [j + i * width for j in range(len(metrics))]
        means = [100 * _m(r, m)["mean"] for m in metrics]
        err = [
            [100 * (_m(r, m)["mean"] - _m(r, m)["ci_lo"]) for m in metrics],
            [100 * (_m(r, m)["ci_hi"] - _m(r, m)["mean"]) for m in metrics],
        ]
        ax.bar(
            xs,
            means,
            width * 0.92,
            color=PALETTE[c],
            label=c,
            yerr=err,
            error_kw={"ecolor": MUTED, "capsize": 2, "linewidth": 1},
        )
    ax.set_xticks(
        [j + width * (len(configs) - 1) / 2 for j in range(len(metrics))],
        [labels[m] for m in metrics],
    )
    ax.set_ylim(0, 105)
    _style(ax, "Held-out results by configuration (mean, 95% bootstrap CI)", "% of runs")
    ax.legend(frameon=False, fontsize=9, ncol=len(configs), loc="upper right")
    return _save(fig, "results-by-config.png")


def fig_by_n_faults(res: dict) -> str | None:
    """Recovery and top-1 vs number of concurrent faults, per config."""
    configs = [c for c in MAIN_CONFIGS if ("heldout", c, "") in res]
    if not configs:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8), sharey=True)
    for ax, metric, title in zip(
        axes, ("top1", "recovered"), ("Top-1 root cause", "Recovered"), strict=True
    ):
        for c in configs:
            by = res[("heldout", c, "")]["summary"]["by_n_faults"]
            ns = sorted(int(k) for k in by)
            ys = [100 * by[str(n)][metric]["mean"] for n in ns]
            lo = [100 * by[str(n)][metric]["ci_lo"] for n in ns]
            hi = [100 * by[str(n)][metric]["ci_hi"] for n in ns]
            ax.plot(ns, ys, marker="o", markersize=6, linewidth=2, color=PALETTE[c], label=c)
            ax.fill_between(ns, lo, hi, color=PALETTE[c], alpha=0.12, linewidth=0)
        ax.set_xticks([1, 2, 3])
        ax.set_xlabel("concurrent faults", color=MUTED)
        _style(ax, title + " by number of concurrent faults", "% of runs")
        ax.set_ylim(0, 105)
    axes[0].legend(frameon=False, fontsize=9)
    return _save(fig, "accuracy-by-number-of-concurrent-faults.png")


def fig_robustness(res: dict, config: str = "agent") -> tuple[str | None, list[dict[str, Any]]]:
    """Performance drop of the agent under each perturbation vs. clean held-out."""
    base = res.get(("heldout", config, ""))
    rows = []
    if base is None:
        return None, rows
    for p in PERTURBATIONS:
        r = res.get((f"perturbations/{p}", config, ""))
        if r is None:
            continue
        rows.append(
            {
                "perturbation": p,
                **{
                    m: {
                        "clean": _m(base, m)["mean"],
                        "perturbed": _m(r, m)["mean"],
                        "drop": _m(base, m)["mean"] - _m(r, m)["mean"],
                    }
                    for m in (
                        "top1",
                        "fault_recall",
                        "recovered",
                        "false_recovery",
                        "harmful_actions",
                    )
                },
                "n_runs": r["summary"]["n_runs"],
            }
        )
    small = res.get(("heldout", config, "small_model"))
    if small is not None:
        rows.append(
            {
                "perturbation": f"smaller LLM ({small['meta']['model']})",
                **{
                    m: {
                        "clean": _m(base, m)["mean"],
                        "perturbed": _m(small, m)["mean"],
                        "drop": _m(base, m)["mean"] - _m(small, m)["mean"],
                    }
                    for m in (
                        "top1",
                        "fault_recall",
                        "recovered",
                        "false_recovery",
                        "harmful_actions",
                    )
                },
                "n_runs": small["summary"]["n_runs"],
            }
        )
    if not rows:
        return None, rows
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(rows) + 1.8))
    names = [r["perturbation"] for r in rows]
    ys = range(len(rows))
    h = 0.38
    ax.barh(
        [y + h / 2 for y in ys],
        [100 * r["recovered"]["perturbed"] for r in rows],
        h,
        color=PALETTE["agent"],
        label="recovered (perturbed)",
    )
    ax.barh(
        [y - h / 2 for y in ys],
        [100 * r["top1"]["perturbed"] for r in rows],
        h,
        color="#9dbfe8",
        label="top-1 (perturbed)",
    )
    ax.axvline(
        100 * base["summary"]["metrics"]["recovered"]["mean"],
        color=PALETTE["agent"],
        linestyle="--",
        linewidth=1.2,
        label="recovered (clean held-out)",
    )
    ax.axvline(
        100 * base["summary"]["metrics"]["top1"]["mean"],
        color="#9dbfe8",
        linestyle="--",
        linewidth=1.2,
        label="top-1 (clean held-out)",
    )
    ax.set_yticks(list(ys), names)
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    _style(ax, "Robustness: agent under perturbations vs. clean held-out", "")
    ax.set_xlabel("% of runs", color=MUTED)
    ax.xaxis.grid(True, color=GRID)
    ax.yaxis.grid(False)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    return _save(fig, "robustness-drop.png"), rows


def fig_ablations(res: dict) -> tuple[str | None, list[dict[str, Any]]]:
    """Agent minus each ablation (held-out, 1 seed)."""
    base = res.get(("heldout", "agent", ""))
    rows = []
    if base is None:
        return None, rows
    for a in ABLATIONS:
        r = res.get(("heldout", a, ""))
        if r is None:
            continue
        rows.append(
            {
                "ablation": a,
                **{
                    m: {
                        "agent": _m(base, m)["mean"],
                        "ablated": _m(r, m)["mean"],
                        "delta": _m(r, m)["mean"] - _m(base, m)["mean"],
                    }
                    for m in (
                        "top1",
                        "fault_recall",
                        "recovered",
                        "false_recovery",
                        "harmful_actions",
                        "unnecessary_actions",
                        "llm_calls",
                    )
                },
                "n_runs": r["summary"]["n_runs"],
            }
        )
    if not rows:
        return None, rows
    metrics = ("recovered", "false_recovery", "harmful_actions")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    width = 0.8 / len(metrics)
    shades = {
        "recovered": PALETTE["agent"],
        "false_recovery": PALETTE["runbook"],
        "harmful_actions": PALETTE["no_safety"],
    }
    for i, m in enumerate(metrics):
        xs = [j + i * width for j in range(len(rows))]
        scale = 100 if m in ("recovered", "false_recovery") else 100
        ax.bar(
            xs,
            [scale * r[m]["delta"] for r in rows],
            width * 0.9,
            color=shades[m],
            label=m.replace("_", " ") + (" (pts)" if m != "harmful_actions" else " (x100 per run)"),
        )
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.set_xticks([j + width for j in range(len(rows))], [r["ablation"] for r in rows])
    _style(ax, "Ablations: change vs. full agent when a component is removed", "delta")
    ax.legend(frameon=False, fontsize=8)
    return _save(fig, "ablation-deltas.png"), rows


def fig_cost(res: dict) -> str | None:
    """Tokens per incident vs recovery rate."""
    pts = [
        (c, res[("heldout", c, "")]) for c in MAIN_CONFIGS + ABLATIONS if ("heldout", c, "") in res
    ]
    if not pts:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    for c, r in pts:
        x = _m(r, "tokens")["mean"]
        y = 100 * _m(r, "recovered")["mean"]
        ax.scatter([x], [y], s=70, color=PALETTE[c], edgecolor="#fcfcfb", linewidth=1.5, zorder=3)
        ax.annotate(c, (x, y), textcoords="offset points", xytext=(7, 4), fontsize=9, color=TEXT)
    _style(ax, "Cost vs. accuracy (held-out)", "recovered, % of runs")
    ax.set_xlabel("LLM tokens per incident (prompt + completion)", color=MUTED)
    ax.set_ylim(0, 105)
    ax.set_xlim(left=-50)
    return _save(fig, "cost-vs-accuracy.png")


def fig_beacon_pr() -> dict[str, Any] | None:
    """Beacon detector precision/recall curve on its synthetic test set."""
    path = ROOT / "models" / "beacon_metrics.json"
    if not path.exists():
        return None
    m = json.loads(path.read_text())
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(
        m["pr_curve"]["recall"], m["pr_curve"]["precision"], color=PALETTE["agent"], linewidth=2
    )
    ax.set_xlabel("recall", color=MUTED)
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    _style(
        ax,
        f"Beacon detector PR curve (AP = {m['average_precision']:.3f}, synthetic test n={m['n_test']})",
        "precision",
    )
    _save(fig, "beacon-detector-PR-curve.png")
    return {k: v for k, v in m.items() if k != "pr_curve"}


def pick_sample_traces(res: dict, n: int = 4) -> list[dict[str, Any]]:
    """A few held-out agent traces for the site's replay viewer (prefer multi-fault + security)."""
    r = res.get(("heldout", "agent", ""))
    if r is None:
        return []
    trace_dir = ROOT / r["meta"]["trace_dir"]
    scored = sorted(
        r["scores"],
        key=lambda s: (-s["n_faults"], -float(s.get("has_security", False)), s["scenario_id"]),
    )
    out = []
    seen = set()
    for s in scored:
        if s["scenario_id"] in seen:
            continue
        p = trace_dir / f"{s['scenario_id']}_s{s['seed_offset']}.json"
        if not p.exists():
            continue
        t = json.loads(p.read_text())
        t["score"] = {
            k: s[k]
            for k in (
                "top1",
                "fault_recall",
                "recovered",
                "false_recovery",
                "harmful_actions",
                "llm_calls",
                "tokens",
            )
        }
        out.append(t)
        seen.add(s["scenario_id"])
        if len(out) == n:
            break
    return out


def summary_md(res: dict, robustness: list, ablations: list, beacon: dict | None) -> str:
    """Markdown tables for results/summary.md and the README."""
    lines = ["# Results summary (generated by `make report`)", ""]
    configs = [c for c in MAIN_CONFIGS if ("heldout", c, "") in res]
    if configs:
        lines += [
            "## Held-out (headline)",
            "",
            "| config | top-1 | top-3 | per-fault recall | recovered | false recovery | harmful / run | unnecessary / run | isolate-before-restart | LLM calls | tokens | n |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in configs:
            r = res[("heldout", c, "")]
            m = r["summary"]["metrics"]
            lines.append(
                f"| {c} | {fmt(m['top1'])} | {fmt(m['top3'])} | {fmt(m['fault_recall'])} | {fmt(m['recovered'])} | {fmt(m['false_recovery'])} | {fmt(m['harmful_actions'], False)} | {fmt(m['unnecessary_actions'], False)} | {fmt(m['isolated_before_restart'])} | {m['llm_calls']['mean']:.1f} | {m['tokens']['mean']:.0f} | {r['summary']['n_runs']} |"
            )
        lines += [
            "",
            "Model: "
            + ", ".join(sorted({res[("heldout", c, "")]["meta"]["model"] for c in configs}))
            + "; files: "
            + ", ".join(res[("heldout", c, "")]["meta"]["file"] for c in configs),
            "",
        ]
        lines += [
            "### By number of concurrent faults (recovered / top-1)",
            "",
            "| config | 1 fault | 2 faults | 3 faults |",
            "|---|---|---|---|",
        ]
        for c in configs:
            by = res[("heldout", c, "")]["summary"]["by_n_faults"]
            cells = [
                f"{100 * by[k]['recovered']['mean']:.0f}% / {100 * by[k]['top1']['mean']:.0f}%"
                if k in by
                else "n/a"
                for k in ("1", "2", "3")
            ]
            lines.append(f"| {c} | " + " | ".join(cells) + " |")
        lines.append("")
    if robustness:
        lines += [
            "## Robustness (agent, held-out variants, 1 seed)",
            "",
            "| perturbation | top-1 | recovered | false recovery | harmful / run | drop in recovered (pts) | n |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in robustness:
            lines.append(
                f"| {r['perturbation']} | {100 * r['top1']['perturbed']:.0f}% | {100 * r['recovered']['perturbed']:.0f}% | {100 * r['false_recovery']['perturbed']:.0f}% | {r['harmful_actions']['perturbed']:.2f} | {100 * r['recovered']['drop']:+.0f} | {r['n_runs']} |"
            )
        lines.append("")
    if ablations:
        lines += [
            "## Ablations (held-out, 1 seed; delta = ablated - full agent)",
            "",
            "| ablation | top-1 | recovered | false recovery | harmful / run | unnecessary / run | LLM calls | n |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in ablations:
            lines.append(
                f"| {r['ablation']} | {100 * r['top1']['ablated']:.0f}% ({100 * r['top1']['delta']:+.0f}) | {100 * r['recovered']['ablated']:.0f}% ({100 * r['recovered']['delta']:+.0f}) | {100 * r['false_recovery']['ablated']:.0f}% ({100 * r['false_recovery']['delta']:+.0f}) | {r['harmful_actions']['ablated']:.2f} ({r['harmful_actions']['delta']:+.2f}) | {r['unnecessary_actions']['ablated']:.2f} ({r['unnecessary_actions']['delta']:+.2f}) | {r['llm_calls']['ablated']:.1f} | {r['n_runs']} |"
            )
        lines.append("")
    if beacon:
        lines += [
            "## Beacon detector",
            "",
            f"Synthetic held-out test (n={beacon['n_test']}): precision {beacon['precision']:.3f}, recall {beacon['recall']:.3f}, AP {beacon['average_precision']:.3f}. False-positive rate on health checks: {beacon['false_positive_rate_by_kind']['health']:.3f}, on update polls: {beacon['false_positive_rate_by_kind']['update']:.3f}. Simulator dev scenarios: precision {beacon['simulator_dev_eval']['precision']:.2f}, recall {beacon['simulator_dev_eval']['recall']:.2f}.",
            "",
        ]
    dev = [(k, v) for k, v in res.items() if k[0] == "dev"]
    if dev:
        lines += [
            "## Dev split (iteration history; see docs/RESULTS.md)",
            "",
            "| config | tag | top-1 | recovered | false recovery | harmful / run | n | file |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for (_, c, tag), r in sorted(dev, key=lambda kv: kv[1]["meta"]["file"]):
            m = r["summary"]["metrics"]
            lines.append(
                f"| {c} | {tag} | {fmt(m['top1'])} | {fmt(m['recovered'])} | {fmt(m['false_recovery'])} | {fmt(m['harmful_actions'], False)} | {r['summary']['n_runs']} | {r['meta']['file']} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    """Build everything."""
    res = load_results()
    figs = [fig_results_by_config(res), fig_by_n_faults(res), fig_cost(res)]
    rob_fig, robustness = fig_robustness(res)
    abl_fig, ablations = fig_ablations(res)
    beacon = fig_beacon_pr()
    figs += [rob_fig, abl_fig]
    md = summary_md(res, robustness, ablations, beacon)
    (RESULTS / "summary.md").write_text(md)
    try:
        git = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        git = "unknown"
    data = {
        "generated_git": git,
        "descriptions": DESCRIPTIONS,
        "heldout": {
            c: {
                "summary": res[("heldout", c, "")]["summary"],
                "meta": res[("heldout", c, "")]["meta"],
            }
            for c in MAIN_CONFIGS + ABLATIONS
            if ("heldout", c, "") in res
        },
        "small_model": {
            "summary": res[("heldout", "agent", "small_model")]["summary"],
            "meta": res[("heldout", "agent", "small_model")]["meta"],
        }
        if ("heldout", "agent", "small_model") in res
        else None,
        "robustness": robustness,
        "ablations": ablations,
        "beacon": beacon,
        "dev_history": [
            {
                "config": k[1],
                "tag": k[2],
                "metrics": v["summary"]["metrics"],
                "file": v["meta"]["file"],
            }
            for k, v in sorted(res.items(), key=lambda kv: kv[1]["meta"]["file"])
            if k[0] == "dev"
        ],
        "sample_traces": pick_sample_traces(res),
        "heldout_run_log": (RESULTS / "heldout_runs.log").read_text().splitlines()
        if (RESULTS / "heldout_runs.log").exists()
        else [],
    }
    SITE_DATA.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA.write_text(json.dumps(data, default=str))
    site_figs = SITE_DATA.parent / "figures"
    site_figs.mkdir(exist_ok=True)
    for png in FIGURES.glob("*.png"):
        shutil.copy(png, site_figs / png.name)
    print(md)
    print("figures:", [f for f in figs if f], "+ beacon-detector-PR-curve.png")


if __name__ == "__main__":
    main()
