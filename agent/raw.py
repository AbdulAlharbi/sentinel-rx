"""Raw telemetry summary used by the LLM-only variants (no detectors).

This is the "obvious" approach: hand the model a compact dump of what the dashboards show.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from env.base import Env
from env.types import METRIC_NAMES


def raw_summary(env: Env, window: float = 900.0, recent: float = 120.0) -> dict[str, Any]:
    """Per-service latest metrics vs. earliest, log level counts + top messages, flow pairs."""
    now = env.now()
    samples = env.get_metrics(window)
    by_svc: dict[str, list] = {}
    for s in samples:
        by_svc.setdefault(s.service, []).append(s)
    services: dict[str, Any] = {}
    status = env.get_status()
    for svc, rows in by_svc.items():
        rows.sort(key=lambda r: r.t)
        first, last = rows[0].values, rows[-1].values
        services[svc] = {
            "status": status[svc].state if svc in status else "?",
            "pool_size": status[svc].pool_size if svc in status else None,
            "config_version": status[svc].config_version if svc in status else None,
            "metrics_start": {m: _r(first.get(m)) for m in METRIC_NAMES},
            "metrics_now": {m: _r(last.get(m)) for m in METRIC_NAMES},
        }
    logs = env.get_logs(None, recent)
    for svc in services:
        lines = [line for line in logs if line.service == svc]
        levels = Counter(line.level for line in lines)
        top = Counter(
            line.msg[:80] for line in lines if line.level in ("ERROR", "WARN", "FATAL")
        ).most_common(3)
        services[svc]["log_levels_recent"] = dict(levels)
        services[svc]["top_warn_error_messages"] = [m for m, _ in top]
    flows = env.get_flows(window)
    pairs = Counter((f.src, f.dst) for f in flows)
    blocked = sum(1 for f in flows if f.blocked)
    known = set(env.list_services()) | {"monitor"}
    flow_rows = [
        {
            "src": s,
            "dst": d,
            "count": c,
            "external": d not in known,
            "allowlisted": d in env.external_allowlist(),
        }
        for (s, d), c in pairs.most_common(12)
    ]
    return {
        "t": now,
        "topology": env.get_topology(),
        "health": round(env.health_score(), 3),
        "services": services,
        "flow_pairs": flow_rows,
        "blocked_flows": blocked,
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 2)
