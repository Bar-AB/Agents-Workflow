"""agentloop — general-purpose agentic development loop.

Vertical slice: define task -> worker executes -> self-check -> independent
validator -> approve / revise / escalate, with SQLite as the single source of
truth, an immutable audit log, and per-attempt metrics.
"""

__version__ = "0.1.0"
