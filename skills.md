# Skills for developing agentloop

Project-specific skills that encode how to make the common, multi-file changes
to agentloop *correctly* — where getting it right means touching several files
in sync and honoring invariants that aren't obvious from any single file.

Each skill lives at `.claude/skills/<name>/SKILL.md` and is invocable via the
Skill tool. They're keyed to the extension seams called out in `CLAUDE.md` and
the roadmap.

| Skill | Use it when you're… | Touches |
|---|---|---|
| [`agentloop-add-runner`](.claude/skills/agentloop-add-runner/SKILL.md) | Adding a model/provider backend (litellm, OpenAI, Codex cross-validator) via the `ModelRunner` seam | `runner.py`, `get_runner`, `MODEL_PRICING` in `config.py`, `cli.py`, `pyproject.toml` |
| [`agentloop-decision-rule`](.claude/skills/agentloop-decision-rule/SKILL.md) | Adding/changing a loop decision rule — approve/revise/escalate, thresholds, budget, risk gating | `loop.py`, `config.py`, `README.md`, `CLAUDE.md`, `tests/test_loop.py` |
| [`agentloop-loop-test`](.claude/skills/agentloop-loop-test/SKILL.md) | Writing an end-to-end loop test with a scripted `MockRunner` | `tests/test_loop.py` |
| [`agentloop-store-schema`](.claude/skills/agentloop-store-schema/SKILL.md) | Changing the SQLite store — tables, columns, accessors, audit/memory tables | `store.py` (`_SCHEMA`), `CLAUDE.md` |
| [`agentloop-agent-role`](.claude/skills/agentloop-agent-role/SKILL.md) | Registering a new agent role (planner, specialized worker, cross-validator) | `registry.py`, `agents.py`, `config.py`, `loop.py` |

## How these map to the roadmap

- **Real tool use in worker** (roadmap #1) → `agentloop-agent-role` (tools
  metadata → real execution) + `agentloop-store-schema` (persist tool results).
- **Context-budget handoff** (roadmap #2) → `agentloop-agent-role`
  (`context_budget_tokens`) + `agentloop-decision-rule` (handoff trigger).
- **RAG store behind memory** (roadmap #3) → `agentloop-store-schema`.
- **Planner + parallel workers** (roadmap #4) → `agentloop-agent-role`.
- **Second-provider cross-validator** (roadmap #5) → `agentloop-add-runner` +
  `agentloop-agent-role`.

## Conventions every skill enforces

- **stdlib-only core** — vendor SDKs are optional extras, imported locally.
- **The audit log is load-bearing** — every state change logs an `events` row;
  `events` is append-only.
- **Never guess-approve** — unparseable/ambiguous paths fail safe to
  `NEEDS_HUMAN`, never to `DONE`.
- **Tests are e2e through the `Loop` with `MockRunner`** — one test per
  decision rule; assert the `escalation_reason`, not just the status.
- **Docs move with code** — decision-rule changes update `README.md` and
  `CLAUDE.md` in the same change.
