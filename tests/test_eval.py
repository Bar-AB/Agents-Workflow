"""Validator eval harness: the CI path checks the harness *mechanics* on the
scripted MockRunner fixtures (agreement, confusion matrix, calibration table),
not the real validator's calibration — that needs `--runner claude`."""

import pytest

from agentloop import eval as evalmod
from agentloop.registry import Registry
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def run(store):
    runner = evalmod.mock_runner_for(evalmod.FIXTURES)
    return evalmod.run_eval(store, runner, Registry.load())


def test_agreement_matches_hand_count(store):
    # 4 of the 20 scripted verdicts deliberately disagree with gold
    # (g-dedup, sw-mutates, sw-rounding, am-criteria) -> 16/20 = 0.80.
    result = run(store)
    assert result["summary"]["n"] == 20
    assert result["summary"]["agreement"] == pytest.approx(0.80)


def test_confusion_matrix_cells(store):
    conf = run(store)["summary"]["confusion"]
    # gold approve: 7 approve, 1 revise (g-dedup)
    assert conf["approve"] == {"approve": 7, "revise": 1, "escalate": 0}
    # gold revise: 5 revise, 2 approve (sw-mutates, sw-rounding)
    assert conf["revise"] == {"approve": 2, "revise": 5, "escalate": 0}
    # gold escalate: 4 escalate, 1 revise (am-criteria)
    assert conf["escalate"] == {"approve": 0, "revise": 1, "escalate": 4}
    total = sum(v for row in conf.values() for v in row.values())
    assert total == 20


def test_calibration_buckets(store):
    cal = {c["bucket"]: c for c in run(store)["summary"]["calibration"]}
    # Boundaries straddle 0.40 and 0.70 (the live thresholds).
    assert cal["[0.00,0.40)"]["n"] == 4 and cal["[0.00,0.40)"]["accuracy"] == 1.0
    assert cal["[0.40,0.70)"]["n"] == 7
    assert cal["[0.40,0.70)"]["accuracy"] == pytest.approx(5 / 7, abs=1e-3)
    # The telling cell: a *higher* confidence band that is *less* accurate than
    # the band below it — exactly the miscalibration the harness must surface.
    assert cal["[0.70,0.85)"]["n"] == 5
    assert cal["[0.70,0.85)"]["accuracy"] == pytest.approx(0.60)
    assert cal["[0.85,1.01)"]["n"] == 4 and cal["[0.85,1.01)"]["accuracy"] == 1.0
    # Every fixture landed in exactly one bucket.
    assert sum(c["n"] for c in run(store)["summary"]["calibration"]) == 20


def test_result_persisted_to_store(store):
    run(store)
    runs = store.eval_runs()
    assert len(runs) == 1
    row = runs[0]
    assert row["runner"] == "MockRunner"
    assert row["n_fixtures"] == 20
    assert row["agreement"] == pytest.approx(0.80)
    assert len(row["detail"]) == 20
    # eval runs are audited like everything else
    assert any(e["kind"] == "eval_run" for e in store.events())


def test_eval_does_not_pollute_the_task_board(store):
    run(store)
    # Validator invocations ran against a scratch store, so no eval fixtures
    # leak into the real task table (and the loop can't pick them up).
    assert store.list_tasks() == []


def test_fixture_lines_are_all_parseable(store):
    # A malformed scripted line would silently become an escalate@0 verdict.
    from agentloop.agents import parse_verdict
    from agentloop.models import VerdictKind
    for fx in evalmod.FIXTURES:
        v = parse_verdict(fx.mock_line)
        assert v.kind in (VerdictKind.APPROVE, VerdictKind.REVISE,
                          VerdictKind.ESCALATE)
        assert 0.0 <= v.confidence <= 1.0
