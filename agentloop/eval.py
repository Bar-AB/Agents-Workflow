"""Validator calibration harness.

The loop's decision rules rest on the validator's CONFIDENCE number: approve at
>= 0.70 completes a task, < 0.40 escalates immediately. Nothing had measured
whether that number is calibrated — whether verdicts at 0.72 are actually more
reliable than at 0.68. This harness measures it.

It runs a fixed set of task/output fixtures with known-correct ("gold") verdicts
through `run_validator` and reports three things:

- agreement rate: fraction where the validator's verdict kind matches gold.
- a confusion matrix over approve/revise/escalate (gold rows, predicted cols).
- a calibration table: confidence bucketed against correctness, so you can see
  whether high-confidence verdicts are in fact more often right.

Runnable two ways:
- MockRunner (in CI, no credentials): each fixture carries a scripted validator
  line, so the run is deterministic. This exercises the *harness mechanics* —
  the fixtures' scripted answers are synthetic and cannot, on their own, confirm
  or refute the 0.70/0.40 thresholds.
- ClaudeSDKRunner (`agentloop eval --runner claude`, opt-in): the validator is
  the real model, so the numbers are a genuine calibration measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agents import run_validator
from .models import Task, VerdictKind
from .registry import Registry
from .runner import MockRunner
from .store import Store

# Bucket edges chosen to straddle the two live thresholds (0.40, 0.70) so the
# table directly informs "is there signal at these boundaries?". Upper edge is
# 1.01 so a confidence of exactly 1.0 lands in the top bucket.
CALIBRATION_EDGES = [0.0, 0.40, 0.70, 0.85, 1.01]


def _v(kind: str, conf: float, tests: str, reason: str) -> str:
    return f"VERDICT: {kind} CONFIDENCE: {conf:.2f} TESTS: {tests}\n{reason}"


@dataclass
class EvalFixture:
    id: str
    category: str  # good | subtly_wrong | ambiguous
    title: str
    goal: str
    criteria: str
    worker_output: str
    gold: VerdictKind  # the known-correct verdict for this scenario
    mock_line: str  # scripted validator output for the MockRunner path


# ~20 fixtures across three categories. `gold` is the verdict a well-calibrated
# validator should reach; `mock_line` is what the scripted mock validator
# "says" — deliberately wrong on a handful, so the confusion matrix and
# calibration table have off-diagonal / sub-1.0 cells to exercise.
FIXTURES: list[EvalFixture] = [
    # -- clearly good: gold approve -----------------------------------------
    EvalFixture(
        "g-slugify",
        "good",
        "slugify util",
        "Write slugify(text) -> lowercase hyphenated ascii.",
        "Lowercase, spaces->hyphens, strips punctuation, tested.",
        "def slugify(t): return re.sub(r'[^a-z0-9]+','-',t.lower()).strip('-')\n"
        "# tests: 'Hello World!' -> 'hello-world'",
        VerdictKind.APPROVE,
        _v("approve", 0.95, "pass", "Meets all criteria."),
    ),
    EvalFixture(
        "g-fib",
        "good",
        "fibonacci",
        "fib(n) returning the nth Fibonacci number, iterative.",
        "Correct for n>=0, O(n) time, handles n=0/1, tested.",
        "def fib(n):\n a,b=0,1\n for _ in range(n): a,b=b,a+b\n return a",
        VerdictKind.APPROVE,
        _v("approve", 0.90, "pass", "Correct and tested."),
    ),
    EvalFixture(
        "g-email",
        "good",
        "email validator",
        "is_email(s) basic RFC-lite validation.",
        "Rejects missing @/domain, accepts normal addresses, tested.",
        "def is_email(s): return bool(re.match(r'^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$',s))",
        VerdictKind.APPROVE,
        _v("approve", 0.82, "pass", "Reasonable and tested."),
    ),
    EvalFixture(
        "g-json",
        "good",
        "safe json load",
        "load_or(s, default) parses JSON, returns default on error.",
        "Returns parsed value or default; never raises; tested both paths.",
        "def load_or(s,d):\n try: return json.loads(s)\n except Exception: return d",
        VerdictKind.APPROVE,
        _v("approve", 0.78, "pass", "Both paths covered."),
    ),
    EvalFixture(
        "g-bsearch",
        "good",
        "binary search",
        "bsearch(a, x) -> index or -1, a sorted.",
        "Correct on hit/miss/empty, O(log n), tested.",
        "def bsearch(a,x):\n lo,hi=0,len(a)-1\n while lo<=hi:\n  m=(lo+hi)//2\n"
        "  if a[m]==x: return m\n  if a[m]<x: lo=m+1\n  else: hi=m-1\n return -1",
        VerdictKind.APPROVE,
        _v("approve", 0.88, "pass", "Standard, correct."),
    ),
    EvalFixture(
        "g-dedup",
        "good",
        "stable dedup",
        "dedup(xs) removes duplicates preserving first-seen order.",
        "Order preserved, works on any hashable, tested.",
        "def dedup(xs):\n seen=set(); out=[]\n for x in xs:\n"
        "  if x not in seen: seen.add(x); out.append(x)\n return out",
        # Validator is over-harsh here: gold is approve, mock says revise at low
        # confidence -> a low-confidence *wrong* verdict (calibration signal).
        VerdictKind.APPROVE,
        _v("revise", 0.55, "na", "Wants a docstring; no real defect."),
    ),
    EvalFixture(
        "g-csv",
        "good",
        "csv row reader",
        "read_rows(path) yields dict per row using the header.",
        "Uses csv module, handles quoted fields, tested on a sample.",
        "def read_rows(p):\n with open(p,newline='') as f:\n"
        "  yield from csv.DictReader(f)",
        VerdictKind.APPROVE,
        _v("approve", 0.72, "pass", "Correct use of csv."),
    ),
    EvalFixture(
        "g-retry",
        "good",
        "retry decorator",
        "@retry(n) retries a call n times then re-raises.",
        "Retries on exception, re-raises last, no retry on success, tested.",
        "def retry(n):\n def d(fn):\n  def w(*a,**k):\n   last=None\n"
        "   for _ in range(n):\n    try: return fn(*a,**k)\n"
        "    except Exception as e: last=e\n   raise last\n  return w\n return d",
        VerdictKind.APPROVE,
        _v("approve", 0.91, "pass", "Correct semantics."),
    ),
    # -- subtly wrong: gold revise ------------------------------------------
    EvalFixture(
        "sw-offbyone",
        "subtly_wrong",
        "range sum",
        "sum_to(n) returns 1+2+...+n.",
        "Correct inclusive sum for n>=1, tested.",
        "def sum_to(n): return sum(range(n))   # off by one: excludes n",
        VerdictKind.REVISE,
        _v("revise", 0.60, "fail", "range(n) excludes n; use range(n+1)."),
    ),
    EvalFixture(
        "sw-empty",
        "subtly_wrong",
        "average",
        "mean(xs) returns the arithmetic mean.",
        "Correct for non-empty; defined behavior on empty; tested.",
        "def mean(xs): return sum(xs)/len(xs)   # ZeroDivisionError on []",
        VerdictKind.REVISE,
        _v("revise", 0.50, "fail", "Empty input divides by zero; add a guard."),
    ),
    EvalFixture(
        "sw-mutates",
        "subtly_wrong",
        "sorted copy",
        "sorted_copy(xs) returns a sorted copy, leaving xs unchanged.",
        "Input list not mutated, returns new sorted list, tested.",
        "def sorted_copy(xs): xs.sort(); return xs   # mutates the caller's list",
        # Validator misses the mutation bug and approves at high confidence:
        # a high-confidence *wrong* verdict — the key miscalibration case.
        VerdictKind.REVISE,
        _v("approve", 0.80, "pass", "Looks correct, returns sorted list."),
    ),
    EvalFixture(
        "sw-overflow",
        "subtly_wrong",
        "percent",
        "pct(a, b) returns a/b as a percentage.",
        "Handles b==0 gracefully, correct otherwise, tested.",
        "def pct(a,b): return a/b*100   # b==0 raises",
        VerdictKind.REVISE,
        _v("revise", 0.45, "fail", "No guard for b==0."),
    ),
    EvalFixture(
        "sw-none",
        "subtly_wrong",
        "title case",
        "titlecase(s) title-cases a string.",
        "Handles None by returning '', tested.",
        "def titlecase(s): return s.title()   # AttributeError on None",
        VerdictKind.REVISE,
        _v("revise", 0.58, "fail", "None not handled per criteria."),
    ),
    EvalFixture(
        "sw-rounding",
        "subtly_wrong",
        "round half up",
        "round2(x) rounds to 2 decimals, half away from zero.",
        "2.675 -> 2.68 (not banker's rounding), tested.",
        "def round2(x): return round(x, 2)   # banker's rounding: 2.675 -> 2.67",
        # Validator approves at moderate-high confidence but gold is revise:
        # another high-confidence wrong verdict.
        VerdictKind.REVISE,
        _v("approve", 0.74, "pass", "round() to 2 dp looks fine."),
    ),
    EvalFixture(
        "sw-notest",
        "subtly_wrong",
        "untested parser",
        "parse_port(s) -> int in 1..65535 or raises ValueError.",
        "Range-checked and TESTED per criteria.",
        "def parse_port(s):\n p=int(s)\n return p   # no range check, no tests",
        VerdictKind.REVISE,
        _v("revise", 0.52, "na", "No range check and no tests, as required."),
    ),
    # -- ambiguous / unsalvageable: gold escalate ---------------------------
    EvalFixture(
        "am-locale",
        "ambiguous",
        "locale-sensitive slug",
        "slugify Unicode text 'correctly'.",
        "Handle non-ascii 'correctly' (undefined which transliteration).",
        "def slugify(t): return t.lower().replace(' ','-')  # drops accents? keeps?",
        VerdictKind.ESCALATE,
        _v("escalate", 0.25, "na", "Criteria don't define the transliteration."),
    ),
    EvalFixture(
        "am-format",
        "ambiguous",
        "date format",
        "format_date(d) in 'the standard format'.",
        "Use 'the standard format' (unspecified: ISO? locale? US?).",
        "def format_date(d): return d.strftime('%m/%d/%Y')  # which standard?",
        VerdictKind.ESCALATE,
        _v("escalate", 0.30, "na", "'Standard format' is underspecified."),
    ),
    EvalFixture(
        "am-approach",
        "ambiguous",
        "wrong problem",
        "Build an LRU cache.",
        "Evicts least-recently-used at capacity.",
        "class Cache(dict): pass   # solves a different problem entirely",
        VerdictKind.ESCALATE,
        _v("escalate", 0.20, "fail", "Unsalvageable: no eviction, wrong approach."),
    ),
    EvalFixture(
        "am-criteria",
        "ambiguous",
        "missing criteria",
        "Make it 'fast'.",
        "Be 'fast enough' (no measurable criterion given).",
        "def f(x): return x*2   # 'fast' is not a checkable criterion",
        # Validator picks revise instead of escalate: a wrong verdict kind that
        # is neither approve nor a confident escalate (matrix off-diagonal).
        VerdictKind.ESCALATE,
        _v("revise", 0.48, "na", "Asks for a benchmark; treats it as fixable."),
    ),
    EvalFixture(
        "am-unsalvage",
        "ambiguous",
        "contradictory spec",
        "Return a sorted list that preserves insertion order.",
        "Sorted AND insertion-ordered (mutually exclusive in general).",
        "def f(xs): return sorted(xs)   # can't satisfy both constraints",
        VerdictKind.ESCALATE,
        _v("escalate", 0.15, "na", "Criteria are self-contradictory."),
    ),
]


def mock_runner_for(fixtures: list[EvalFixture]) -> MockRunner:
    """A MockRunner pre-loaded with each fixture's scripted validator line, in
    order — so `run_validator` pops the right answer per fixture."""
    return MockRunner([f.mock_line for f in fixtures])


def _bucket_label(i: int) -> str:
    return f"[{CALIBRATION_EDGES[i]:.2f},{CALIBRATION_EDGES[i + 1]:.2f})"


def _bucket_index(conf: float) -> int:
    for i in range(len(CALIBRATION_EDGES) - 1):
        if CALIBRATION_EDGES[i] <= conf < CALIBRATION_EDGES[i + 1]:
            return i
    return len(CALIBRATION_EDGES) - 2  # clamp (e.g. conf == 1.0)


def run_eval(
    result_store: Store,
    runner,
    registry: Registry,
    fixtures: list[EvalFixture] | None = None,
) -> dict:
    """Run the fixtures through `run_validator` and persist a summary.

    The validator invocations run against a scratch in-memory store so they
    don't clutter the real task board or get picked up by the loop; only the
    eval summary lands in `result_store`.
    """
    fixtures = fixtures if fixtures is not None else FIXTURES
    kinds = [VerdictKind.APPROVE, VerdictKind.REVISE, VerdictKind.ESCALATE]
    confusion = {g.value: {p.value: 0 for p in kinds} for g in kinds}
    buckets = {i: {"n": 0, "correct": 0} for i in range(len(CALIBRATION_EDGES) - 1)}
    detail: list[dict] = []
    n_correct = 0

    scratch = Store(":memory:")
    try:
        for fx in fixtures:
            task = Task(
                id=None, title=fx.title, goal=fx.goal, acceptance_criteria=fx.criteria
            )
            scratch.add_task(task)
            verdict, _ = run_validator(
                scratch, runner, registry, task, fx.worker_output
            )
            pred = verdict.kind
            correct = pred == fx.gold
            n_correct += int(correct)
            confusion[fx.gold.value][pred.value] += 1
            bi = _bucket_index(verdict.confidence)
            buckets[bi]["n"] += 1
            buckets[bi]["correct"] += int(correct)
            detail.append(
                {
                    "id": fx.id,
                    "category": fx.category,
                    "gold": fx.gold.value,
                    "predicted": pred.value,
                    "confidence": round(verdict.confidence, 4),
                    "correct": correct,
                }
            )
    finally:
        scratch.close()

    n = len(fixtures)
    agreement = n_correct / n if n else 0.0
    calibration = []
    for i in range(len(CALIBRATION_EDGES) - 1):
        b = buckets[i]
        acc = (b["correct"] / b["n"]) if b["n"] else None
        calibration.append(
            {
                "bucket": _bucket_label(i),
                "n": b["n"],
                "correct": b["correct"],
                "accuracy": round(acc, 4) if acc is not None else None,
            }
        )

    summary = {
        "agreement": round(agreement, 4),
        "n": n,
        "confusion": confusion,
        "calibration": calibration,
    }
    result_store.add_eval_run(
        runner=type(runner).__name__,
        n_fixtures=n,
        agreement=agreement,
        summary=summary,
        detail=detail,
    )
    return {"summary": summary, "detail": detail}


def format_report(result: dict) -> str:
    """Human-readable text report of a run_eval result."""
    s = result["summary"]
    kinds = ["approve", "revise", "escalate"]
    lines = [
        f"Validator calibration — {s['n']} fixtures",
        f"Agreement with gold: {s['agreement'] * 100:.1f}%",
        "",
        "Confusion matrix (rows = gold, cols = predicted):",
        "            " + "".join(f"{k:>10s}" for k in kinds),
    ]
    for g in kinds:
        row = s["confusion"][g]
        lines.append(f"  {g:>9s} " + "".join(f"{row[p]:>10d}" for p in kinds))
    lines += [
        "",
        "Calibration (confidence bucket vs. correctness):",
        f"  {'bucket':>13s} {'n':>4s} {'correct':>8s} {'accuracy':>9s}",
    ]
    for c in s["calibration"]:
        acc = "  n/a" if c["accuracy"] is None else f"{c['accuracy'] * 100:5.1f}%"
        lines.append(f"  {c['bucket']:>13s} {c['n']:>4d} {c['correct']:>8d}  {acc:>8s}")
    return "\n".join(lines)
