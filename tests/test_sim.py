"""Simulator determinism, propagation and action semantics."""

from __future__ import annotations

import time

from env.sim.sim_env import SimEnv
from env.types import Action
from tests.conftest import make_scenario


def _latest(env: SimEnv, svc: str) -> dict[str, float]:
    return env.world.latest[svc]


def test_sim_is_deterministic():
    sc = make_scenario(
        [("memory_leak", "api", 50, {"rate": 1.5}), ("c2_beacon", "worker", 45, {"interval": 30})]
    )
    a, b = SimEnv(sc), SimEnv(sc)
    assert [m.to_dict() for m in a.get_metrics(900)] == [m.to_dict() for m in b.get_metrics(900)]
    assert [line.to_dict() for line in a.get_logs(None, 900)] == [
        line.to_dict() for line in b.get_logs(None, 900)
    ]
    assert [f.to_dict() for f in a.get_flows(900)] == [f.to_dict() for f in b.get_flows(900)]
    a.apply_action(Action("block_egress", "worker"))
    b.apply_action(Action("block_egress", "worker"))
    assert a.snapshot() == b.snapshot()


def test_seed_changes_telemetry():
    sc1 = make_scenario([("memory_leak", "api", 50, {})], seed=1)
    sc2 = make_scenario([("memory_leak", "api", 50, {})], seed=2)
    assert SimEnv(sc1).get_metrics(50) != SimEnv(sc2).get_metrics(50)


def test_scenario_builds_fast():
    sc = make_scenario(
        [
            ("memory_leak", "api", 50, {}),
            ("db_conn_exhaustion", "db", 55, {}),
            ("c2_beacon", "worker", 45, {}),
        ]
    )
    t0 = time.perf_counter()
    env = SimEnv(sc)
    env.get_logs(None, 900)
    env.get_flows(900)
    assert time.perf_counter() - t0 < 1.0


def test_healthy_baseline_without_faults():
    env = SimEnv(make_scenario([]))
    assert env.health_score() > 0.95
    assert all(s.state == "up" for s in env.get_status().values())
    assert env.gt_recovered()


def test_faults_propagate_upstream(three_fault_env: SimEnv):
    env = three_fault_env
    assert _latest(env, "db")["error_rate"] > 0.4
    assert _latest(env, "api")["error_rate"] > 0.3  # api depends on db (+ its own leak)
    assert _latest(env, "gateway")["error_rate"] > 0.2  # gateway depends on api
    assert _latest(env, "cache")["error_rate"] < 0.05  # cache is unaffected


def test_actions_are_allowlisted(three_fault_env: SimEnv):
    r = three_fault_env.apply_action(Action("rm_rf", "db"))
    assert not r.applied
    r = three_fault_env.apply_action(Action("restart_service", "mainframe"))
    assert not r.applied


def test_wrong_fix_does_not_recover():
    env = SimEnv(make_scenario([("bad_config_deploy", "api", 50, {"error": 0.5})]))
    env.apply_action(Action("restart_service", "api"))
    assert not env.gt_all_faults_resolved()
    assert _latest(env, "api")["error_rate"] > 0.3
    env.apply_action(Action("rollback_config", "api"))
    assert env.gt_recovered()


def test_restart_before_containment_is_harmful_and_reinfects():
    env = SimEnv(make_scenario([("c2_beacon", "worker", 45, {"interval": 30, "jitter": 0.1})]))
    env.apply_action(Action("restart_service", "worker"))
    assert env.action_log[-1]["harmful"] is not None
    assert not env.gt_all_faults_resolved()
    env.wait(200)
    beacons = [f for f in env.get_flows(200) if f.dst.startswith("203.")]
    assert beacons, "implant should re-establish after a naive restart"
    env.apply_action(Action("block_egress", "worker"))
    env.wait(200)
    attempts = [f for f in env.get_flows(200) if f.dst.startswith("203.")]
    assert attempts and all(f.blocked for f in attempts)  # contained but still observable
    env.apply_action(Action("restart_service", "worker"))
    assert env.action_log[-1]["harmful"] is None
    assert env.gt_recovered()


def test_isolate_then_restart_rejoins():
    env = SimEnv(make_scenario([("c2_beacon", "worker", 45, {})]))
    env.apply_action(Action("isolate_host", "worker"))
    assert env.get_status()["worker"].state == "isolated"
    env.apply_action(Action("restart_service", "worker"))
    assert env.get_status()["worker"].state == "up"
    assert env.gt_recovered()


def test_full_recovery_of_three_faults(three_fault_env: SimEnv):
    env = three_fault_env
    for a in [
        ("block_egress", "worker"),
        ("restart_service", "worker"),
        ("restart_service", "api"),
        ("scale_connections", "db"),
    ]:
        env.apply_action(Action(*a))
    assert env.gt_recovered()
    assert env.health_score() > 0.95


def test_restart_after_containment_rebuilds_and_lifts_the_block():
    env = SimEnv(make_scenario([("c2_beacon", "api", 45, {"interval": 30})]))
    env.apply_action(Action("block_egress", "api"))
    assert env.get_status()["api"].egress_blocked
    assert env.world.latest["api"]["error_rate"] > 0.2  # containment of api is not free
    env.apply_action(Action("restart_service", "api"))
    assert not env.get_status()["api"].egress_blocked
    assert env.gt_recovered() and env.health_score() > 0.95
