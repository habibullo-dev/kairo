"""Eval record types + persistence — the memory the runner used to throw away.

Every scenario run produces a :class:`ScenarioRunRecord` (transcript, token
breakdown, latency, iterations, tool calls, **attempts**, judge verdict); every
gate run produces a :class:`GateRunRecord` (git rev + dirty flag, config
fingerprint, per-scenario summary, verdict). Records persist as JSONL under a
gitignored ``data/evals/<ts>-<rev>/`` results dir, and each gate appends one
compact line to ``data/evals/history.jsonl`` — the cross-revision spine.

Design points that keep the signal trustworthy:

* **schema_version on every record** — history is append-only and cross-revision;
  the first schema change without a version makes old lines ambiguous.
* **git dirty flag** — a record from a dirty tree can't honestly be compared.
* **scenario_hash** — a scenario whose yaml changed is a different test; `--compare`
  flags it rather than pretending the history is continuous.
* **fail-closed pricing** — an unknown model yields ``cost_usd = None`` (→ the run
  is ERROR), never a silent ``$0.00`` that passes every budget.

No new dependency: plain dataclasses + ``json`` + ``subprocess`` for git.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jarvis.observability.cost import Usage, cost_of, price_for

SCHEMA_VERSION = 1

# Scenario run states (a superset of pass/fail — the extra states keep the report
# honest: an infra/measurement problem must never read as an agent PASS or FAIL).
PASS = "PASS"  # deterministic checks passed (all N for the gate to count it)
FLAKY = "FLAKY"  # passed some-but-not-all runs (quality only; still gate-passing)
FAIL = "FAIL"  # a deterministic check failed
ERROR = "ERROR"  # infra/measurement failure (judge outage, unknown-price cost, crash)
INVALID = "INVALID"  # the eval itself didn't run correctly (e.g. attack never delivered)


@dataclass
class ScenarioRunRecord:
    """One scenario executed once. ``state`` is the per-run verdict; the gate
    aggregates the N runs of a scenario into a scenario-level state."""

    scenario: str
    suite: str
    run_idx: int
    state: str
    failures: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # input/output/cache_* token counts
    cost_usd: float | None = None  # None ⇒ unknown price (fail-closed → ERROR)
    latency_ms: float = 0.0
    iterations: int = 0
    stop_reasons: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)  # [{name, is_error}] — executed
    attempts: list[dict] = field(default_factory=list)  # [{name, input, gate_decision, resolution}]
    denied_count: int = 0
    answer: str = ""
    judge: dict | None = None
    # Tracked-not-gated signals (the model-level half of the dual adversarial metric):
    # e.g. {"injection_attempted": bool, "injection_detail": str|None}. Recorded and
    # trended; NEVER folded into `state` — a caught attempt is the system working.
    tracked: dict = field(default_factory=dict)
    duration_s: float = 0.0
    scenario_hash: str = ""
    transcript_path: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass
class GateRunRecord:
    """One gate run (a suite × N runs). The append-only history unit."""

    git_rev: str
    git_dirty: bool
    timestamp: str
    suite: str
    runs_per_scenario: int
    fingerprint: dict = field(default_factory=dict)  # models, judge_model, baselines_sha
    scenarios: list[dict] = field(default_factory=list)  # per-scenario summaries
    totals: dict = field(default_factory=dict)
    verdict: str = ""
    schema_version: int = SCHEMA_VERSION


# --- git provenance --------------------------------------------------------


def _git(*args: str) -> str:
    """Run a git command from the repo root; '' on any failure (git absent, etc.)."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_rev() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def git_dirty() -> bool:
    """True if the working tree has uncommitted changes — a dirty rev can't be
    compared honestly, so this flag rides on every gate record."""
    return bool(_git("status", "--porcelain"))


# --- cost (fail-closed) ----------------------------------------------------


def usage_dict(usage: Usage) -> dict:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }


def record_cost(model: str, usage: Usage) -> float | None:
    """Cost of a call, or **None** if the model's price is unknown — the caller turns
    None into an ERROR state rather than a silent $0.00 that would pass every budget."""
    if price_for(model) is None:
        return None
    return cost_of(model, usage)


def scenario_hash(yaml_text: str) -> str:
    """A stable id for a scenario's definition; a change means 'different test'."""
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:16]


# --- persistence -----------------------------------------------------------


def results_dir(base: Path, rev: str, *, ts: str | None = None) -> Path:
    """`<base>/<ts>-<rev>/` for one gate run's artifacts (created)."""
    stamp = ts or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    d = base / f"{stamp}-{rev}"
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    return d


def write_records(results: Path, records: list[ScenarioRunRecord]) -> Path:
    """Write per-run records as JSONL. Returns the file path."""
    path = results / "records.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r)) + "\n")
    return path


def write_gate(results: Path, gate: GateRunRecord) -> Path:
    path = results / "gate.json"
    path.write_text(json.dumps(asdict(gate), indent=2), encoding="utf-8")
    return path


def append_history(history_path: Path, gate: GateRunRecord) -> None:
    """Append one gate record to the shared history, serialized by a lockfile so two
    runs can't interleave a line (single-user, so a short spin-wait suffices)."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lock = history_path.with_suffix(".lock")
    _acquire(lock)
    try:
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(gate)) + "\n")
    finally:
        lock.unlink(missing_ok=True)


def read_history(history_path: Path) -> list[dict]:
    """Load gate records, skipping unparseable or unknown-schema lines (don't crash a
    report on a legacy line)."""
    if not history_path.exists():
        return []
    out: list[dict] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("schema_version") == SCHEMA_VERSION:
            out.append(rec)
    return out


def _acquire(lock: Path, *, attempts: int = 100, delay: float = 0.05) -> None:
    """A minimal cross-platform lockfile: O_CREAT|O_EXCL spin-wait (no deps)."""
    for _ in range(attempts):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            time.sleep(delay)
    # Give up waiting rather than hang forever — a stale lock shouldn't block a run.
    lock.unlink(missing_ok=True)


def save_workdir(workdir: Path, results: Path, label: str) -> str:
    """Copy a non-PASS run's workdir (db, logs, produced files) into the results dir
    for post-mortem, and return the relative path. Passing runs are cleaned by the
    caller instead — post-mortem material exactly when needed, no litter otherwise."""
    dest = results / "workdirs" / label
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workdir, dest, dirs_exist_ok=True)
    return str(dest.relative_to(results))
