"""single_shot baseline: one LLM call on a raw telemetry summary, act, declare victory."""

from __future__ import annotations

import time

from agent.base import Agent
from agent.llm import LLMClient, parse_json
from agent.loop import SentinelAgent
from agent.prompts import PLAN_SCHEMA, SYSTEM, dumps
from agent.raw import raw_summary
from agent.schema import validate
from agent.trace import RunTrace
from env.base import Env
from env.types import Action

SINGLE_SHOT_PROMPT = """Telemetry summary:
{summary}
Task: identify every root cause and give an ordered remediation plan.
Return JSON: {{"hypotheses":[{{"service":str,"type":str,"confidence":0-1,"rationale":str}}],
"plan":[{{"action":str,"target":str,"reason":str}}]}}"""


class SingleShotAgent(Agent):
    """No loop, no detectors, no verification: the naive LLM approach."""

    name = "single_shot"

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    @property
    def model(self) -> str:
        """LLM model name."""
        return self.llm.model

    def run(self, env: Env, budget: int = 8) -> RunTrace:
        """One call, then act."""
        t0 = time.perf_counter()
        trace = RunTrace(config=self.name, scenario_id="", seed_offset=0, model=self.llm.model)
        services = env.list_services()
        summary = raw_summary(env)
        r = self.llm.complete(SINGLE_SHOT_PROMPT.format(summary=dumps(summary)), SYSTEM)
        try:
            obj = parse_json(r.text)
            errors = validate(obj, PLAN_SCHEMA)
        except ValueError as e:
            obj, errors = {}, [str(e)]
        it = trace.step(
            "iteration", i=0, evidence=summary, prompt_key=r.key, response=obj, errors=errors
        )
        hyps = SentinelAgent._parse_hypotheses(obj if isinstance(obj, dict) else {}, services)
        plan = (
            [
                Action(str(p.get("action", "")), str(p.get("target", "")))
                for p in (obj.get("plan") or [])
                if isinstance(p, dict)
            ][:5]
            if isinstance(obj, dict)
            else []
        )
        records = []
        for a in plan:
            res = env.apply_action(a)
            records.append(
                {
                    "action": a.to_dict(),
                    "allowed": True,
                    "applied": res.applied,
                    "message": res.message,
                    "result": "applied" if res.applied else "rejected",
                }
            )
        it["hypotheses"] = [h.to_dict() for h in hyps]
        it["plan"] = [a.key() for a in plan]
        it["actions"] = records
        trace.actions = records
        trace.final_hypotheses = hyps
        trace.claimed_recovered = True  # by construction: no verification step
        u = self.llm.usage
        trace.llm_calls, trace.tokens_in, trace.tokens_out, trace.llm_seconds = (
            u.calls,
            u.tokens_in,
            u.tokens_out,
            u.seconds,
        )
        trace.wall_seconds = time.perf_counter() - t0
        return trace
