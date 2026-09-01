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


def test_confidence_above_one_is_clamped_not_accepted_as_written():
    """`2.0` is a malformed confidence, not a very confident one. It must never
    read as clearing the 0.70 approve threshold on its own terms."""
    v = parse_verdict("VERDICT: approve CONFIDENCE: 2.0 TESTS: pass")
    assert v.confidence <= 1.0


def test_the_reasoning_tail_is_still_whole():
    """`reasoning` is what the loop feeds back as revision feedback, so the
    widened match must not eat any of it."""
    v = parse_verdict(
        "**VERDICT:** revise **CONFIDENCE:** 0.5 **TESTS:** fail\n"
        "The empty-input case is unhandled.\n\nFINDINGS:\n- checked the guard"
    )
    assert "The empty-input case is unhandled." in v.reasoning
    assert "checked the guard" in v.findings
