"""Scorer on hand-made traces with known answers."""

from __future__ import annotations

from bench.aggregate import aggregate, bootstrap_ci
from bench.score import score_run
from tests.conftest import make_scenario


def _gt(actions, faults, recovered):
    return {"actions": actions, "faults": faults, "recovered": recovered}


def _act(name, target, harmful=None, effective=True, undo=False):
    rec = {
        "action": {"name": name, "target": target, "params": {}},
        "harmful": harmful,
        "effective": effective,
    }
    if undo:
        rec["undo"] = True
    return rec


SC = make_scenario(
    [("memory_leak", "api", 50, {}), ("c2_beacon", "worker", 45, {"interval": 30})], sid="hand"
)
FAULTS = [
    {"type": "memory_leak", "target": "api", "resolved_at": 95},
    {"type": "c2_beacon", "target": "worker", "resolved_at": 99, "reinfections": 0},
]


def test_perfect_run_scores_perfectly():
    trace = {
        "final_hypotheses": [
            {"service": "worker", "type": "c2_beacon", "confidence": 0.9, "status": "confirmed"},
            {"service": "api", "type": "memory_leak", "confidence": 0.8, "status": "confirmed"},
        ],
        "claimed_recovered": True,
        "steps": [{"phase": "iteration"}],
    }
    gt = _gt(
        [
            _act("block_egress", "worker"),
            _act("restart_service", "worker"),
            _act("restart_service", "api"),
        ],
        FAULTS,
        True,
    )
    s = score_run(trace, SC, gt)
    assert s["top1"] == 1 and s["top3"] == 1 and s["fault_recall"] == 1.0
    assert s["recovered"] == 1 and s["false_recovery"] == 0
    assert s["harmful_actions"] == 0 and s["unnecessary_actions"] == 0
    assert s["isolated_before_restart"] == 1.0


def test_false_recovery_and_harmful_actions_are_counted():
    trace = {
        "final_hypotheses": [
            {"service": "db", "type": "db_conn_exhaustion", "confidence": 0.9, "status": "likely"}
        ],
        "claimed_recovered": True,
        "steps": [],
    }
    faults = [dict(FAULTS[0]), dict(FAULTS[1], reinfections=1, resolved_at=None)]
    gt = _gt(
        [
            _act(
                "restart_service", "worker", harmful="restarted before containment", effective=False
            ),
            _act("flush_cache", "cache", effective=False),
        ],
        faults,
        False,
    )
    s = score_run(trace, SC, gt)
    assert s["top1"] == 0 and s["fault_recall"] == 0.0
    assert s["false_recovery"] == 1 and s["recovered"] == 0
    assert s["harmful_actions"] == 1
    assert s["unnecessary_actions"] == 2  # neither action resolved anything
    assert s["isolated_before_restart"] == 0.0


def test_isolating_an_innocent_service_is_harmful():
    trace = {"final_hypotheses": [], "claimed_recovered": False, "steps": []}
    gt = _gt([_act("isolate_host", "api")], FAULTS, False)
    assert score_run(trace, SC, gt)["harmful_actions"] == 1


def test_top3_but_not_top1():
    trace = {
        "final_hypotheses": [
            {"service": "db", "type": "disk_full", "confidence": 0.9, "status": "likely"},
            {"service": "api", "type": "memory_leak", "confidence": 0.5, "status": "likely"},
        ],
        "claimed_recovered": False,
        "steps": [],
    }
    s = score_run(trace, SC, _gt([], FAULTS, False))
    assert s["top1"] == 0 and s["top3"] == 1 and s["fault_recall"] == 0.5


def test_renamed_services_are_mapped_for_scoring():
    trace = {
        "final_hypotheses": [
            {"service": "orders-svc", "type": "memory_leak", "confidence": 0.9, "status": "likely"}
        ],
        "claimed_recovered": False,
        "steps": [],
    }
    gt = _gt([_act("restart_service", "api")], FAULTS, False)
    s = score_run(trace, SC, gt, name_map={"api": "orders-svc", "worker": "batch-runner"})
    assert s["top1"] == 1 and s["unnecessary_actions"] == 0


def test_bootstrap_ci_brackets_mean_and_is_deterministic():
    vals = [0, 1, 1, 1, 0, 1, 0, 1, 1, 1]
    m, lo, hi = bootstrap_ci(vals)
    assert lo <= m <= hi and abs(m - 0.7) < 1e-9
    assert bootstrap_ci(vals) == bootstrap_ci(vals)
    assert bootstrap_ci([])[0] != bootstrap_ci([])[0]  # nan


def test_aggregate_breaks_down_by_n_faults():
    scores = [
        {
            "n_faults": 1,
            "top1": 1,
            "fault_recall": 1,
            "recovered": 1,
            "false_recovery": 0,
            "has_security": False,
        },
        {
            "n_faults": 2,
            "top1": 0,
            "fault_recall": 0.5,
            "recovered": 0,
            "false_recovery": 1,
            "has_security": True,
            "isolated_before_restart": 0.0,
            "harmful_actions": 1,
        },
    ]
    agg = aggregate(scores)
    assert agg["n_runs"] == 2
    assert set(agg["by_n_faults"]) == {"1", "2"}
    assert agg["security"]["n_runs"] == 1


def test_action_precision_and_rolled_back():
    trace = {"final_hypotheses": [], "claimed_recovered": False, "steps": []}
    gt = _gt(
        [
            _act("restart_service", "db", effective=False),
            _act("block_egress", "worker"),
            _act("restart_service", "worker"),
            _act("rollback_config", "api", effective=False),
            _act("block_egress", "worker", undo=True),
        ],
        FAULTS,
        False,
    )
    s = score_run(trace, SC, gt)
    assert s["n_actions"] == 4 and s["effective_actions"] == 2
    assert abs(s["action_precision"] - 0.5) < 1e-9
    assert s["unnecessary_actions"] == 2 and s["rolled_back_actions"] == 1
    assert score_run(trace, SC, _gt([], FAULTS, False))["action_precision"] is None


def test_containment_of_uncompromised_host_is_harmful():
    trace = {"final_hypotheses": [], "claimed_recovered": False, "steps": []}
    gt = _gt(
        [_act("block_egress", "api", effective=False), _act("block_egress", "worker")],
        FAULTS,
        False,
    )
    s = score_run(trace, SC, gt)
    assert s["harmful_actions"] == 1  # api is not the beaconing host; worker is


def test_effective_is_derived_for_legacy_traces():
    from bench.score import action_effective

    assert action_effective({"notes": ["pool scaled"], "harmful": None})
    assert not action_effective({"notes": [], "harmful": None})
    assert not action_effective(
        {"notes": ["restart re-applied config v2; errors persist"], "harmful": None}
    )
    assert not action_effective({"notes": ["implant re-established"], "harmful": "x"})
    assert not action_effective({"notes": ["x"], "harmful": None, "effective": False})
