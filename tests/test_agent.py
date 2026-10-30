"""Agent loop, safety layer, schema validation and LLM cache — all without Ollama."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.llm import LLMClient, ReplayMissError, parse_json
from agent.loop import SentinelAgent
from agent.safety import SafetyLayer, observable_health
from agent.schema import validate
from agent.trace import Hypothesis
from bench.configs import build_agent
from env.sim.sim_env import SimEnv
from env.types import Action
from tests.conftest import make_scenario


class FakeLLM(LLMClient):
    """Scripted responses keyed by prompt phase (diagnose/plan/verify) in order."""

    def __init__(self, script: list[str]) -> None:
        super().__init__(model="fake", replay=False, cache_dir=Path("/nonexistent"))
        self.script = list(script)
        self.prompts: list[str] = []

    def complete(self, prompt, system="", json_mode=True):  # noqa: D102
        from agent.llm import LLMResponse

        self.prompts.append(prompt)
        text = self.script.pop(0) if self.script else '{"hypotheses":[],"plan":[],"checks":[]}'
        r = LLMResponse(text, 100, 20, 0.01, False, "fake")
        self.usage.add(r)
        return r


def _demo_env():
    return SimEnv(
        make_scenario(
            [
                ("memory_leak", "api", 45, {"rate": 1.5}),
                ("db_conn_exhaustion", "db", 50, {}),
                ("c2_beacon", "worker", 45, {"interval": 30, "jitter": 0.2}),
            ]
        )
    )


DIAG = json.dumps(
    {
        "hypotheses": [
            {"service": "worker", "type": "c2_beacon", "confidence": 0.9},
            {"service": "api", "type": "memory_leak", "confidence": 0.8},
            {"service": "db", "type": "db_conn_exhaustion", "confidence": 0.8},
        ],
        "checks": [
            {"tool": "logs", "service": "db", "grep": "too many"},
            {"tool": "flows", "service": "worker"},
        ],
    }
)
PLAN_BAD_ORDER = json.dumps(
    {
        "hypotheses": json.loads(DIAG)["hypotheses"],
        "plan": [
            {"action": "restart_service", "target": "worker"},
            {"action": "restart_service", "target": "api"},
            {"action": "scale_connections", "target": "db"},
            {"action": "block_egress", "target": "worker"},
        ],
    }
)


def test_loop_recovers_three_faults_with_safety_reordering():
    env = _demo_env()
    llm = FakeLLM([DIAG, PLAN_BAD_ORDER])
    agent = SentinelAgent(llm)
    trace = agent.run(env, budget=4)
    names = [(r["action"]["name"], r["action"]["target"], r["result"]) for r in trace.actions]
    assert names[0] == ("block_egress", "worker", "applied")  # containment moved first
    assert ("restart_service", "worker", "applied") in names
    assert env.gt_recovered() and trace.claimed_recovered
    assert env.faults[2].reinfections == 0
    assert {(h.service, h.type) for h in trace.final_hypotheses if h.status == "confirmed"} == {
        ("worker", "c2_beacon"),
        ("api", "memory_leak"),
        ("db", "db_conn_exhaustion"),
    }
    assert trace.llm_calls == 2


def test_loop_does_not_claim_recovery_when_fix_is_wrong():
    env = SimEnv(make_scenario([("bad_config_deploy", "api", 45, {"error": 0.5})]))
    diag = json.dumps(
        {"hypotheses": [{"service": "api", "type": "memory_leak", "confidence": 0.9}], "checks": []}
    )
    plan = json.dumps(
        {
            "hypotheses": [{"service": "api", "type": "memory_leak", "confidence": 0.9}],
            "plan": [{"action": "restart_service", "target": "api"}],
        }
    )
    llm = FakeLLM([diag, plan] * 3)
    trace = SentinelAgent(llm).run(env, budget=3)
    assert not trace.claimed_recovered and trace.escalated
    assert not env.gt_recovered()
    assert "Previous iterations" in llm.prompts[2]  # history fed back into the next diagnose


def test_no_verify_ablation_claims_falsely():
    env = SimEnv(make_scenario([("bad_config_deploy", "api", 45, {"error": 0.5})]))
    plan = json.dumps(
        {
            "hypotheses": [{"service": "api", "type": "memory_leak", "confidence": 0.9}],
            "plan": [{"action": "restart_service", "target": "api"}],
        }
    )
    trace = SentinelAgent(FakeLLM([plan, plan]), verify=False).run(env, budget=3)
    assert trace.claimed_recovered and not env.gt_recovered()


def test_safety_blocks_restart_before_containment_and_isolation_without_evidence():
    env = _demo_env()
    s = SafetyLayer()
    hyps = [Hypothesis("worker", "c2_beacon", 0.9, "likely")]
    plan = [
        Action("restart_service", "worker"),
        Action("isolate_host", "api"),
        Action("rm_rf", "db"),
        Action("restart_service", "zzz"),
    ]
    d = s.screen(plan, hyps, set(), env.list_services(), {})
    reasons = {x.action.key(): x.reason for x in d if not x.allowed}
    assert "restart_service(worker)" in reasons and "contain" in reasons["restart_service(worker)"]
    assert "isolate_host(api)" in reasons
    assert "rm_rf(db)" in reasons and "restart_service(zzz)" in reasons


def test_safety_rolls_back_regressions():
    env = SimEnv(make_scenario([("c2_beacon", "worker", 45, {"interval": 30})]))
    s = SafetyLayer()
    # block egress on api without evidence is refused; with a (wrong) hypothesis it is allowed,
    # and isolating a healthy critical service regresses health -> rolled back
    hyps = [Hypothesis("api", "c2_beacon", 0.6, "likely")]
    d = s.screen([Action("isolate_host", "api")], hyps, set(), env.list_services(), {})
    assert d[0].allowed
    rec = s.execute(env, d[0])
    assert rec["result"] == "rolled_back"
    assert env.get_status()["api"].state == "up"


def test_safety_disabled_lets_harm_through():
    env = _demo_env()
    s = SafetyLayer(enabled=False)
    hyps = [Hypothesis("worker", "c2_beacon", 0.9, "likely")]
    d = s.screen([Action("restart_service", "worker")], hyps, set(), env.list_services(), {})
    assert d[0].allowed
    s.execute(env, d[0])
    assert env.faults[2].reinfections == 1


def test_dry_run_applies_nothing():
    env = _demo_env()
    s = SafetyLayer(dry_run=True)
    d = s.screen([Action("restart_service", "api")], [], set(), env.list_services(), {})
    rec = s.execute(env, d[0])
    assert rec["result"] == "dry-run" and not env.gt_all_faults_resolved()


def test_observable_health_excludes_target():
    env = _demo_env()
    assert 0 <= observable_health(env) <= 1
    assert observable_health(env, exclude="api") != observable_health(env)


def test_schema_validator():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "array", "maxItems": 1, "items": {"type": "string", "enum": ["x"]}}
        },
    }
    assert validate({"a": ["x"]}, schema) == []
    assert validate({"a": ["y"]}, schema)
    assert validate({"a": ["x", "x"]}, schema)
    assert validate({}, schema)
    assert validate([], schema)


def test_parse_json_is_lenient():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('noise {"a": 1} trailing') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("not json")


def test_llm_cache_record_and_replay(tmp_path: Path, monkeypatch):
    calls = []

    def fake_call(self, prompt, system, json_mode):
        calls.append(prompt)
        return {"response": '{"ok": true}', "tokens_in": 5, "tokens_out": 3, "seconds": 0.1}

    monkeypatch.setattr(LLMClient, "_call_ollama", fake_call)
    rec = LLMClient("m", cache_dir=tmp_path)
    r1 = rec.complete("p", "s")
    r2 = rec.complete("p", "s")
    assert r1.text == r2.text and not r1.cached and r2.cached and len(calls) == 1
    assert rec.usage.calls == 2 and rec.usage.cache_hits == 1
    rep = LLMClient("m", replay=True, cache_dir=tmp_path)
    assert rep.complete("p", "s").cached
    with pytest.raises(ReplayMissError):
        rep.complete("other", "s")
    assert rec.key_for("p", "s") != LLMClient("m2", cache_dir=tmp_path).key_for("p", "s")


def test_build_agent_configs():
    for cfg in (
        "agent",
        "llm_only",
        "no_verify",
        "no_loop",
        "no_beacon",
        "no_safety",
        "single_shot",
        "runbook",
    ):
        assert build_agent(cfg, replay=True) is not None
    with pytest.raises(KeyError):
        build_agent("nope")
