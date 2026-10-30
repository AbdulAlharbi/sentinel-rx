"""Scenario generation and split discipline."""

from __future__ import annotations

from pathlib import Path

from faults.registry import KNOWN_FAULT_TYPES
from scenarios.generate import generate
from scenarios.schema import Scenario, load_split


def test_generation_is_deterministic(tmp_path: Path):
    a = generate(tmp_path / "a")
    b = generate(tmp_path / "b")
    assert [s.to_dict() for s in a["dev"]] == [s.to_dict() for s in b["dev"]]
    assert [s.to_dict() for s in a["heldout"]] == [s.to_dict() for s in b["heldout"]]


def test_committed_scenarios_match_generator(tmp_path: Path):
    fresh = generate(tmp_path)
    for split in ("dev", "heldout"):
        committed = load_split(split)
        assert [s.to_dict() for s in committed] == [s.to_dict() for s in fresh[split]]


def test_heldout_is_disjoint_from_dev():
    dev = load_split("dev")
    heldout = load_split("heldout")
    dev_combos = {s.combo_key for s in dev}
    dev_seeds = {s.seed for s in dev}
    dev_type_sets = {s.fault_types for s in dev if s.n_faults >= 2}
    for s in heldout:
        assert s.combo_key not in dev_combos, s.id
        assert s.seed not in dev_seeds, s.id
        if s.n_faults >= 2:
            assert s.fault_types not in dev_type_sets, s.id


def test_unseen_fault_only_in_unseen_split():
    for split in ("dev", "heldout", "perturbations/noisy_logs"):
        for s in load_split(split):
            assert all(f.type in KNOWN_FAULT_TYPES for f in s.faults)
    unseen = load_split("perturbations/unseen_fault")
    assert all(any(f.type == "dns_failure" for f in s.faults) for s in unseen)


def test_split_sizes_and_concurrency_mix():
    dev, heldout = load_split("dev"), load_split("heldout")
    assert len(dev) == 30 and len(heldout) == 20
    assert {s.n_faults for s in dev} == {1, 2, 3} == {s.n_faults for s in heldout}


def test_yaml_roundtrip(tmp_path: Path):
    s = load_split("dev")[0]
    s.save(tmp_path / "x.yaml")
    assert Scenario.load(tmp_path / "x.yaml").to_dict() == s.to_dict()
