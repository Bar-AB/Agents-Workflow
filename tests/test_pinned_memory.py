"""Pinned memory facts: pinned approved facts sort first and bypass the
injection cap under their own separate ceiling, so a must-have fact is not
dropped by the alphabetical tail-off past the cap."""

import pytest

from agentloop.memory import (MemoryService, _MAX_FACTS_IN_PROMPT,
                             _MAX_PINNED_FACTS)
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_pinned_fact_survives_past_the_cap_while_unpinned_drops(store):
    # Fill well past the cap with approved unpinned facts whose keys sort BEFORE
    # our probe fact, so ordinary alphabetical ordering would drop the probe.
    for i in range(_MAX_FACTS_IN_PROMPT + 5):
        store.memory_write("project", f"aaa_{i:03d}", f"filler {i}", approved=True)
    # 'zzz_unpinned' would fall off the end of the cap; 'zzz_pinned' is pinned.
    store.memory_write("project", "zzz_unpinned", "should drop", approved=True)
    store.memory_write("project", "zzz_pinned", "must stay", approved=True,
                       pinned=True)

    block = MemoryService(store).facts_for_prompt()
    assert "must stay" in block            # pinned survived
    assert "should drop" not in block      # unpinned past the cap dropped


def test_pinned_facts_come_first(store):
    store.memory_write("project", "mmm", "middle", approved=True)
    store.memory_write("project", "zzz", "last-alphabetically", approved=True,
                       pinned=True)
    block = MemoryService(store).facts_for_prompt()
    lines = block.splitlines()
    # The pinned fact leads despite sorting last alphabetically.
    assert "zzz" in lines[0]
    assert "*" in lines[0]                  # pinned marker


def test_pinning_still_requires_approval_to_be_read(store):
    # Pinned but NOT approved: gating still holds — it must not reach a prompt.
    store.memory_write("project", "secret", "unvetted", pinned=True)
    block = MemoryService(store).facts_for_prompt()
    assert "unvetted" not in block


def test_pinned_ceiling_is_bounded(store):
    # More pinned facts than the pinned ceiling: extras beyond the ceiling drop,
    # so pinning everything cannot reintroduce prompt crowding.
    for i in range(_MAX_PINNED_FACTS + 4):
        store.memory_write("loop", f"pin_{i:03d}", f"pinned {i}", approved=True,
                           pinned=True)
    block = MemoryService(store).facts_for_prompt()
    injected = [ln for ln in block.splitlines() if "*" in ln]
    assert len(injected) == _MAX_PINNED_FACTS


def test_pin_is_sticky_across_rewrites_and_toggleable(store):
    store.memory_write("project", "k", "v1", approved=True, pinned=True)
    # An agent re-writing the fact (pinned defaults False) must not silently
    # unpin it.
    store.memory_write("project", "k", "v2")
    row = next(r for r in store.memory_list() if r["key"] == "k")
    assert row["pinned"] == 1
    # Explicit unpin lowers it.
    store.memory_set_pinned(row["id"], False)
    row = next(r for r in store.memory_list() if r["key"] == "k")
    assert row["pinned"] == 0
    kinds = [e["kind"] for e in store.events()]
    assert "memory_unpinned" in kinds
