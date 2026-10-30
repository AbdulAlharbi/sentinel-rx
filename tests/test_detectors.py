"""Detectors: metric anomalies, log signatures, dependency ranking, beacon detection."""

from __future__ import annotations

import json

import numpy as np
import pytest

from agent.detectors.beacon import MODEL_PATH, BeaconDetector, pair_features
from agent.detectors.evidence import EvidenceBuilder
from agent.detectors.logs import detect_log_signatures, template_of
from agent.detectors.metrics import detect_metric_anomalies
from env.sim.sim_env import SimEnv
from env.types import Action, Flow
from tests.conftest import make_scenario


def _env(faults):
    return SimEnv(make_scenario(faults))


def test_template_extraction_merges_ids():
    a = template_of("GET /orders/12345 200 17ms")
    b = template_of("GET /orders/99 200 180ms")
    assert a == b
    assert template_of("too many connections (100/100)") != a


def test_metric_detector_flags_the_leaking_service_only():
    env = _env([("memory_leak", "api", 45, {"rate": 1.5})])
    flagged = {(a.service, a.metric) for a in detect_metric_anomalies(env.get_metrics(900))}
    assert ("api", "mem") in flagged
    assert not any(s == "cache" for s, _ in flagged)


def test_metric_detector_quiet_when_healthy():
    env = _env([])
    assert detect_metric_anomalies(env.get_metrics(900)) == []


def test_metric_detector_tolerates_missing_values():
    env = _env([("disk_full", "db", 45, {"rate": 5})])
    samples = env.get_metrics(900)
    for s in samples:
        s.values["cpu"] = None
    flagged = {(a.service, a.metric) for a in detect_metric_anomalies(samples)}
    assert ("db", "disk_used") in flagged


def test_log_detector_surfaces_fault_signature():
    env = _env([("db_conn_exhaustion", "db", 45, {})])
    sigs = detect_log_signatures(env.get_logs(None, 900), env.now(), 900)
    assert sigs and sigs[0].service == "db" and "too many connections" in sigs[0].template


def test_ranker_prefers_downstream_cause_over_upstream_symptom():
    env = _env([("latency_injection", "db", 45, {"ms": 500})])
    ev = EvidenceBuilder(use_beacon=False).build(env)
    assert ev.ranked[0].service == "db"
    api = next(r for r in ev.ranked if r.service == "api")
    assert "db" in api.explained_by and api.score < ev.ranked[0].score


def test_ranker_keeps_intrinsic_causes_on_multiple_services():
    env = _env([("memory_leak", "api", 45, {"rate": 1.5}), ("db_conn_exhaustion", "db", 50, {})])
    ev = EvidenceBuilder(use_beacon=False).build(env)
    top2 = {r.service for r in ev.ranked[:2]}
    assert top2 == {"api", "db"}


@pytest.mark.parametrize("jitter", [0.05, 0.2, 0.35])
def test_beacon_detector_separates_beacons_from_health_checks(jitter: float):
    env = _env([("c2_beacon", "worker", 45, {"interval": 35, "jitter": jitter})])
    det = BeaconDetector()
    found = det.detect(env.get_flows(900), env.list_services(), env.external_allowlist())
    pairs = {(b.src, b.dst) for b in found}
    assert ("worker", "203.0.113.47") in pairs
    assert not any(src == "monitor" for src, _ in pairs)  # health checks are periodic but legit
    assert not any(dst == "updates.internal" for _, dst in pairs)  # update polls too
    assert not any(dst == "payments.example" for _, dst in pairs)


def test_beacon_detector_quiet_on_clean_traffic():
    env = _env([("memory_leak", "api", 45, {})])
    det = BeaconDetector()
    assert det.detect(env.get_flows(900), env.list_services(), env.external_allowlist()) == []


def test_beacon_detector_sees_contained_implant_attempts():
    env = _env([("c2_beacon", "worker", 45, {"interval": 30, "jitter": 0.1})])
    env.apply_action(Action("block_egress", "worker"))
    env.wait(300)
    det = BeaconDetector()
    found = det.detect(env.get_flows(400), env.list_services(), env.external_allowlist())
    assert found and found[0].blocked_fraction > 0.5


def test_beacon_features_are_scale_invariant():
    rng = np.random.default_rng(0)
    slow = np.cumsum(60 * (1 + rng.uniform(-0.2, 0.2, 30)))
    fast = slow / 15.0
    nb = np.full(30, 200.0)
    a = pair_features(slow, nb, True, False, False)
    b = pair_features(fast, nb, True, False, False)
    # all features except log_median_iat should coincide
    mask = np.ones_like(a, dtype=bool)
    mask[8] = False
    assert np.allclose(a[mask], b[mask], atol=0.05)


def test_beacon_model_metrics_are_reported():
    m = json.loads(MODEL_PATH.with_name("beacon_metrics.json").read_text())
    assert m["average_precision"] > 0.95 and m["precision"] > 0.9 and m["recall"] > 0.9
    assert m["false_positive_rate_by_kind"]["health"] == 0.0
    assert m["false_positive_rate_by_kind"]["update"] == 0.0


def test_evidence_is_compact_and_quiet_after_recovery():
    env = _env(
        [("memory_leak", "api", 45, {"rate": 1.5}), ("c2_beacon", "worker", 45, {"interval": 30})]
    )
    eb = EvidenceBuilder()
    ev = eb.build(env)
    assert ev.beacons and ev.ranked[0].service in {"api", "worker"}
    assert len(json.dumps(ev.to_dict())) < 6000  # compact: never raw telemetry
    env.apply_action(Action("block_egress", "worker"))
    env.apply_action(Action("restart_service", "worker"))
    env.apply_action(Action("restart_service", "api"))
    t_fix = env.now()
    env.wait(200)
    ev2 = eb.verify(env, since=t_fix)
    assert ev2.is_quiet(), ev2.problems()


def test_verify_is_not_fooled_by_containment_alone():
    env = _env([("c2_beacon", "worker", 45, {"interval": 30, "jitter": 0.1})])
    eb = EvidenceBuilder()
    eb.build(env)
    env.apply_action(Action("block_egress", "worker"))
    t_fix = env.now()
    env.wait(200)
    ev = eb.verify(env, since=t_fix)
    assert not ev.is_quiet() and any("implant" in p or "beacon" in p for p in ev.problems())


def test_verify_fails_while_fault_persists():
    env = _env([("bad_config_deploy", "api", 45, {"error": 0.5})])
    eb = EvidenceBuilder(use_beacon=False)
    eb.build(env)
    env.apply_action(Action("restart_service", "api"))
    t_fix = env.now()
    env.wait(100)
    assert not eb.verify(env, since=t_fix).is_quiet()


def test_flow_dataclass_blocked_default():
    assert Flow(1.0, "a", "b", 10).blocked is False
