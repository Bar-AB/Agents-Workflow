"""Validator eval harness: the CI path checks the harness *mechanics* on the
scripted MockRunner fixtures (agreement, confusion matrix, calibration table),
not the real validator's calibration — that needs `--runner claude`."""

import dataclasses

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
        assert v.kind in (VerdictKind.APPROVE, VerdictKind.REVISE, VerdictKind.ESCALATE)
        assert 0.0 <= v.confidence <= 1.0


# -- batch whole-loop evaluation (slice 6, Part 3) ----------------------------
#
# The per-verdict harness above measures one validator answer at a time. A batch
# fixture measures one whole task's journey through the *Loop* against a gold
# `TaskStatus`, which is the only way to observe that a revise at 0.55 actually
# produces a revision round rather than an escalation.


def run_batch(store, fixtures=None):
    return evalmod.run_batch_eval(store, Registry.load(), fixtures=fixtures)


def test_batch_fixtures_reach_their_gold_status(store):
    """Every shipped fixture's measured status equals gold.

    A fixture whose script drifts from the decision rules must fail *loudly*
    here rather than quietly lowering `agreement` in a report nobody re-derives.
    """
    result = run_batch(store)
    assert evalmod.BATCH_FIXTURES, "the harness ships fixtures"
    wrong = [d for d in result["detail"] if not d["correct"]]
    assert wrong == []
    assert result["summary"]["agreement"] == 1.0


def test_batch_fixtures_cover_the_named_decision_rules(store):
    """The rules the harness exists to regression-test, by outcome shape."""
    detail = {d["id"]: d for d in run_batch(store)["detail"]}
    ids = set(detail)
    assert {
        "b-approve-first",
        "b-revise-then-approve",
        "b-revisions-exhausted",
        "b-severe-verdict",
        "b-below-severe-threshold",
        "b-worker-escalate",
        "b-empty-output",
        "b-high-risk-signoff",
    } <= ids
    # revise-at-0.55-then-approve is a *revision*, not an escalation.
    assert detail["b-revise-then-approve"]["measured"] == "done"
    assert detail["b-revise-then-approve"]["revision_count"] == 1
    # a severe verdict skips the revision loop entirely.
    assert detail["b-severe-verdict"]["measured"] == "needs_human"
    assert detail["b-severe-verdict"]["revision_count"] == 0
    assert detail["b-severe-verdict"]["has_escalation_reason"] is True
    # an exhausted revision budget lands at NEEDS_HUMAN with revisions spent.
    assert detail["b-revisions-exhausted"]["measured"] == "needs_human"
    assert detail["b-revisions-exhausted"]["revision_count"] >= 1
    # approve-first-try is DONE with no escalation reason and real spend.
    assert detail["b-approve-first"]["measured"] == "done"
    assert detail["b-approve-first"]["has_escalation_reason"] is False
    assert detail["b-approve-first"]["attempts"] == 2
    assert detail["b-approve-first"]["tokens"] > 0


def test_batch_eval_persists_one_row_with_kind_batch(store):
    """Acceptance test #3 — the discriminator doing its job.

    A `kind='batch'` row lands beside a `kind='verdict'` row, `n_fixtures`
    reconciles with the detail, and `agreement` is the fraction of that detail
    which matched gold.
    """
    run(store)  # the per-verdict harness, unchanged
    result = run_batch(store)

    rows = store.eval_runs()
    assert [r["kind"] for r in rows] == ["verdict", "batch"]
    batch = rows[1]
    assert batch["runner"] == "MockRunner"
    assert batch["n_fixtures"] == len(evalmod.BATCH_FIXTURES)
    assert len(batch["detail"]) == batch["n_fixtures"]
    n_correct = sum(1 for d in batch["detail"] if d["correct"])
    assert batch["agreement"] == pytest.approx(n_correct / batch["n_fixtures"])
    assert batch["summary"]["agreement"] == pytest.approx(batch["agreement"])
    assert result["summary"]["agreement"] == pytest.approx(batch["agreement"])
    # audited like every other eval run, and the event says which kind it was
    kinds = [
        e["payload"].get("kind") for e in store.events() if e["kind"] == "eval_run"
    ]
    assert kinds == ["verdict", "batch"]


def test_run_eval_still_writes_kind_verdict(store):
    """The pre-existing per-verdict path is untouched by the discriminator."""
    run(store)
    rows = store.eval_runs()
    assert len(rows) == 1
    assert rows[0]["kind"] == "verdict"


def test_an_old_eval_runs_row_reads_as_kind_verdict(tmp_path):
    """A slice-5 database migrates: its rows *are* verdict runs, so that is what
    they must read back as."""
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE eval_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " runner TEXT NOT NULL, n_fixtures INTEGER NOT NULL,"
        " agreement REAL NOT NULL, summary TEXT NOT NULL DEFAULT '{}',"
        " detail TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL)"
    )
    con.execute(
        "INSERT INTO eval_runs (runner, n_fixtures, agreement, created_at)"
        " VALUES ('MockRunner', 20, 0.8, 1.0)"
    )
    con.commit()
    con.close()

    s = Store(db)
    try:
        rows = s.eval_runs()
        assert len(rows) == 1
        assert rows[0]["kind"] == "verdict"
    finally:
        s.close()


def test_batch_eval_does_not_pollute_the_task_board(store):
    run_batch(store)
    # Every fixture ran through a real Loop over a scratch in-memory store, so
    # no eval task leaks into the result store's task table.
    assert store.list_tasks() == []


def test_batch_eval_confusion_matrix_reconciles(store):
    s = run_batch(store)["summary"]
    conf = s["confusion"]
    total = sum(v for row in conf.values() for v in row.values())
    assert total == s["n"]
    diagonal = sum(conf[g].get(g, 0) for g in conf)
    assert diagonal / s["n"] == pytest.approx(s["agreement"])


def test_batch_eval_handles_an_empty_fixture_list(store):
    result = run_batch(store, fixtures=[])
    assert result["summary"]["n"] == 0
    assert result["summary"]["agreement"] == 0.0
    assert result["detail"] == []
    rows = store.eval_runs()
    assert len(rows) == 1 and rows[0]["kind"] == "batch"


def test_batch_agreement_falls_when_a_fixture_disagrees(store):
    """Sensitivity control: agreement is a measurement, not a constant.

    Without this, `test_batch_fixtures_reach_their_gold_status` would pass
    identically against a `run_batch_eval` that hard-coded 1.0.
    """
    from agentloop.models import TaskStatus

    good = evalmod.BATCH_FIXTURES[0]
    bad = dataclasses.replace(
        good, id="b-planted-wrong", gold=TaskStatus.FAILED, category="control"
    )
    result = run_batch(store, fixtures=[good, bad])
    assert result["summary"]["agreement"] == pytest.approx(0.5)
    planted = [d for d in result["detail"] if d["id"] == "b-planted-wrong"][0]
    assert planted["correct"] is False
    assert planted["measured"] == "done"
    conf = result["summary"]["confusion"]
    assert conf["failed"]["done"] == 1


def test_format_batch_report_names_the_measurement(store):
    text = evalmod.format_batch_report(run_batch(store))
    assert "Agreement with gold" in text
    assert "b-approve-first" in text


# ===========================================================================
# Remediation cycle 1 - the batch harness scored free passes and read the
# loop's in-hand object instead of the row.
# ===========================================================================


def test_a_fixture_that_overruns_its_script_is_not_scored_correct(store):
    """MEDIUM d. `MockRunner` returns "(mock output)" forever once its script
    runs out, an unparseable verdict escalates to NEEDS_HUMAN, and NEEDS_HUMAN
    is the gold for 5 of the 9 shipped fixtures - so a regression in the
    severe-verdict rule, the risk gate or the revision budget could overrun the
    script and still score `correct`. The harness collected `revision_count`
    and `has_escalation_reason` and asserted on neither."""
    from agentloop.models import TaskStatus

    starved = evalmod.BatchFixture(
        id="b-starved",
        category="control",
        title="starved",
        goal="Write slugify(text).",
        criteria="Lowercase, hyphen-separated.",
        risk_level=1,
        script=["worker output"],  # no validator reply: the runner improvises
        gold=TaskStatus.NEEDS_HUMAN,
    )

    detail = run_batch(store, fixtures=[starved])["detail"][0]

    # It *reaches* gold - by accident, off the end of its script.
    assert detail["measured"] == "needs_human"
    assert detail["unscripted_calls"] >= 1
    assert detail["script_consumed"] is False
    assert detail["correct"] is False


def test_every_shipped_fixture_consumes_its_script_exactly(store):
    """Control for the row above, and the property that makes it meaningful:
    a shipped fixture scripts every call the loop makes, so an extra or a
    missing call is a rule change rather than harness noise."""
    by_id = {d["id"]: d for d in run_batch(store)["detail"]}
    for fixture in evalmod.BATCH_FIXTURES:
        detail = by_id[fixture.id]
        assert detail["unscripted_calls"] == 0, fixture.id
        assert detail["script_remaining"] == fixture.unused_script, fixture.id
        assert detail["script_consumed"] is True, fixture.id
    # The one fixture whose unconsumed reply *is* the assertion: the validator
    # must never see an empty worker output.
    assert by_id["b-empty-output"]["script_remaining"] == 1


def test_the_measured_status_comes_from_the_row_not_the_in_hand_task(
    store, monkeypatch
):
    """MEDIUM e. `human_approve` documents this exact pattern as recording "a
    transition the row never took": `set_status` assigns the new status onto
    the in-hand object *before* its lease-predicated write. Latent today, and
    the wrong default for a measurement harness."""
    import agentloop.loop as loop_mod
    from agentloop.models import TaskStatus

    real_loop = loop_mod.Loop

    class LyingLoop(real_loop):
        """A loop whose in-hand object disagrees with the row it wrote - which
        is what a predicated-miss looks like from the harness."""

        def run_task(self, task):
            result = real_loop.run_task(self, task)
            task.status = TaskStatus.DONE
            return result

    monkeypatch.setattr(loop_mod, "Loop", LyingLoop)

    escalating = evalmod.BatchFixture(
        id="b-severe-for-the-row",
        category="control",
        title="severe",
        goal="Write slugify(text).",
        criteria="Lowercase, hyphen-separated.",
        risk_level=1,
        script=["worker output", evalmod._SEVERE],
        gold=TaskStatus.NEEDS_HUMAN,
    )

    detail = run_batch(store, fixtures=[escalating])["detail"][0]

    assert detail["measured"] == "needs_human"
    assert detail["correct"] is True
