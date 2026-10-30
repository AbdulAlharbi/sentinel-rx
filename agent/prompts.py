"""Prompt templates and JSON schemas for the agent loop (compact by design)."""

from __future__ import annotations

import json
from typing import Any

from env.types import ACTION_ALLOWLIST
from faults.registry import FAULT_CLASSES, KNOWN_FAULT_TYPES

ACTION_DOCS: dict[str, str] = {
    "restart_service": "restart the process (fixes leaks, crashes, stuck connections; not config)",
    "rollback_config": "roll back the last config push on the service",
    "scale_connections": "double the service's connection pool",
    "clear_disk": "reclaim disk space on the service's volume",
    "isolate_host": "quarantine the host from the network (service goes offline until restart)",
    "block_egress": "block outbound traffic to external destinations (service keeps serving)",
    "flush_cache": "flush all cache keys",
    "rate_limit_auth": "throttle the login endpoint on the service",
}

HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypotheses", "checks"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["service", "type", "confidence"],
                "properties": {
                    "service": {"type": "string"},
                    "type": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "checks": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["tool"],
                "properties": {
                    "tool": {"type": "string", "enum": ["logs", "metrics", "flows", "status"]},
                    "service": {"type": "string"},
                    "grep": {"type": "string"},
                },
            },
        },
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypotheses", "plan"],
    "properties": {
        "hypotheses": HYPOTHESIS_SCHEMA["properties"]["hypotheses"],
        "plan": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["action", "target"],
                "properties": {
                    "action": {"type": "string", "enum": list(ACTION_ALLOWLIST)},
                    "target": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def fault_taxonomy() -> str:
    """One line per known fault type (what an SRE knows from runbooks)."""
    lines = []
    for t in KNOWN_FAULT_TYPES:
        cls = FAULT_CLASSES[t]
        fix = " | ".join(
            " then ".join(k.split("(")[0] for k in seq)
            for seq in cls(_spec(t), None).correct_sequences()
        )  # type: ignore[arg-type]
        lines.append(f"- {t}: {cls.symptoms} Fix: {fix}.")
    lines.append("- other: anything not listed (describe it in rationale).")
    return "\n".join(lines)


def _spec(t: str):
    from faults.base import FaultSpec

    return FaultSpec(t, "svc", 0, {})


SYSTEM = f"""You are Sentinel-RX, an SRE + security incident-response agent for a small service mesh
(the dependency topology is given in the evidence). Several faults may be active AT ONCE, and a
security incident may hide inside an operational one. Reason from the evidence; be concise;
answer ONLY with JSON matching the requested schema.

Fault taxonomy (type: symptoms. Fix):
{fault_taxonomy()}

Actions (allowlist): {", ".join(f"{k} = {v}" for k, v in ACTION_DOCS.items())}.
Rules: a dependency's symptoms propagate upstream, so prefer the deepest anomalous service as the
cause. Restarting fixes nothing if the cause is a bad config. If a host is beaconing to an unknown
external destination, CONTAIN it first (block_egress or isolate_host) and only then restart it.
Never act on services with no evidence."""

DIAGNOSE_PROMPT = """Evidence (ranked by detectors; higher score = more likely root cause):
{evidence}
{history}
Task: rank root-cause hypotheses (one per distinct fault; use types from the taxonomy or "other"),
then request up to 3 targeted checks to confirm/refute them.
Return JSON: {{"hypotheses":[{{"service":str,"type":str,"confidence":0-1,"rationale":str}}],
"checks":[{{"tool":"logs|metrics|flows|status","service":str,"grep":str}}]}}"""

PLAN_PROMPT = """Hypotheses so far:
{hypotheses}
Check results:
{checks}
Task: update the hypotheses (drop refuted ones) and give an ORDERED remediation plan: one action per
confirmed fault, containment before restart for compromised hosts, nothing for services without
evidence. If a previous plan did not fully recover the system, change strategy.
Return JSON: {{"hypotheses":[{{"service":str,"type":str,"confidence":0-1,"rationale":str}}],
"plan":[{{"action":str,"target":str,"reason":str}}]}}"""

REPAIR_PROMPT = """Your previous answer was not valid: {error}
Previous answer: {answer}
Return ONLY corrected JSON for the same task with schema: {schema}"""


def render_history(history: list[dict[str, Any]]) -> str:
    """Compact summary of previous iterations for the diagnose prompt."""
    if not history:
        return ""
    parts = ["Previous iterations:"]
    for h in history[-3:]:
        acts = (
            ", ".join(f"{a['action']}({a['target']})->{a['result']}" for a in h.get("actions", []))
            or "none"
        )
        parts.append(
            f"- iter {h['i']}: actions: {acts}. verification: {h.get('verification', 'n/a')}"
        )
    return "\n".join(parts)


def dumps(obj: Any) -> str:
    """Deterministic compact JSON for prompts."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)
