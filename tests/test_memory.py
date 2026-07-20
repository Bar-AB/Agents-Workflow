"""Two-tier memory policy (spec §7): approval gating, promotion, auditability."""

import pytest

from agentloop.memory import MemoryService
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "mem.db")
    yield s
    s.close()


@pytest.fixture()
def memory(store):
    return MemoryService(store, promote_threshold=3)


def test_unapproved_facts_never_reach_a_prompt(store, memory):
    memory.remember("project", "sketchy", "possibly wrong")
    assert "sketchy" not in memory.facts_for_prompt()


def test_approved_facts_reach_the_prompt(store, memory):
    memory.remember("project", "test_command", "pytest -q", approved=True)
    block = memory.facts_for_prompt()
    assert "test_command" in block and "pytest -q" in block


def test_loop_facts_are_listed_before_project_facts(store, memory):
    memory.remember("project", "aaa_local", "local", approved=True)
    memory.remember("loop", "zzz_global", "global", approved=True)
    block = memory.facts_for_prompt()
    assert block.index("zzz_global") < block.index("aaa_local")


def test_rewriting_an_approved_fact_keeps_it_approved(store):
    """Regression: the ON CONFLICT clause used to copy the incoming (default
    False) approval over an existing True, silently hiding vetted memory."""
    store.memory_write("project", "k", "v1", approved=True)
    store.memory_write("project", "k", "v2")          # agent rewrite, unapproved

    assert store.memory_read("project", "k") == "v2"
    assert store.memory_list(tier="project")[0]["approved"] == 1


def test_explicit_approval_can_still_be_revoked(store):
    store.memory_write("project", "k", "v", approved=True)
    mem_id = store.memory_list(tier="project")[0]["id"]
    store.memory_set_approved(mem_id, False)
    assert store.memory_read("project", "k") is None


def test_hot_project_fact_is_promoted_to_loop(store, memory):
    memory.remember("project", "test_command", "pytest -q", approved=True)
    for _ in range(3):
        memory.read("project", "test_command")

    loop_keys = [r["key"] for r in store.memory_list(tier="loop")]
    assert "test_command" in loop_keys


def test_cold_fact_is_not_promoted(store, memory):
    memory.remember("project", "rare", "seldom used", approved=True)
    memory.read("project", "rare")
    assert store.memory_list(tier="loop") == []


def test_promotion_is_recorded_in_the_audit_log(store, memory):
    memory.remember("project", "k", "v", approved=True)
    for _ in range(3):
        memory.read("project", "k")

    promotions = [e for e in store.events() if e["kind"] == "memory_promoted"]
    assert promotions and promotions[0]["payload"]["key"] == "k"


def test_loop_tier_facts_are_not_re_promoted(store, memory):
    memory.remember("loop", "k", "v", approved=True)
    for _ in range(5):
        memory.read("loop", "k")
    assert memory.maybe_promote("loop", "k") is False


def test_every_write_is_auditable(store, memory):
    memory.remember("project", "a", "1")
    memory.remember("project", "b", "2", approved=True)
    writes = [e for e in store.events() if e["kind"] == "memory_write"]
    assert len(writes) == 2


def test_gating_helpers_drive_the_dashboard(store, memory):
    memory.remember("project", "candidate", "agent-proposed fact")
    row = store.memory_list()[0]
    assert row["approved"] == 0

    store.memory_set_approved(row["id"], True)
    assert "candidate" in memory.facts_for_prompt()

    store.memory_delete(row["id"])
    assert store.memory_list() == []


def test_deleting_a_missing_fact_raises(store):
    with pytest.raises(KeyError):
        store.memory_delete(4242)
