"""Relevance retrieval behind the memory seam (roadmap slice 2).

The point of these tests is that ranking changes *which* approved facts survive
the injection cap, without changing any of the guarantees around them: the
approval gate, the pinned ceiling, and the caps themselves all still hold.
"""

import subprocess
import sys

import pytest

from agentloop.config import LoopConfig
from agentloop.memory import MemoryService, _MAX_FACTS_IN_PROMPT
from agentloop.retrieval import (
    HashingBackend,
    cosine,
    embed,
    get_backend,
)
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "rag.db")
    yield s
    s.close()


@pytest.fixture()
def memory(store):
    return MemoryService(store, promote_threshold=3, backend=HashingBackend())


# -- the embedding function ---------------------------------------------------


def test_embedding_is_deterministic_across_processes():
    """Guards against a regression to builtin hash(), which is salted per
    process: an index built in one process would silently fail to match a query
    embedded in another."""
    here = embed("pytest is the test command")
    code = (
        "from agentloop.retrieval import embed;"
        "print(embed('pytest is the test command'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert eval(out.stdout.strip()) == pytest.approx(here)


def test_embedding_is_normalized_and_empty_text_is_the_zero_vector():
    assert cosine(embed("alpha beta"), embed("alpha beta")) == pytest.approx(1.0)
    assert cosine(embed(""), embed("alpha")) == pytest.approx(0.0)


def test_shared_vocabulary_scores_above_unrelated_text():
    query = "how do I run the test suite"
    related = embed("test suite command: pytest -q")
    unrelated = embed("the deployment bucket is named prod-assets")
    assert cosine(embed(query), related) > cosine(embed(query), unrelated)


# -- ranking changes what survives the cap ------------------------------------


def test_relevant_fact_survives_the_cap_it_would_have_dropped_out_of(store, memory):
    """Without ranking, selection past the cap is alphabetical accident. The
    one fact that answers the query sorts last, so it used to be dropped."""
    for i in range(_MAX_FACTS_IN_PROMPT + 5):
        store.memory_write("project", f"aaa_{i:03d}", f"filler {i}", approved=True)
    store.memory_write(
        "project", "zzz_deploy", "deploy with the rollout script", approved=True
    )

    unranked, _ = memory.facts_for_prompt()
    ranked, _ = memory.facts_for_prompt(query="how do I deploy the rollout")

    assert "zzz_deploy" not in unranked
    assert "zzz_deploy" in ranked


def test_ranking_respects_the_injection_cap(store, memory):
    for i in range(_MAX_FACTS_IN_PROMPT + 10):
        store.memory_write("project", f"fact_{i:03d}", "deploy rollout", approved=True)
    block, _ = memory.facts_for_prompt(query="deploy rollout")
    assert len(block.splitlines()) == _MAX_FACTS_IN_PROMPT


def test_tier_breaks_ties_between_equally_relevant_facts(store, memory):
    store.memory_write("project", "local_rule", "deploy rollout", approved=True)
    store.memory_write("loop", "global_rule", "deploy rollout", approved=True)
    block, _ = memory.facts_for_prompt(query="deploy rollout")
    assert block.index("global_rule") < block.index("local_rule")


# -- the guarantees that must survive ranking ---------------------------------


def test_approval_gate_holds_against_an_exactly_matching_query(store, memory):
    """The strongest possible relevance signal must still lose to the gate."""
    store.memory_write("project", "unvetted_secret", "deploy rollout token")
    block, _ = memory.facts_for_prompt(query="deploy rollout token")
    assert "unvetted_secret" not in block


def test_pinned_facts_still_bypass_the_cap_under_ranking(store, memory):
    for i in range(_MAX_FACTS_IN_PROMPT + 5):
        store.memory_write("project", f"aaa_{i:03d}", "deploy rollout", approved=True)
    store.memory_write(
        "project", "zzz_pinned", "utterly unrelated", approved=True, pinned=True
    )
    block, _ = memory.facts_for_prompt(query="deploy rollout")
    assert "zzz_pinned" in block
    assert block.splitlines()[0].startswith("- (project) *")


def test_ranking_still_bumps_hit_counts_and_promotes(store, memory):
    store.memory_write("project", "hot", "deploy rollout", approved=True)
    for _ in range(3):
        memory.facts_for_prompt(query="deploy rollout")
    assert [r["key"] for r in store.memory_list(tier="loop")] == ["hot"]


# -- back-compatibility: no query means no behaviour change --------------------


def test_no_query_keeps_the_previous_ordering_and_yields_no_provenance(store, memory):
    store.memory_write("project", "aaa", "deploy rollout", approved=True)
    store.memory_write("loop", "bbb", "unrelated", approved=True)

    block, provenance = memory.facts_for_prompt()
    assert block.index("bbb") < block.index("aaa")  # loop tier first, as before
    assert provenance is None  # nothing was ranked, so there is nothing to log


def test_blank_query_is_treated_as_no_query(store, memory):
    store.memory_write("project", "aaa", "v", approved=True)
    assert memory.facts_for_prompt(query="   \n  ")[1] is None


def test_service_without_a_backend_falls_back_to_unranked(store):
    plain = MemoryService(store, backend=None)
    store.memory_write("project", "aaa", "deploy rollout", approved=True)
    block, provenance = plain.facts_for_prompt(query="deploy rollout")
    assert "aaa" in block
    assert provenance is None


# -- provenance ---------------------------------------------------------------


def test_provenance_records_what_was_fetched_and_why(store, memory):
    store.memory_write("project", "deploy_how", "deploy rollout", approved=True)
    store.memory_write("project", "unrelated", "colour of the bikeshed", approved=True)

    _, provenance = memory.facts_for_prompt(query="how do I deploy")

    assert provenance["query"] == "how do I deploy"
    assert provenance["backend"] == "hash"
    assert provenance["n_candidates"] == 2
    facts = {f["key"]: f for f in provenance["facts"]}
    assert set(facts) == {"deploy_how", "unrelated"}
    assert facts["deploy_how"]["score"] > facts["unrelated"]["score"]
    assert facts["deploy_how"]["tier"] == "project"
    assert isinstance(facts["deploy_how"]["id"], int)


def test_the_service_does_not_log_the_retrieval_itself(store, memory):
    """A retrieval is only meaningful attached to the attempt it fed, and this
    service does not know which attempt that is — so it hands the provenance
    back rather than writing an unattributable event."""
    store.memory_write("project", "k", "v", approved=True)
    memory.facts_for_prompt(query="how do I deploy")
    assert [e for e in store.events() if e["kind"] == "retrieval"] == []


def test_provenance_truncates_a_huge_query(store, memory):
    store.memory_write("project", "k", "v", approved=True)
    _, provenance = memory.facts_for_prompt(query="deploy " * 5000)
    assert len(provenance["query"]) <= 1000


# -- backend selection ---------------------------------------------------------


def test_the_vector_index_backend_is_gone_and_says_so():
    """The `RetrievalBackend` seam stays — a real embedder is still a drop-in —
    but the Chroma implementation was an optional, CI-untested code path that
    ranked identically to the stdlib one (same `embed()` on both sides). A
    config that still asks for it must fail loudly, not degrade silently."""
    for name in ("chroma", "auto"):
        with pytest.raises(ValueError, match=name):
            get_backend(name, LoopConfig())


def test_the_default_backend_needs_no_dependency():
    assert LoopConfig().memory_retrieval_backend == "hash"
    assert isinstance(
        get_backend(LoopConfig().memory_retrieval_backend), HashingBackend
    )


def test_none_disables_retrieval_entirely():
    assert get_backend("none", LoopConfig()) is None


def test_unknown_backend_name_is_rejected():
    with pytest.raises(ValueError):
        get_backend("pinecone", LoopConfig())
