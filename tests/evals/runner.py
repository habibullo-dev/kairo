"""Live smoke-eval runner.

Runs each scenario in tests/evals/scenarios/*.yaml against the *real* API, N times
(default 3 — agents are stochastic, so a single pass hides flakiness), in an
isolated temp working directory. A scenario passes only if all N runs pass.

Not a pytest test: it hits the network and costs money. Run explicitly:

    uv run python tests/evals/runner.py            # all scenarios, 3 runs each
    uv run python tests/evals/runner.py --runs 1   # quick single pass
    uv run python tests/evals/runner.py --scenario web_research
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

from jarvis.config import ConfigError, load_config
from jarvis.core import AgentLoop, AnthropicClient, ToolCall
from jarvis.core.events import Event, ToolStarted
from jarvis.observability.cost import cost_of
from jarvis.permissions import PermissionGate, load_policy
from jarvis.permissions.gate import Decision
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def load_scenarios() -> list[dict]:
    return [
        yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))
    ]


def make_approver(deny_tools: list[str]):
    async def approver(call: ToolCall, _decision: Decision) -> Permission:
        return Permission.DENY if call.name in deny_tools else Permission.ALLOW

    return approver


def evaluate(checks: list[dict], workdir: Path, answer: str, called: list[str]) -> list[str]:
    """Return a list of failure descriptions (empty == all checks passed)."""
    failures: list[str] = []
    for check in checks:
        kind = check["type"]
        if kind == "file_exists":
            if not (workdir / check["path"]).exists():
                failures.append(f"expected file {check['path']} to exist")
        elif kind == "file_absent":
            if (workdir / check["path"]).exists():
                failures.append(f"expected file {check['path']} to be absent")
        elif kind == "file_matches":
            path = workdir / check["path"]
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if not re.search(check["pattern"], text):
                failures.append(f"{check['path']} did not match /{check['pattern']}/")
        elif kind == "answer_matches":
            if not re.search(check["pattern"], answer):
                failures.append(f"answer did not match /{check['pattern']}/")
        elif kind == "tool_called":
            if check["name"] not in called:
                failures.append(f"expected tool {check['name']} to be called (called: {called})")
        elif kind == "tool_not_called":
            if check["name"] in called:
                failures.append(f"expected tool {check['name']} NOT to be called")
        else:
            failures.append(f"unknown check type {kind!r}")
    return failures


async def run_once(config, scenario: dict) -> tuple[list[str], float, str]:
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-eval-"))
    for name, content in scenario.get("setup", {}).get("files", {}).items():
        (workdir / name).write_text(content, encoding="utf-8")

    # Isolate the run by making the workdir the workspace *root*: tools and gate
    # both resolve relative paths against it (the unified resolution), so the
    # agent's files land here, not in the repo. The real permissions.yaml is
    # loaded from the project root before the override.
    policy_path = config.root / "config" / "permissions.yaml"
    run_config = config.model_copy(update={"root": workdir})

    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=run_config))
    executor = ToolExecutor(
        timeout=run_config.limits.tool_timeout_seconds,
        max_result_chars=run_config.limits.max_tool_result_chars,
    )
    gate = PermissionGate(load_policy(policy_path), workdir)
    loop = AgentLoop(
        client=AnthropicClient.from_config(run_config),
        registry=registry,
        executor=executor,
        gate=gate,
        config=run_config,
        approver=make_approver(scenario.get("deny_tools", [])),
    )

    called: list[str] = []

    def on_event(event: Event) -> None:
        if isinstance(event, ToolStarted):
            called.append(event.name)

    cwd = Path.cwd()
    os.chdir(workdir)
    try:
        result = await loop.run_turn(
            [{"role": "user", "content": scenario["prompt"]}], on_event=on_event
        )
    finally:
        os.chdir(cwd)

    failures = evaluate(scenario.get("checks", []), workdir, result.text, called)
    return failures, cost_of(config.models.main, result.usage), result.text


async def run_scenario(config, scenario: dict, runs: int) -> tuple[bool, float]:
    print(f"\n=== {scenario['name']} ===  {scenario.get('description', '')}")
    all_passed = True
    total_cost = 0.0
    for i in range(runs):
        failures, cost, answer = await run_once(config, scenario)
        total_cost += cost
        if failures:
            all_passed = False
            print(f"  run {i + 1}/{runs}: FAIL  (${cost:.4f})")
            for f in failures:
                print(f"      - {f}")
            print(f"      answer: {answer[:160].strip()!r}")
        else:
            print(f"  run {i + 1}/{runs}: PASS  (${cost:.4f})")
    verdict = "PASS" if all_passed else "FAIL"
    print(f"  => {verdict}  (total ${total_cost:.4f})")
    return all_passed, total_cost


async def run_all(config, runs: int, only: str | None) -> int:
    scenarios = load_scenarios()
    if only:
        scenarios = [s for s in scenarios if s["name"] == only]
        if not scenarios:
            print(f"No scenario named {only!r}.")
            return 2

    passed = 0
    total_cost = 0.0
    for scenario in scenarios:
        ok, cost = await run_scenario(config, scenario, runs)
        passed += int(ok)
        total_cost += cost

    print(f"\nSUMMARY: {passed}/{len(scenarios)} scenarios passed · total ${total_cost:.4f}")
    return 0 if passed == len(scenarios) else 1


def main() -> None:
    _force_utf8()
    parser = argparse.ArgumentParser(description="Run Jarvis smoke evals against the live API.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario (default 3).")
    parser.add_argument("--scenario", help="Run only this scenario by name.")
    args = parser.parse_args()

    try:
        config = load_config(require=("anthropic", "tavily"))
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(1)

    sys.exit(asyncio.run(run_all(config, args.runs, args.scenario)))


if __name__ == "__main__":
    main()
