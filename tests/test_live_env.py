"""LiveEnv integration test: real processes, real beacon, same detectors (no LLM)."""

from __future__ import annotations

import sys
import time

import pytest

from agent.detectors.evidence import EvidenceBuilder
from env.live.live_env import LiveEnv
from env.types import Action

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix only")


def test_live_env_beacon_is_detected_and_contained(tmp_path):
    env = LiveEnv(workdir=tmp_path / "live")
    try:
        time.sleep(8)
        assert env.health_score() > 0.9
        assert all(s.state == "up" for s in env.get_status().values())
        env.inject("c2_beacon", "worker", {"interval": 1.5, "jitter": 0.2})
        env.inject("db_conn_exhaustion", "db")
        time.sleep(16)
        eb = EvidenceBuilder(window=env.now())
        ev = eb.build(env)
        assert any(b.src == "worker" and b.dst == "203.0.113.47" for b in ev.beacons)
        assert any(m.service == "db" and m.metric == "conn_count" for m in ev.metrics)
        assert env.get_logs("db", 60, grep="too many connections")
        r = env.apply_action(Action("block_egress", "worker"))
        assert r.applied and env.get_status()["worker"].egress_blocked
        time.sleep(4)
        attempts = [f for f in env.get_flows(4) if f.dst == "203.0.113.47"]
        assert attempts and all(f.blocked for f in attempts)
        env.apply_action(Action("restart_service", "worker"))
        env.apply_action(Action("scale_connections", "db"))
        gt = env.gt_summary()
        assert gt["all_faults_resolved"] and gt["restart_before_containment"] == 0
    finally:
        env.close()
    assert not (tmp_path / "live").exists()


def test_live_env_restart_without_containment_reinfects(tmp_path):
    env = LiveEnv(workdir=tmp_path / "live")
    try:
        time.sleep(3)
        env.inject("c2_beacon", "worker", {"interval": 1.0})
        time.sleep(3)
        env.apply_action(Action("restart_service", "worker"))
        assert env.action_log[-1]["harmful"]
        time.sleep(4)
        assert [f for f in env.get_flows(4) if f.dst == "203.0.113.47" and not f.blocked]
    finally:
        env.close()
