"""Local service adapters — Semgrep, Gitleaks, Playwright-localhost (Phase 10B Task 16).

Keyless: ``run_cli`` is monkeypatched (no scanner binary) and the Playwright driver is injected
(no browser). Pins the binding constraints:

* B4 — scanners exclude the sensitive floor (derived from paths.py) AND filter findings whose
  path is sensitive; gitleaks output is file:line + rule id ONLY (never a matched value).
* B3 — playwright is localhost-only + inspect-only (5 verbs; no click/type/submit/eval).
* #5 — the tool's egress/write/dangerous/reads_private/permission come from the ServiceSpec.
* #6/#7 — the engine runs check_context_policy at the call-site (drops a service for a bundle it
  may not receive) and adapter output is framed per output_trust.
* #8 — a service_calls row is written (metadata only) with project/team/role/stage attribution.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

# `jarvis.core` is imported first (below) to resolve a pre-existing ledger<->core.context import
# cycle that only bites when this file is collected in isolation — the app always imports a core
# module before the ledger.
import jarvis.core  # noqa: F401
from jarvis.config import load_config
from jarvis.services import exclusions, tooling
from jarvis.services.catalog import SERVICE_CATALOG, OutputTrust
from jarvis.services.gitleaks import GitleaksScanTool
from jarvis.services.gitleaks import parse_findings as gitleaks_parse
from jarvis.services.playwright_local import (
    INSPECT_VERBS,
    PlaywrightInspectTool,
    set_driver,
    url_is_localhost,
)
from jarvis.services.semgrep import SemgrepScanTool
from jarvis.services.semgrep import parse_findings as semgrep_parse
from jarvis.tools.base import Permission, ToolContext


def _ctx(tmp_path: Path, *, enabled=("semgrep", "gitleaks", "playwright_local")) -> ToolContext:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = list(enabled)
    return ToolContext(config=cfg)


# --- availability is fail-closed (derives from the ServiceRegistry) ---------


def test_adapter_registration_is_flag_gated(tmp_path: Path) -> None:
    on = _ctx(tmp_path)
    assert SemgrepScanTool.is_available(on) and GitleaksScanTool.is_available(on)
    assert PlaywrightInspectTool.is_available(on)
    off = _ctx(tmp_path, enabled=())
    assert not SemgrepScanTool.is_available(off)
    assert not GitleaksScanTool.is_available(off)
    assert not PlaywrightInspectTool.is_available(off)


def test_policy_is_derived_from_spec() -> None:
    # #5: gate/taint ClassVars come from the catalog row, not hand-set on the tool.
    for tool in (SemgrepScanTool, GitleaksScanTool, PlaywrightInspectTool):
        spec = SERVICE_CATALOG[tool.service_name]
        assert tool.egress == spec.egress
        assert tool.write == spec.write
        assert tool.dangerous == spec.dangerous
        assert tool.permission_default == Permission(spec.permission_default)
        assert tool.reads_private is False  # none of the "now" services read private
    # scanners are ALLOW (read-only, safe in council); playwright is ASK (execution).
    assert SemgrepScanTool.permission_default is Permission.ALLOW
    assert PlaywrightInspectTool.permission_default is Permission.ASK


# --- B4: sensitive-path exclusions + finding filter -------------------------


def test_exclude_globs_cover_the_sensitive_floor() -> None:
    globs = exclusions.exclude_globs()
    # Derived from paths.py — covers env files, keys, token stores, secret dirs.
    assert any(".env" in g for g in globs)
    assert any("data/connectors" in g for g in globs)
    assert any(g.endswith(".pem") for g in globs)
    assert ".ssh" in globs


def test_finding_is_sensitive_uses_the_floor(tmp_path: Path) -> None:
    assert exclusions.finding_is_sensitive(".env", tmp_path) is True
    assert exclusions.finding_is_sensitive("data/connectors/google_token.json", tmp_path) is True
    assert exclusions.finding_is_sensitive("src/app.py", tmp_path) is False


def test_semgrep_findings_drop_sensitive_paths(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "results": [
                {
                    "path": "src/app.py",
                    "start": {"line": 10},
                    "check_id": "rule.a",
                    "extra": {"message": "m", "severity": "ERROR"},
                },
                {
                    "path": ".env",
                    "start": {"line": 1},
                    "check_id": "rule.b",
                    "extra": {"message": "secret in env"},
                },
            ]
        }
    )
    findings = semgrep_parse(raw, tmp_path)
    assert [f["file"] for f in findings] == ["src/app.py"]  # .env finding dropped


def test_gitleaks_findings_are_file_line_rule_only(tmp_path: Path) -> None:
    # #4: the matched secret value must NEVER survive parsing.
    raw = json.dumps(
        [
            {
                "File": "src/config.py",
                "StartLine": 42,
                "RuleID": "aws-key",
                "Secret": "AKIA-SUPER-SECRET-VALUE",
                "Match": "key = AKIA-SUPER-SECRET-VALUE",
                "Description": "AWS key AKIA-SUPER-SECRET-VALUE",
            },
            {"File": ".env", "StartLine": 3, "RuleID": "generic", "Secret": "hunter2"},
        ]
    )
    findings = gitleaks_parse(raw, tmp_path)
    assert findings == [{"file": "src/config.py", "line": 42, "rule": "aws-key"}]  # .env dropped
    blob = json.dumps(findings)
    assert "AKIA-SUPER-SECRET-VALUE" not in blob and "hunter2" not in blob and "Match" not in blob


async def test_semgrep_refuses_paths_outside_repo(tmp_path: Path) -> None:
    tool = SemgrepScanTool(_ctx(tmp_path))
    out = await tool.run(tool.Params(path="../../etc"))
    assert out.is_error and "escapes the project root" in out.content


async def test_semgrep_refuses_sensitive_target(tmp_path: Path) -> None:
    tool = SemgrepScanTool(_ctx(tmp_path))
    out = await tool.run(tool.Params(path=".env"))
    assert out.is_error and "sensitive" in out.content


async def test_semgrep_output_is_framed_untrusted(tmp_path: Path, monkeypatch) -> None:
    # security findings quote code ⇒ framed security_finding_untrusted (B2).
    def fake_run(argv, *, cwd, timeout=120.0):
        return tooling.CliResult(returncode=1, stdout=json.dumps({"results": []}), stderr="")

    monkeypatch.setattr(tooling, "run_cli", fake_run)
    monkeypatch.setattr("jarvis.services.semgrep.run_cli", fake_run)
    tool = SemgrepScanTool(_ctx(tmp_path))
    out = await tool.run(tool.Params(path="."))
    assert not out.is_error
    assert "security_finding_untrusted" in out.content  # framed per the spec


# --- B3: playwright localhost-only + inspect-only ---------------------------


def test_url_allowlist_is_localhost_only() -> None:
    assert url_is_localhost("http://127.0.0.1:5173/") is True
    assert url_is_localhost("http://localhost:3000/app") is True
    assert url_is_localhost("https://example.com/") is False  # external ⇒ refused (non-egress)
    assert url_is_localhost("http://169.254.169.254/") is False  # cloud metadata ⇒ refused
    assert url_is_localhost("file:///etc/passwd") is False
    # port narrowing
    assert url_is_localhost("http://127.0.0.1:9999/", allow_ports=[5173]) is False
    assert url_is_localhost("http://127.0.0.1:5173/", allow_ports=[5173]) is True


def test_inspect_verbs_exclude_interaction() -> None:
    assert {"navigate", "screenshot", "dom_inspect", "a11y_check", "visual_diff"} == INSPECT_VERBS
    for forbidden in ("click", "type", "submit", "eval", "fill", "press"):
        assert forbidden not in INSPECT_VERBS


async def test_playwright_refuses_interaction_verbs(tmp_path: Path) -> None:
    tool = PlaywrightInspectTool(_ctx(tmp_path))
    out = await tool.run(tool.Params(verb="click", url="http://127.0.0.1:5173/"))
    assert out.is_error and "inspect-only" in out.content


async def test_playwright_refuses_non_localhost(tmp_path: Path) -> None:
    tool = PlaywrightInspectTool(_ctx(tmp_path))
    out = await tool.run(tool.Params(verb="screenshot", url="https://evil.example.com/"))
    assert out.is_error and "non-localhost" in out.content


async def test_playwright_inspect_runs_via_injected_driver(tmp_path: Path) -> None:
    from jarvis.services.playwright_local import _NotInstalledDriver

    class FakeDriver:
        async def inspect(self, verb, url, selector):
            return f"[{verb}] {url}"

    set_driver(FakeDriver())
    try:
        tool = PlaywrightInspectTool(_ctx(tmp_path))
        out = await tool.run(tool.Params(verb="screenshot", url="http://localhost:5173/"))
        assert not out.is_error and "[screenshot]" in out.content
    finally:
        set_driver(_NotInstalledDriver())  # restore the default stub for other tests


# --- #8: service_calls ledger records metadata with attribution -------------


async def test_service_call_is_ledgered(tmp_path: Path) -> None:
    from jarvis.observability.ledger import CostContext, ServiceLedger, cost_context
    from jarvis.persistence.db import connect
    from jarvis.projects import ProjectStore

    db = await connect(tmp_path / "l.db")
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    ledger = ServiceLedger(db, lock)
    ctx = _ctx(tmp_path)
    ctx.service_ledger = ledger

    def fake_run(argv, *, cwd, timeout=120.0):
        return tooling.CliResult(returncode=0, stdout="[]", stderr="")

    import jarvis.services.gitleaks as gl

    gl.run_cli = fake_run  # type: ignore[assignment]
    tool = GitleaksScanTool(ctx)
    token = cost_context.set(
        CostContext(project_id=1, team="security", agent_role="scanner", stage="council")
    )
    try:
        out = await tool.run(tool.Params(path="."))
    finally:
        cost_context.reset(token)
    assert not out.is_error
    cur = await db.execute(
        "SELECT service, team, agent_role, stage, project_id, est_cost_usd FROM service_calls"
    )
    row = await cur.fetchone()
    await db.close()
    assert row == ("gitleaks", "security", "scanner", "council", 1, 0.0)  # fixed_zero known 0.0


def test_playwright_output_trust_is_trusted_local_scan() -> None:
    assert SERVICE_CATALOG["playwright_local"].output_trust is OutputTrust.TRUSTED_LOCAL_SCAN
    assert SERVICE_CATALOG["semgrep"].output_trust is OutputTrust.SECURITY_FINDING_UNTRUSTED
