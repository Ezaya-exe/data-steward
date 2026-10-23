"""Catalogue acceptance tests (`ARCHITECTURE.md` §14, Phase 8a).

The catalogue is the benchmark's answer key: a wrong expected check would make
a perfect agent look wrong (or worse, validate a wrong diagnosis). These tests
assert the documented structure — counts, split integrity, variant magnitudes,
complete ground truth — without running an agent.
"""

from __future__ import annotations

import pytest

from dqs.bench import scenarios
from dqs.bench.harness import HARNESS_VERSION, harness_metadata, prompt_hashes
from dqs.models.enums import FaultKind
from dqs.sim.faults import FK_MAP, parse_fault_spec


def _faults() -> list[scenarios.Scenario]:
    return [scenario for scenario in scenarios.SCENARIOS if not scenario.is_control]


def test_catalogue_structure_is_valid() -> None:
    assert scenarios.catalogue_problems() == []


def test_catalogue_size_and_split_ratio() -> None:
    faults = _faults()
    controls = [scenario for scenario in scenarios.SCENARIOS if scenario.is_control]
    assert len(faults) >= 40
    assert len(controls) >= 6
    holdout = [scenario for scenario in faults if scenario.split is scenarios.Split.HOLDOUT]
    assert 0.2 <= len(holdout) / len(faults) <= 0.4
    assert all(scenario.split is scenarios.Split.DEV for scenario in controls)


def test_ids_are_unique_stable_and_filterable() -> None:
    ids = [scenario.id for scenario in scenarios.SCENARIOS]
    assert len(ids) == len(set(ids))
    assert scenarios.SCENARIO_IDS == tuple(ids)
    assert all(identifier == identifier.lower() for identifier in ids)
    assert [scenario.id for scenario in scenarios.scenarios_for("dev")] == [
        scenario.id for scenario in scenarios.SCENARIOS if scenario.split is scenarios.Split.DEV
    ]
    assert len(scenarios.scenarios_for("all")) == len(scenarios.SCENARIOS)
    with pytest.raises(ValueError):
        scenarios.scenarios_for("nope")
    assert scenarios.get_scenario(ids[0]).id == ids[0]
    with pytest.raises(KeyError):
        scenarios.get_scenario("does-not-exist")


def test_every_fault_kind_is_covered() -> None:
    kinds = {scenario.expected.kind for scenario in _faults()}
    assert kinds == set(FaultKind)


def test_holdout_contains_all_scale_shift_and_missing_table_scenarios() -> None:
    faults = _faults()
    for kind in (FaultKind.SCALE_SHIFT, FaultKind.MISSING_TABLE):
        matching = [scenario for scenario in faults if scenario.expected.kind is kind]
        assert matching
        assert all(scenario.split is scenarios.Split.HOLDOUT for scenario in matching)
    holdout_kinds = {
        scenario.expected.kind for scenario in faults if scenario.split is scenarios.Split.HOLDOUT
    }
    assert holdout_kinds == set(FaultKind)


def test_documented_variants_are_present() -> None:
    nulls = {
        scenario.expected.parameters.get("fraction")
        for scenario in _faults()
        if scenario.expected.kind is FaultKind.NULL_SPIKE
    }
    duplicates = {
        scenario.expected.parameters.get("fraction")
        for scenario in _faults()
        if scenario.expected.kind is FaultKind.DUPLICATE_ROWS
    }
    scales = {
        scenario.expected.parameters.get("factor")
        for scenario in _faults()
        if scenario.expected.kind is FaultKind.SCALE_SHIFT
    }
    assert {0.2, 0.35, 0.6} <= nulls
    assert {0.03, 0.08, 0.15} <= duplicates
    assert {100, 1000} <= scales


def test_orphan_scenarios_only_use_documented_foreign_keys() -> None:
    for scenario in _faults():
        if scenario.expected.kind is not FaultKind.ORPHAN_KEYS:
            continue
        spec = parse_fault_spec(scenario.fault_spec or "")
        assert f"{spec.table}.{spec.column}" in FK_MAP


def test_ground_truth_is_complete_for_every_fault() -> None:
    for scenario in _faults():
        expected = scenario.expected
        assert expected is not None
        assert expected.table == scenario.table
        assert expected.expected_checks, scenario.id
        assert expected.expected_category is not None
        assert expected.expected_resolution is not None
        if expected.kind in {
            FaultKind.DUPLICATE_ROWS,
            FaultKind.LATE_BATCH,
            FaultKind.MISSING_TABLE,
        }:
            assert expected.acceptable_columns == []
        else:
            assert expected.acceptable_columns, scenario.id


def test_controls_have_no_fault_spec_or_ground_truth() -> None:
    for scenario in scenarios.SCENARIOS:
        if not scenario.is_control:
            continue
        assert scenario.fault_spec is None
        assert scenario.expected is None
        assert scenario.table is None
        assert scenario.kind == "clean"


def test_harness_metadata_is_complete_and_deterministic() -> None:
    hashes = prompt_hashes()
    assert set(hashes) == {"investigator", "fixer"}
    assert all(len(value) == 64 for value in hashes.values())
    assert hashes == prompt_hashes()
    metadata = harness_metadata()
    assert metadata["harness_version"] == HARNESS_VERSION
    assert metadata["prompt_hashes"] == hashes
