"""Each fault produces its documented symptoms and is resolved by its documented actions."""

from __future__ import annotations

import pytest

from env.sim.sim_env import SimEnv
from env.types import Action
from faults.registry import FAULT_CLASSES, VALID_TARGETS
from tests.conftest import make_scenario


def _env(ftype: str, target: str, params: dict | None = None) -> SimEnv:
    return SimEnv(make_scenario([(ftype, target, 45, params or {})]))


def test_memory_leak_symptoms():
    env = _env("memory_leak", "api", {"rate": 1.5})
    m = env.world.latest["api"]
    assert m["mem"] > 85 and m["latency_p95"] > 300
    assert any("GC pause" in line.msg for line in env.get_logs("api", 200))


def test_db_conn_exhaustion_symptoms():
    env = _env("db_conn_exhaustion", "db")
    assert env.world.latest["db"]["conn_count"] >= 95
    assert any("too many connections" in line.msg for line in env.get_logs("db", 100))


def test_disk_full_symptoms():
    env = _env("disk_full", "db", {"rate": 5})
    assert env.world.latest["db"]["disk_used"] >= 99
    assert env.get_logs("db", 100, grep="No space left")


def test_latency_injection_symptoms():
    env = _env("latency_injection", "db", {"ms": 400})
    assert env.world.latest["db"]["latency_p50"] > 300
    assert env.world.latest["api"]["latency_p50"] > 250  # propagated
    assert env.world.latest["db"]["error_rate"] < 0.05


def test_bad_config_symptoms():
    env = _env("bad_config_deploy", "gateway", {"error": 0.4})
    assert env.world.latest["gateway"]["error_rate"] > 0.3
    assert env.get_status()["gateway"].config_version == "v2"
    assert env.get_logs("gateway", 900, grep="config reload")


def test_process_crash_symptoms():
    env = _env("process_crash", "api")
    assert env.get_status()["api"].state == "down"
    assert env.world.latest["gateway"]["error_rate"] > 0.8


def test_cache_failure_symptoms():
    env = _env("cache_failure", "cache")
    assert env.world.latest["cache"]["error_rate"] > 0.2
    assert env.world.latest["db"]["cpu"] > 50


def test_c2_beacon_symptoms_hidden_in_normal_traffic():
    env = _env("c2_beacon", "worker", {"interval": 30, "jitter": 0.2})
    flows = env.get_flows(900)
    beacons = [f for f in flows if f.src == "worker" and f.dst == "203.0.113.47"]
    assert 8 <= len(beacons) <= 20
    health = [f for f in flows if f.src == "monitor"]
    assert len(health) > 400  # legitimate periodic traffic for false-positive pressure
    assert env.world.latest["worker"]["error_rate"] < 0.05  # no ops symptom


def test_credential_stuffing_symptoms():
    env = _env("credential_stuffing", "gateway")
    fails = env.get_logs("gateway", 100, grep="authentication failed")
    assert len(fails) > 20
    assert len({line.msg.split(" from ")[1].split(" ")[0] for line in fails}) > 10


def test_dns_failure_symptoms():
    env = _env("dns_failure", "api")
    assert env.world.latest["api"]["error_rate"] > 0.5
    assert env.get_logs("api", 100, grep="Name or service not known")


@pytest.mark.parametrize("ftype", list(FAULT_CLASSES))
def test_every_fault_is_resolved_by_each_correct_sequence(ftype: str):
    target = VALID_TARGETS[ftype][0]
    env = _env(ftype, target)
    fault = env.faults[0]
    for seq in fault.correct_sequences():
        env = _env(ftype, target)
        assert not env.gt_recovered()
        for key in seq:
            name, tgt = key[:-1].split("(")
            env.apply_action(Action(name, tgt))
        assert env.gt_recovered(), f"{ftype}: {seq} should recover"


@pytest.mark.parametrize("ftype", list(FAULT_CLASSES))
def test_faults_persist_without_remediation(ftype: str):
    from agent.detectors.evidence import EvidenceBuilder

    target = VALID_TARGETS[ftype][0]
    env = _env(ftype, target)
    eb = EvidenceBuilder()
    eb.build(env)  # remembers the pre-incident baseline, as the agent does
    env.wait(3000)  # 300 ticks of doing nothing
    assert not env.gt_all_faults_resolved()
    assert not env.gt_recovered()
    # the degradation stays observable to the verifier against the healthy baseline
    assert eb.verify(env, since=env.now() - 200).problems()


@pytest.mark.parametrize("ftype", list(FAULT_CLASSES))
def test_wrong_target_or_wrong_action_never_resolves(ftype: str):
    from env.types import ACTION_ALLOWLIST

    target = VALID_TARGETS[ftype][0]
    correct = {k for seq in _env(ftype, target).faults[0].correct_sequences() for k in seq}
    for action in ACTION_ALLOWLIST:
        for svc in ("gateway", "api", "db", "cache", "worker"):
            if f"{action}({svc})" in correct:
                continue
            env = _env(ftype, target)
            env.apply_action(Action(action, svc))
            assert not env.gt_all_faults_resolved(), f"{ftype} resolved by {action}({svc})"
            assert not env.action_log[-1]["effective"]


def test_blocking_egress_of_a_service_with_external_deps_is_visible():
    env = _env("memory_leak", "worker")
    env.apply_action(Action("block_egress", "api"))
    assert env.world.latest["api"]["error_rate"] > 0.2
    assert env.get_logs("api", 60, grep="egress blocked")
