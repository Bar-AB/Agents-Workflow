"""Slice 8: the decision-critical parser must survive ordinary LLM formatting.

An unparseable verdict escalates at confidence 0, which is below
`severe_threshold`, so it goes **straight** to NEEDS_HUMAN with no revision
round and a `reasoning` of "Unparseable validator output" — the record hiding
the fact that the validator actually approved. Measured against the shipped
regex, five of six realistic shapes did exactly that:

    approve   <- VERDICT: approve CONFIDENCE: 0.95 TESTS: pass
    escalate  <- **VERDICT:** approve **CONFIDENCE:** 0.95 **TESTS:** pass
    escalate  <- VERDICT: approve, CONFIDENCE: 0.95, TESTS: pass
    escalate  <- VERDICT: approve CONFIDENCE: .95 TESTS: pass
    escalate  <- VERDICT: approve CONFIDENCE: 0.95 TESTS: n/a
    escalate  <- VERDICT: approve CONFIDENCE: 95% TESTS: pass

The project already conceded this argument one module over:
`toolpolicy._MARKER_RE` tolerates a markdown prefix, four reason separators,
CRLF and every Unicode line terminator, on the stated grounds that LLM output
is markdown. The parser that drives an automatic state transition got none of
that hardening.

**What is widened is decoration and separators only, never semantics.** A reply
with no verdict in it still escalates at confidence 0; nothing here guesses a
verdict from prose. The two directions are not symmetric — over-reading
decoration costs nothing, while under-reading discards a real decision and
spends a human's attention on it.
"""

import pytest

from agentloop.agents import parse_verdict
from agentloop.models import VerdictKind

# (reply, kind, confidence, tests_passed)
REAL_WORLD_SHAPES = [
    ("VERDICT: approve CONFIDENCE: 0.95 TESTS: pass", VerdictKind.APPROVE, 0.95, True),
    (
        "**VERDICT:** approve **CONFIDENCE:** 0.95 **TESTS:** pass",
        VerdictKind.APPROVE,
        0.95,
        True,
    ),
    (
        "**VERDICT: approve CONFIDENCE: 0.95 TESTS: pass**",
        VerdictKind.APPROVE,
        0.95,
        True,
    ),
    (
        "VERDICT: approve, CONFIDENCE: 0.95, TESTS: pass",
        VerdictKind.APPROVE,
        0.95,
        True,
    ),
    (
        "VERDICT: approve | CONFIDENCE: 0.95 | TESTS: pass",
        VerdictKind.APPROVE,
        0.95,
        True,
    ),
    (
        "VERDICT: revise - CONFIDENCE: 0.55 - TESTS: fail",
        VerdictKind.REVISE,
        0.55,
        False,
    ),
    ("VERDICT: approve CONFIDENCE: .95 TESTS: pass", VerdictKind.APPROVE, 0.95, True),
    ("VERDICT: approve CONFIDENCE: 0.95 TESTS: n/a", VerdictKind.APPROVE, 0.95, None),
    ("VERDICT: approve CONFIDENCE: 0.95 TESTS: N/A", VerdictKind.APPROVE, 0.95, None),
    ("VERDICT: approve CONFIDENCE: 95% TESTS: pass", VerdictKind.APPROVE, 0.95, True),
    ("VERDICT: approve CONFIDENCE: 1 TESTS: pass", VerdictKind.APPROVE, 1.0, True),
    (
        "- VERDICT: escalate\n- CONFIDENCE: 0.2\n- TESTS: na",
        VerdictKind.ESCALATE,
        0.2,
        None,
    ),
    (
        "`VERDICT: approve` `CONFIDENCE: 0.8` `TESTS: pass`",
        VerdictKind.APPROVE,
        0.8,
        True,
    ),
    (
        "## Verdict\nVERDICT: approve\r\nCONFIDENCE: 0.71\r\nTESTS: pass",
        VerdictKind.APPROVE,
        0.71,
        True,
    ),
]


@pytest.mark.parametrize("reply, kind, confidence, tests", REAL_WORLD_SHAPES)
def test_ordinary_llm_formatting_still_carries_the_verdict(
    reply, kind, confidence, tests
):
    v = parse_verdict(reply)
    assert v.kind is kind
    assert v.confidence == pytest.approx(confidence)
    assert v.tests_passed is tests


# ---------------------------------------------------------------------------
# The other half, and the more important one: widening decoration must not
# widen *meaning*. These are the controls — a parser that matched everything
# would sail through the block above and be far worse than the strict one.
# ---------------------------------------------------------------------------

NOT_A_VERDICT = [
    "",
    "The work looks fine to me, ship it.",
    "I would approve this with high confidence and the tests pass.",
    "VERDICT: approve",  # no confidence, no tests
    "VERDICT: approve CONFIDENCE: 0.95",  # no tests
    "CONFIDENCE: 0.95 TESTS: pass",  # no verdict
    "VERDICT: maybe CONFIDENCE: 0.95 TESTS: pass",  # not a verdict kind
    "VERDICT: approve CONFIDENCE: high TESTS: pass",  # not a number
    "VERDICT: approve CONFIDENCE: 0.95 TESTS: probably",  # not a tests value
]


@pytest.mark.parametrize("reply", NOT_A_VERDICT)
def test_a_reply_without_a_real_verdict_still_escalates_at_zero(reply):
    v = parse_verdict(reply)
    assert v.kind is VerdictKind.ESCALATE
    assert v.confidence == 0.0
    assert v.reasoning.startswith("Unparseable validator output")


@pytest.mark.parametrize(
    "reply",
    [
        "VERDICT: approve CONFIDENCE: 95 TESTS: pass",  # a percentage, sign dropped
        "VERDICT: approve CONFIDENCE: 2.0 TESTS: pass",
        "VERDICT: approve CONFIDENCE: 8 TESTS: pass",
        "VERDICT: revise CONFIDENCE: 55 TESTS: pass",
        "VERDICT: approve CONFIDENCE: 150% TESTS: pass",
    ],
)
def test_an_out_of_range_confidence_escalates_rather_than_reading_as_certainty(reply):
    """The regression this file exists to prevent, and the direction that costs
    real money.

    An earlier version of the widened parser *clamped* instead of rejecting, and
    a clamp substitutes the most permissive legal value: every out-of-range
    number became `1.0`, the top of the scale, which unconditionally clears both
    `approve_threshold` (0.70) and `severe_threshold` (0.40). Measured:
    `CONFIDENCE: 95` parsed as APPROVE at 1.0, so the task went DONE with no
    human and released its dependents. Widening the pattern had turned a
    fail-safe non-match into a fail-open maximum on the one gate CLAUDE.md rules
    "never guess-approve".

    The assertion is deliberately against the **decision thresholds**, not
    against the clamp bound. The test that missed this asserted
    `v.confidence <= 1.0`, which cannot fail for a clamped value — a hollow
    assertion is worse than none, because it reads as coverage."""
    v = parse_verdict(reply)
    assert v.kind is VerdictKind.ESCALATE
    assert v.confidence == 0.0
    assert v.confidence < 0.40  # below severe: no revision loop, straight to human
    assert "Unparseable" in v.reasoning


def test_a_confidence_at_the_edges_of_the_range_is_still_accepted():
    """The control. Rejecting out-of-range must not reject the boundary values
    themselves, or the fix would be a different bug."""
    for reply, expected in (
        ("VERDICT: approve CONFIDENCE: 1 TESTS: pass", 1.0),
        ("VERDICT: approve CONFIDENCE: 1.0 TESTS: pass", 1.0),
        ("VERDICT: escalate CONFIDENCE: 0 TESTS: na", 0.0),
        ("VERDICT: approve CONFIDENCE: 100% TESTS: pass", 1.0),
        ("VERDICT: approve CONFIDENCE: 95% TESTS: pass", 0.95),
    ):
        v = parse_verdict(reply)
        assert v.confidence == pytest.approx(expected), reply
        assert v.kind is not VerdictKind.ESCALATE or "escalate" in reply


def test_the_reasoning_tail_is_still_whole():
    """`reasoning` is what the loop feeds back as revision feedback, so the
    widened match must not eat any of it."""
    v = parse_verdict(
        "**VERDICT:** revise **CONFIDENCE:** 0.5 **TESTS:** fail\n"
        "The empty-input case is unhandled.\n\nFINDINGS:\n- checked the guard"
    )
    assert "The empty-input case is unhandled." in v.reasoning
    assert "checked the guard" in v.findings


@pytest.mark.parametrize(
    "reply, tests",
    [
        ("VERDICT: approve CONFIDENCE: 0.85 TESTS: passed", True),
        ("VERDICT: revise CONFIDENCE: 0.5 TESTS: failed", False),
        ("VERDICT: revise CONFIDENCE: 0.5 TESTS: failing", False),
    ],
)
def test_the_word_forms_of_the_tests_values_are_accepted(reply, tests):
    """`passed`/`failed` is at least as ordinary an LLM rendering as the shapes
    this parser was widened for. The `\b` added to stop `TESTS: nap` reading as
    `na` had silently narrowed them out — a slice whose purpose is surviving
    ordinary formatting must not lose a form on the way."""
    v = parse_verdict(reply)
    assert v.kind is not VerdictKind.ESCALATE
    assert v.tests_passed is tests


def test_the_nap_protection_the_word_forms_had_to_preserve():
    """The control for the test above: widening to `passed` must not reopen the
    prefix match `\b` was added to close."""
    assert (
        parse_verdict("VERDICT: approve CONFIDENCE: 0.85 TESTS: nap").kind
        is VerdictKind.ESCALATE
    )
