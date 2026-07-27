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
    ChromaBackend,
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


# -- backend selection and graceful degradation -------------------------------


def test_auto_selection_never_raises_and_yields_a_working_backend(tmp_path):
    backend = get_backend("auto", LoopConfig(memory_chroma_path=str(tmp_path / "c")))
    assert backend is not None
    assert backend.search("a", [{"id": 1, "key": "a", "value": "a"}], 1)[0][0] == 1


def test_the_default_backend_does_not_depend_on_optional_packages():
    """An unrelated `pip install chromadb` must not silently change how a run
    selects memory, or start writing an index to disk."""
    assert LoopConfig().memory_retrieval_backend == "hash"
    assert isinstance(
        get_backend(LoopConfig().memory_retrieval_backend), HashingBackend
    )


def test_none_disables_retrieval_entirely():
    assert get_backend("none", LoopConfig()) is None


def test_missing_chroma_degrades_to_the_stdlib_backend(monkeypatch, capsys):
    """The optional extra must never be load-bearing: asking for a backend that
    isn't installed warns and degrades rather than crashing the loop."""
    monkeypatch.setattr("agentloop.retrieval._import_chromadb", lambda: None)
    backend = get_backend("chroma", LoopConfig())
    assert isinstance(backend, HashingBackend)
    assert "chroma" in capsys.readouterr().out.lower()


def test_unknown_backend_name_is_rejected():
    with pytest.raises(ValueError):
        get_backend("pinecone", LoopConfig())


# -- the optional vector backend: integration logic, exercised offline --------


class _FakeCollection:
    """Enough of a Chroma collection to drive ChromaBackend deterministically.

    Lets the sync/filter/fallback logic be tested with no dependency installed —
    the real-chromadb tests below still run when the extra is present, but the
    code path is not shipped unexercised just because CI lacks the package.
    """

    def __init__(self):
        self.docs: dict[str, list[float]] = {}
        self.upserts = 0
        self.queries: list[int] = []
        self.raises = False

    def upsert(self, ids, embeddings, documents):
        self.upserts += 1
        for i, e in zip(ids, embeddings):
            self.docs[i] = e

    def query(self, query_embeddings, n_results):
        if self.raises:
            raise RuntimeError("index unavailable")
        self.queries.append(n_results)
        scored = sorted(
            self.docs.items(), key=lambda kv: -cosine(query_embeddings[0], kv[1])
        )
        return {"ids": [[i for i, _ in scored[:n_results]]]}


@pytest.fixture()
def fake_chroma(monkeypatch):
    collection = _FakeCollection()

    class _Client:
        def __init__(self, path):
            self.path = path

        def get_or_create_collection(self, name):
            return collection

    monkeypatch.setattr(
        "agentloop.retrieval._import_chromadb",
        lambda: type("chromadb", (), {"PersistentClient": _Client}),
    )
    return collection


CANDIDATES = [
    {"id": 1, "tier": "loop", "key": "test_cmd", "value": "pytest -q runs the tests"},
    {"id": 2, "tier": "project", "key": "bikeshed", "value": "colour choices"},
    {"id": 3, "tier": "project", "key": "deploy_how", "value": "deploy rollout script"},
]


def test_chroma_orders_identically_to_the_stdlib_backend(fake_chroma):
    """Same embedding function on both sides, so installing the extra swaps the
    index without changing which facts an agent sees."""
    backend = ChromaBackend(path="unused")
    query = "how do I deploy the rollout"
    assert backend.search(query, CANDIDATES, 3) == HashingBackend().search(
        query, CANDIDATES, 3
    )


def test_chroma_only_reindexes_facts_whose_content_changed(fake_chroma):
    backend = ChromaBackend(path="unused")
    backend.search("deploy", CANDIDATES, 3)
    backend.search("deploy", CANDIDATES, 3)
    assert fake_chroma.upserts == 1  # second call had nothing new to push

    changed = [dict(CANDIDATES[0], value="pytest -x now")] + CANDIDATES[1:]
    backend.search("deploy", changed, 3)
    assert fake_chroma.upserts == 2


def test_a_stale_index_entry_cannot_re_enter_a_prompt(fake_chroma):
    """The store is the source of truth: an id the index still knows about but
    the store no longer approves must not come back."""
    backend = ChromaBackend(path="unused")
    backend.search("deploy", CANDIDATES, 3)  # id 3 is now in the index

    survivors = [c for c in CANDIDATES if c["id"] != 3]  # approval revoked
    assert [i for i, _ in backend.search("deploy rollout", survivors, 3)] == [1, 2]


def test_an_index_failure_falls_back_to_exact_scoring(fake_chroma):
    """A broken cache degrades retrieval quality at worst, never a prompt."""
    backend = ChromaBackend(path="unused")
    fake_chroma.raises = True
    query = "how do I deploy the rollout"
    assert backend.search(query, CANDIDATES, 3) == HashingBackend().search(
        query, CANDIDATES, 3
    )


def test_chroma_backend_reports_provenance_through_the_service(store, fake_chroma):
    memory = MemoryService(store, backend=get_backend("chroma", LoopConfig()))
    store.memory_write("project", "deploy_how", "deploy rollout", approved=True)
    store.memory_write("project", "bikeshed", "colour choices", approved=True)

    _, provenance = memory.facts_for_prompt(query="how do I deploy")

    assert provenance["backend"] == "chroma"
    assert {f["key"] for f in provenance["facts"]} == {"deploy_how", "bikeshed"}


def test_empty_candidate_set_never_touches_the_index(fake_chroma):
    assert ChromaBackend(path="unused").search("q", [], 5) == []
    assert fake_chroma.queries == []


# -- the optional vector backend: against the real library --------------------


def test_chroma_backend_agrees_with_the_stdlib_backend(tmp_path):
    """Skipped unless `pip install agentloop[rag]`. Both backends embed with the
    same function, so Chroma is an index swap — the ordering must not move."""
    pytest.importorskip("chromadb")
    candidates = [
        {"id": 1, "tier": "project", "key": "deploy_how", "value": "deploy rollout"},
        {"id": 2, "tier": "project", "key": "bikeshed", "value": "colour choices"},
        {"id": 3, "tier": "loop", "key": "test_cmd", "value": "pytest -q runs tests"},
    ]
    chroma = ChromaBackend(path=str(tmp_path / "chroma"))
    query = "how do I deploy the rollout"
    # Ordering must match down to the tie-break: facts the query cannot
    # distinguish fall back to the caller's (tier, key) order in both backends,
    # so installing the extra never re-ranks anything on its own.
    assert [i for i, _ in chroma.search(query, candidates, 3)] == [
        i for i, _ in HashingBackend().search(query, candidates, 3)
    ]


def test_chroma_index_is_a_derived_cache_not_a_source_of_truth(tmp_path):
    """A fact revoked in SQLite must not resurface from a stale vector index —
    candidates are filtered upstream, so the index can only ever re-rank rows
    the store already approved."""
    pytest.importorskip("chromadb")
    chroma = ChromaBackend(path=str(tmp_path / "chroma"))
    candidates = [{"id": 1, "tier": "project", "key": "a", "value": "deploy rollout"}]
    chroma.search("deploy", candidates, 1)
    assert chroma.search("deploy", [], 1) == []
