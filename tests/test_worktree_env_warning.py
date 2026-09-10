"""Slice 9 P4, test 31 - `sandbox_env_allowlist` can re-admit a
credential-shaped variable with no denylist; the fix is a warning, never a
refusal (CLAUDE.md: "a refusal it cannot justify becomes a knob someone
disables")."""

from __future__ import annotations

from agentloop.config import LoopConfig
from agentloop.executor import credential_like_names
from agentloop.loop import Loop
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store


def test_credential_like_names_matches_the_documented_patterns():
    """[RED-FIRST] against a tree with no `credential_like_names` at all this
    is an ImportError, not a behavioral failure - confirmed separately by
    running this file against the pre-P4 tree."""
    allowed = [
        "MY_SERVICE_API_KEY",
        "GITHUB_TOKEN",
        "DB_SECRET",
        "ADMIN_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "DATABASE_URL",  # not credential-shaped by this heuristic
        "FEATURE_FLAG",
    ]
    matched = credential_like_names(allowed)
    assert set(matched) == {
        "MY_SERVICE_API_KEY",
        "GITHUB_TOKEN",
        "DB_SECRET",
        "ADMIN_PASSWORD",
        "AWS_ACCESS_KEY_ID",
    }
    assert "DATABASE_URL" not in matched
    assert "FEATURE_FLAG" not in matched


def test_credential_like_names_is_total_and_case_insensitive():
    assert credential_like_names([]) == []
    assert credential_like_names(["my_api_key"]) == ["my_api_key"]


def test_constructing_a_loop_with_a_credential_shaped_allowlist_entry_warns(tmp_path):
    """A `config_warning` event, the existing channel — not a refusal: the
    loop must construct successfully and the entry must still be honored."""
    store = Store(tmp_path / "t.db")
    try:
        config = LoopConfig(
            db_path=store.db_path,
            workspace_root=str(tmp_path / "ws"),
            sandbox_env_allowlist=["ANTHROPIC_API_KEY", "DATABASE_URL"],
        )
        Loop(store, MockRunner([]), Registry.load(), config)

        warnings_ = [
            e
            for e in store.events(None)
            if e["kind"] == "config_warning"
            and "sandbox_env_allowlist_credential_like" in e["payload"]
        ]
        assert len(warnings_) == 1
        assert warnings_[0]["payload"]["sandbox_env_allowlist_credential_like"] == [
            "ANTHROPIC_API_KEY"
        ]
        assert "not refused" in warnings_[0]["payload"]["message"].lower() or (
            "not silent" in warnings_[0]["payload"]["message"].lower()
        )
    finally:
        store.close()


def test_a_clean_allowlist_produces_no_credential_warning(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        config = LoopConfig(
            db_path=store.db_path,
            workspace_root=str(tmp_path / "ws"),
            sandbox_env_allowlist=["DATABASE_URL", "CI"],
        )
        Loop(store, MockRunner([]), Registry.load(), config)

        warnings_ = [
            e
            for e in store.events(None)
            if e["kind"] == "config_warning"
            and "sandbox_env_allowlist_credential_like" in e["payload"]
        ]
        assert warnings_ == []
    finally:
        store.close()


def test_the_matched_variable_is_still_actually_admitted_never_refused(tmp_path):
    """The warning must not become a silent removal - a real operator need
    (a provider key a real test suite requires) must still reach the child
    process. Checked at `TestExecutor._child_env`, the seam that actually
    admits it, independent of `Loop`."""
    import os

    from agentloop.executor import TestExecutor

    os.environ["SOME_TEST_API_KEY"] = "not-a-real-secret"
    try:
        ex = TestExecutor(env_allowlist=["SOME_TEST_API_KEY"])
        env = ex._child_env()
        assert env.get("SOME_TEST_API_KEY") == "not-a-real-secret"
    finally:
        del os.environ["SOME_TEST_API_KEY"]
