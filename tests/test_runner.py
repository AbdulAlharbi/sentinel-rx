"""Runner: heldout guard, perturbation wrappers, end-to-end runbook run."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import runner
from env.perturb import MissingMetrics, NoisyLogs, RenamedServices, TimestampJitter
from env.sim.sim_env import SimEnv
from env.types import Action
from scenarios.schema import load_split
from tests.conftest import make_scenario


def test_heldout_requires_final(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runner, "HELDOUT_LOG", tmp_path / "log")
    with pytest.raises(SystemExit):
        runner.guard_heldout("heldout", final=False, configs=["runbook"], model="m", tag="")
    with pytest.raises(SystemExit):
        runner.guard_heldout("perturbations/noisy_logs", False, ["runbook"], "m", "")
    runner.guard_heldout("dev", False, ["runbook"], "m", "")  # dev never needs --final
    runner.guard_heldout("heldout", True, ["runbook"], "m", "x")
    assert "split=heldout" in (tmp_path / "log").read_text()


def test_runbook_end_to_end_on_dev(tmp_path: Path):
    paths = runner.run_split("dev", ["runbook"], 1, "none", True, 8, 3, "t", 1, out_dir=tmp_path)
    assert len(paths) == 1
    import json

    d = json.loads(paths[0].read_text())
    assert d["summary"]["n_runs"] == 3 and len(d["scores"]) == 3
    assert (tmp_path / "traces").exists()


def test_renamed_services_round_trip():
    sc = make_scenario([("memory_leak", "api", 50, {"rate": 1.5})])
    sim = SimEnv(sc)
    env = RenamedServices(sim, sc.seed)
    assert "orders-svc" in env.list_services() and "api" not in env.list_services()
    assert env.get_topology()["edge-proxy-01"] == ["orders-svc"]
    r = env.apply_action(Action("restart_service", "orders-svc"))
    assert r.applied and sim.gt_recovered()


def test_missing_metrics_has_holes():
    sc = make_scenario([("memory_leak", "api", 50, {})])
    env = MissingMetrics(SimEnv(sc), sc.seed)
    samples = env.get_metrics(100)
    assert any(v is None for s in samples for v in s.values.values())
    assert any(v is not None for s in samples for v in s.values.values())


def test_noisy_logs_and_jitter_are_deterministic():
    sc = make_scenario([("bad_config_deploy", "api", 50, {})])
    a, b = NoisyLogs(SimEnv(sc), sc.seed), NoisyLogs(SimEnv(sc), sc.seed)
    assert [x.to_dict() for x in a.get_logs(None, 300)] == [
        x.to_dict() for x in b.get_logs(None, 300)
    ]
    assert len(a.get_logs(None, 300)) > len(a.inner.get_logs(None, 300))
    j1, j2 = TimestampJitter(SimEnv(sc), sc.seed), TimestampJitter(SimEnv(sc), sc.seed)
    assert [f.to_dict() for f in j1.get_flows(300)] == [f.to_dict() for f in j2.get_flows(300)]
    assert [f.t for f in j1.get_flows(300)] != [f.t for f in j1.inner.get_flows(300)]


def test_every_committed_scenario_runs_in_sim():
    for sc in load_split("dev")[:5] + load_split("heldout")[:3]:
        env = SimEnv(sc)
        assert env.health_score() < 0.99 or sc.n_faults == 0
