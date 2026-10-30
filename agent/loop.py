"""Sentinel-RX agent: a MAPE-K loop with a step budget.

Observe (detectors -> compact evidence) -> LLM hypotheses + targeted checks -> LLM plan ->
safety layer -> actions -> wait -> verify against the healthy baseline -> recovered | loop.
"""

from __future__ import annotations

import time
from typing import Any

from agent.base import Agent
from agent.detectors.evidence import Evidence, EvidenceBuilder
from agent.llm import LLMClient, parse_json
from agent.prompts import (
    DIAGNOSE_PROMPT,
    HYPOTHESIS_SCHEMA,
    PLAN_PROMPT,
    PLAN_SCHEMA,
    REPAIR_PROMPT,
    SYSTEM,
    dumps,
    render_history,
)
from agent.raw import raw_summary
from agent.safety import SafetyLayer, observable_health
from agent.schema import validate
from agent.trace import Hypothesis, RunTrace
from env.base import Env
from env.types import Action
from faults.registry import KNOWN_FAULT_TYPES

CHECK_WINDOW = 300.0
VERIFY_SCHEMA = {
    "type": "object",
    "required": ["recovered"],
    "properties": {"recovered": {"type": "boolean"}, "problems": {"type": "array"}},
}
VERIFY_PROMPT = """Post-remediation telemetry summary:
{summary}
Actions taken: {actions}
Task: decide whether the system is fully recovered (all services healthy, no attack traffic).
Return JSON: {{"recovered": true|false, "problems": [str]}}"""


class SentinelAgent(Agent):
    """The full agent; flags turn components off for ablations and the llm_only baseline."""

    def __init__(
        self,
        llm: LLMClient,
        name: str = "agent",
        use_detectors: bool = True,
        use_beacon: bool = True,
        verify: bool = True,
        safety: bool = True,
        max_iterations: int | None = None,
        dry_run: bool = False,
    ) -> None:
        self.llm = llm
        self.name = name
        self.use_detectors = use_detectors
        self.use_beacon = use_beacon
        self.do_verify = verify
        self.safety_enabled = safety
        self.max_iterations = max_iterations
        self.dry_run = dry_run

    @property
    def model(self) -> str:
        """LLM model name."""
        return self.llm.model

    # ---- LLM helpers ----------------------------------------------------------------------
    def _json_call(
        self, prompt: str, schema: dict[str, Any], trace: RunTrace, phase: str
    ) -> dict[str, Any]:
        """Call the LLM, validate JSON against ``schema``, auto-repair once."""
        r = self.llm.complete(prompt, SYSTEM)
        text = r.text
        try:
            obj = parse_json(text)
            errors = validate(obj, schema)
        except ValueError as e:
            obj, errors = {}, [f"invalid JSON: {e}"]
        rec = trace.step(
            phase,
            prompt_key=r.key,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            seconds=r.seconds,
            cached=r.cached,
            response=obj,
            errors=errors,
        )
        if errors:
            r2 = self.llm.complete(
                REPAIR_PROMPT.format(
                    error="; ".join(errors[:3]), answer=text[:1500], schema=dumps(schema)
                ),
                SYSTEM,
            )
            try:
                obj2 = parse_json(r2.text)
                errors2 = validate(obj2, schema)
            except ValueError as e:
                obj2, errors2 = {}, [f"invalid JSON: {e}"]
            trace.step(
                phase + "_repair",
                prompt_key=r2.key,
                tokens_in=r2.tokens_in,
                tokens_out=r2.tokens_out,
                seconds=r2.seconds,
                cached=r2.cached,
                response=obj2,
                errors=errors2,
            )
            if not errors2:
                obj = obj2
            else:
                rec["unrepaired"] = True
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _parse_hypotheses(obj: dict[str, Any], services: list[str]) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        seen: set[tuple[str, str]] = set()
        for h in obj.get("hypotheses", []) or []:
            if not isinstance(h, dict):
                continue
            svc = str(h.get("service", "")).strip()
            if svc not in services:
                continue
            ftype = str(h.get("type", "other")).strip().lower()
            if ftype not in KNOWN_FAULT_TYPES:
                ftype = "other"
            try:
                conf = max(0.0, min(1.0, float(h.get("confidence", 0.5))))
            except (TypeError, ValueError):
                conf = 0.5
            if (svc, ftype) in seen:
                continue
            seen.add((svc, ftype))
            out.append(
                Hypothesis(
                    svc,
                    ftype,
                    conf,
                    "likely" if conf >= 0.5 else "open",
                    str(h.get("rationale", ""))[:200],
                )
            )
        out.sort(key=lambda h: -h.confidence)
        return out[:5]

    # ---- checks ---------------------------------------------------------------------------
    @staticmethod
    def _run_check(env: Env, check: dict[str, Any]) -> dict[str, Any]:
        tool = check.get("tool", "status")
        svc = check.get("service")
        services = env.list_services()
        svc = svc if svc in services else None
        res: dict[str, Any] = {"tool": tool, "service": svc}
        if tool == "logs":
            grep = check.get("grep") or None
            lines = env.get_logs(svc, CHECK_WINDOW, grep)
            res["grep"] = grep
            res["count"] = len(lines)
            levels: dict[str, int] = {}
            for line in lines:
                levels[line.level] = levels.get(line.level, 0) + 1
            res["levels"] = levels
            res["examples"] = [
                f"{line.service} {line.level} {line.msg[:100]}" for line in lines[-3:]
            ]
        elif tool == "metrics":
            rows = [s for s in env.get_metrics(900.0) if svc is None or s.service == svc]
            by = {}
            for s in rows:
                by.setdefault(s.service, []).append(s)
            res["metrics"] = {
                k: {m: [_r(v[0].values.get(m)), _r(v[-1].values.get(m))] for m in v[-1].values}
                for k, v in by.items()
            }
        elif tool == "flows":
            flows = env.get_flows(CHECK_WINDOW)
            pairs: dict[tuple[str, str], int] = {}
            blocked = 0
            for f in flows:
                if svc and svc not in (f.src, f.dst):
                    continue
                pairs[(f.src, f.dst)] = pairs.get((f.src, f.dst), 0) + 1
                blocked += int(f.blocked)
            known = set(services) | {"monitor"}
            res["pairs"] = [
                {"src": s, "dst": d, "count": c, "external": d not in known}
                for (s, d), c in sorted(pairs.items(), key=lambda kv: -kv[1])[:8]
            ]
            res["blocked"] = blocked
        else:
            res["status"] = {
                k: v.to_dict() for k, v in env.get_status().items() if svc is None or k == svc
            }
        return res

    # ---- main loop ------------------------------------------------------------------------
    def run(self, env: Env, budget: int = 8) -> RunTrace:
        """Diagnose and recover within ``budget`` iterations."""
        t0 = time.perf_counter()
        trace = RunTrace(config=self.name, scenario_id="", seed_offset=0, model=self.llm.model)
        services = env.list_services()
        eb = EvidenceBuilder(use_beacon=self.use_beacon) if self.use_detectors else None
        safety = SafetyLayer(enabled=self.safety_enabled, dry_run=self.dry_run)
        hyps: list[Hypothesis] = []
        confirmed: dict[tuple[str, str], Hypothesis] = {}
        tried_keys: set[str] = set()
        history: list[dict[str, Any]] = []
        evidence: Evidence | None = None
        iterations = min(budget, self.max_iterations or budget)
        for i in range(iterations):
            # 1. observe
            if eb is not None:
                if evidence is None:
                    evidence = eb.build(env)
                ev_dict = evidence.to_dict()
                prompt_ev = evidence.to_prompt()
                beacon_hosts = {b.src for b in evidence.beacons}
                beacon_intervals = [b.median_interval_s for b in evidence.beacons]
            else:
                ev_dict = raw_summary(env)
                prompt_ev = ev_dict
                beacon_hosts, beacon_intervals = set(), []
            ev_dict["topology"] = env.get_topology()
            prompt_ev["topology"] = ev_dict["topology"]
            it = trace.step("iteration", i=i, evidence=ev_dict)
            # 2. hypotheses + checks
            diag = self._json_call(
                DIAGNOSE_PROMPT.format(evidence=dumps(prompt_ev), history=render_history(history)),
                HYPOTHESIS_SCHEMA,
                trace,
                "diagnose",
            )
            hyps = self._parse_hypotheses(diag, services) or hyps
            checks = [c for c in (diag.get("checks") or []) if isinstance(c, dict)][:3]
            check_results = [self._run_check(env, c) for c in checks]
            it["hypotheses"] = [h.to_dict() for h in hyps]
            it["checks"] = check_results
            # 3. plan
            plan_obj = self._json_call(
                PLAN_PROMPT.format(
                    hypotheses=dumps([h.to_dict() for h in hyps]), checks=dumps(check_results)
                ),
                PLAN_SCHEMA,
                trace,
                "plan",
            )
            hyps = self._parse_hypotheses(plan_obj, services) or hyps
            plan = _parse_plan(plan_obj)
            if plan and {a.key() for a in plan} <= tried_keys:
                # identical to something already tried: ask once more with an explicit warning
                warning = (
                    f"\nWARNING: {sorted(a.key() for a in plan)} were already applied and did not "
                    f"recover the system (remaining problems: "
                    f"{history[-1]['verification'] if history else 'n/a'}). "
                    "Propose DIFFERENT actions."
                )
                retry = self._json_call(
                    PLAN_PROMPT.format(
                        hypotheses=dumps([h.to_dict() for h in hyps]), checks=dumps(check_results)
                    )
                    + warning,
                    PLAN_SCHEMA,
                    trace,
                    "plan_retry",
                )
                new_plan = _parse_plan(retry)
                if new_plan and {a.key() for a in new_plan} - tried_keys:
                    plan = new_plan
                    hyps = self._parse_hypotheses(retry, services) or hyps
                    it["replanned"] = True
            it["plan"] = [a.key() for a in plan]
            # 4. safety + act
            status = {k: v.state for k, v in env.get_status().items()}
            decisions = safety.screen(plan, hyps, beacon_hosts, services, status)
            records = [safety.execute(env, d) for d in decisions]
            it["actions"] = records
            trace.actions.extend(records)
            t_after = env.now()
            # 5. verify
            if not self.do_verify:
                trace.claimed_recovered = True
                trace.verification = {"skipped": True}
                break
            wait = 60.0 if not beacon_hosts else min(240.0, 4.5 * max(beacon_intervals or [40.0]))
            env.wait(wait)
            if eb is not None:
                evidence = eb.verify(env, since=t_after)
                problems = evidence.problems()
            else:
                problems = self._llm_verify(env, records, trace)
            it["verification"] = {"waited_s": wait, "problems": problems}
            trace.verification = it["verification"]
            history.append(
                {
                    "i": i,
                    "actions": [
                        {
                            "action": r["action"]["name"],
                            "target": r["action"]["target"],
                            "result": r["result"],
                        }
                        for r in records
                    ],
                    "verification": "recovered" if not problems else "; ".join(problems[:4]),
                }
            )
            tried_keys |= {
                f"{r['action']['name']}({r['action']['target']})"
                for r in records
                if r.get("result") == "applied"
            }
            acted = {r["action"]["target"] for r in records if r.get("result") == "applied"}
            for h in hyps:
                if h.service in acted and not any(h.service in p for p in problems):
                    h.status = "confirmed"
                    confirmed[(h.service, h.type)] = h
            if not problems:
                trace.claimed_recovered = True
                break
        else:
            trace.escalated = True
            trace.notes.append("budget exhausted; escalating to a human with the trace")
        if not trace.claimed_recovered and not trace.escalated:
            trace.escalated = True
        merged = dict(confirmed)
        for h in hyps:
            merged.setdefault((h.service, h.type), h)
        trace.final_hypotheses = sorted(
            merged.values(), key=lambda h: (h.status != "confirmed", -h.confidence)
        )
        u = self.llm.usage
        trace.llm_calls, trace.tokens_in, trace.tokens_out, trace.llm_seconds = (
            u.calls,
            u.tokens_in,
            u.tokens_out,
            u.seconds,
        )
        trace.notes.append(
            f"safety decisions: {len(safety.decisions)}; llm cache hits: {u.cache_hits}/{u.calls}"
        )
        trace.wall_seconds = time.perf_counter() - t0
        return trace

    def _llm_verify(self, env: Env, records: list[dict[str, Any]], trace: RunTrace) -> list[str]:
        """LLM-only verification (no detectors): the model judges a raw post-action summary."""
        summary = raw_summary(env, window=120.0, recent=120.0)
        acts = (
            ", ".join(
                f"{r['action']['name']}({r['action']['target']})={r['result']}" for r in records
            )
            or "none"
        )
        obj = self._json_call(
            VERIFY_PROMPT.format(summary=dumps(summary), actions=acts),
            VERIFY_SCHEMA,
            trace,
            "verify",
        )
        if obj.get("recovered") is True and observable_health(env) >= 0.9:
            return []
        probs = [str(p) for p in (obj.get("problems") or [])][:4]
        return probs or ["model judged not recovered"]


def _parse_plan(obj: dict[str, Any]) -> list[Action]:
    return [
        Action(str(p.get("action", "")), str(p.get("target", "")))
        for p in (obj.get("plan") or [])
        if isinstance(p, dict)
    ][:5]


def _r(v: Any) -> Any:
    return None if v is None else round(float(v), 2)
