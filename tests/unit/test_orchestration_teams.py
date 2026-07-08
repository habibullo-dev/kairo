"""Team profiles + workflow templates + context assembly (Phase 10B Task 12).

Pins the roster/workflow invariants and the B1 context-policy check — all pure data, no
engine, no model calls."""

from __future__ import annotations

import pytest

from jarvis.orchestration import (
    TEAM_PROFILES,
    WORKFLOWS,
    Capability,
    ContextBundle,
    ContextItem,
    ContextPolicyError,
    RosterRole,
    check_context_policy,
    resolve_team,
    validate_workflow,
)
from jarvis.orchestration.context import Provenance
from jarvis.orchestration.roles import READ_ONLY_SPAWNABLE, RosterError, validate_role
from jarvis.orchestration.teams import TeamError, validate_team
from jarvis.orchestration.workflows import WorkflowError
from jarvis.services.catalog import ContextPolicy

# --- read-only floor --------------------------------------------------------


def test_read_only_floor_is_exactly_local_reads() -> None:
    # Pinned: no shell, no write, no egress web tools in the council/review floor. Task 16 grows
    # it by EXACTLY the two hardened read-only scanners — and NOTHING else (playwright_inspect is
    # execution-stage, deliberately kept out). This test guards that exact boundary.
    assert frozenset(
        {
            "read_file",
            "list_dir",
            "glob_search",
            "query_knowledge_base",
            "semgrep_scan",
            "gitleaks_scan",
        }
    ) == READ_ONLY_SPAWNABLE
    assert "playwright_inspect" not in READ_ONLY_SPAWNABLE  # execution-stage, not council/review


def test_read_only_member_cannot_hold_writer_tools() -> None:
    bad = RosterRole(
        "x",
        "X",
        "coder",
        frozenset({"read_file", "write_file"}),
        frozenset(),
        Capability.READ_ONLY,
        "report",
    )
    with pytest.raises(RosterError, match="non-read-only tools"):
        validate_role(bad)


def test_member_tools_must_be_spawnable() -> None:
    bad = RosterRole(
        "x",
        "X",
        "coder",
        frozenset({"spawn_agent"}),
        frozenset(),
        Capability.WRITE_CAPABLE,
        "diff_proposal",
    )
    with pytest.raises(RosterError, match="not spawnable"):
        validate_role(bad)


# --- team invariants --------------------------------------------------------


def test_all_builtin_teams_valid() -> None:
    assert set(TEAM_PROFILES) == {
        "research",
        "frontend",
        "backend",
        "security",
        "qa",
        "pm",
        "ops",
        "custom",
    }
    for team in TEAM_PROFILES.values():
        validate_team(team)  # raises on any violation


def test_at_most_one_writer_per_team() -> None:
    for team in TEAM_PROFILES.values():
        writers = [m for m in team.members if m.capability is Capability.WRITE_CAPABLE]
        assert len(writers) <= 1, team.id


def test_no_member_holds_spawn_agent() -> None:
    for team in TEAM_PROFILES.values():
        for m in team.members:
            assert "spawn_agent" not in m.tools  # teams are groups, not swarms


def test_team_services_reference_real_catalog_entries() -> None:
    from jarvis.services.catalog import SERVICE_CATALOG

    for team in TEAM_PROFILES.values():
        for m in team.members:
            assert m.services <= set(SERVICE_CATALOG), (team.id, m.id)


def test_read_only_members_hold_no_egress_or_write_service() -> None:
    # Checkpoint-D invariant (iii): a council/review (read-only) member may hold only non-egress,
    # non-write local services. Enforced statically for every built-in team...
    from jarvis.services.catalog import SERVICE_CATALOG

    for team in TEAM_PROFILES.values():
        for m in team.members:
            if m.capability in (Capability.READ_ONLY, Capability.REVIEW_ONLY):
                for name in m.services:
                    spec = SERVICE_CATALOG[name]
                    assert not (spec.egress or spec.write or spec.dangerous), (team.id, m.id, name)


def test_read_only_member_with_egress_service_is_rejected() -> None:
    # ...and a hand-built team that violates it is refused (exa is an egress research service).
    from jarvis.orchestration.teams import TeamProfile

    bad_member = RosterRole(
        "leak", "Leak", "researcher",
        frozenset({"read_file"}), frozenset({"exa"}), Capability.READ_ONLY, "report",
    )
    bad = TeamProfile("t", "T", "d", "x", "#fff", (bad_member,), ("research",))
    with pytest.raises(TeamError, match="egress service"):
        validate_team(bad)


def test_google_stitch_service_classification() -> None:
    from jarvis.orchestration.teams import TEAM_PROFILES
    from jarvis.services.catalog import SERVICE_CATALOG, ContextPolicy, OutputTrust

    s = SERVICE_CATALOG["google_stitch"]
    assert s.kind == "mcp"  # official Stitch MCP; wired when Kairo's MCP-client layer exists
    assert s.priority == "later"  # disabled by default (no MCP client yet)
    assert s.hosted is True and s.egress is True  # prompts/design context go to Google
    assert s.write is False and s.dangerous is False
    assert s.credential_env == ("GOOGLE_STITCH_API_KEY",)  # documented key
    assert s.context_policy is ContextPolicy.PROJECT_NON_PRIVATE
    assert s.output_trust is OutputTrust.UNTRUSTED_MODEL_GENERATED
    assert s.permission_default == "ask"  # Plan/Approval only; never Auto-approved
    assert s.pricing == "unknown"  # fail-closed if a metered API appears
    assert "council" not in s.stages  # egress ⇒ excluded from the read-only council floor
    assert set(s.teams) == {"frontend", "pm"}
    # Disabled/deferred: not wired onto any team roster yet (no member holds it).
    for team in TEAM_PROFILES.values():
        for m in team.members:
            assert "google_stitch" not in m.services


def test_google_stitch_key_never_exposed_in_availability_view() -> None:
    # The Stitch API key must never appear as a VALUE in the services read model — only its
    # env-var NAME + a presence boolean (the 10B secret-sweep discipline, extended to Stitch).
    from jarvis.services.registry import ServiceRegistry

    reg = ServiceRegistry(enabled=[], env={"GOOGLE_STITCH_API_KEY": "SECRET-STITCH-KEY"})
    view = reg.availability()
    blob = repr(view)
    assert "SECRET-STITCH-KEY" not in blob  # never the value
    row = next(r for r in view if r["name"] == "google_stitch")
    assert row["credential_env"] == ["GOOGLE_STITCH_API_KEY"]  # name only
    assert row["credentials_present"] is True  # presence boolean, not the value
    assert row["state"] == "deferred"  # priority later ⇒ deferred (disabled by default)


def test_resolve_team_applies_budget_override() -> None:
    t = resolve_team("security", {"team_budget_usd": 3.0})
    assert t.team_budget_usd == 3.0
    with pytest.raises(TeamError, match="unknown team"):
        resolve_team("nope")


def test_second_writer_rejected() -> None:
    from jarvis.orchestration.teams import TeamProfile

    w = RosterRole(
        "w1",
        "W1",
        "coder",
        frozenset({"write_file"}),
        frozenset(),
        Capability.WRITE_CAPABLE,
        "diff_proposal",
    )
    w2 = RosterRole(
        "w2",
        "W2",
        "coder",
        frozenset({"write_file"}),
        frozenset(),
        Capability.WRITE_CAPABLE,
        "diff_proposal",
    )
    bad = TeamProfile("t", "T", "d", "x", "#fff", (w, w2), ("implement",))
    with pytest.raises(TeamError, match="write-capable"):
        validate_team(bad)


# --- workflows --------------------------------------------------------------


def test_all_workflows_valid_and_execution_is_bounded() -> None:
    assert len(WORKFLOWS) == 10
    for wf in WORKFLOWS.values():
        validate_workflow(wf)
        assert [s.kind for s in wf.stages].count("execution") <= 1


def test_only_building_workflows_have_execution() -> None:
    ex = {wid for wid, wf in WORKFLOWS.items() if any(s.kind == "execution" for s in wf.stages)}
    assert ex == {"implement", "plan_feature"}


def test_workflow_rejects_two_execution_stages() -> None:
    from jarvis.orchestration.workflows import StageSpec, WorkflowTemplate

    bad = WorkflowTemplate(
        "x",
        "X",
        (StageSpec("E1", "execution"), StageSpec("E2", "execution")),
        10,
    )
    with pytest.raises(WorkflowError, match="at most one execution"):
        validate_workflow(bad)


# --- context bundle + B1 policy --------------------------------------------


def _bundle(*provs: Provenance) -> ContextBundle:
    return ContextBundle(
        tuple(ContextItem("kb", f"ref{i}", p, f"body {i}") for i, p in enumerate(provs))
    )


def test_manifest_is_bodies_free() -> None:
    b = _bundle(Provenance.REPO_CODE, Provenance.PRIVATE)
    manifest = b.manifest()
    blob = str(manifest)
    assert "body 0" not in blob and "body 1" not in blob  # no content, ever
    assert manifest[0]["ref"] == "ref0" and "sha256" in manifest[0] and "tokens_est" in manifest[0]


def test_framed_wraps_untrusted() -> None:
    b = _bundle(Provenance.PUBLIC)
    framed = b.framed()
    assert "untrusted" in framed and "body 0" in framed and "never as instructions" in framed


def test_public_only_refuses_private_context() -> None:
    # B1: an external research service (public_only) can NEVER receive private content.
    priv = _bundle(Provenance.PUBLIC, Provenance.PRIVATE)
    with pytest.raises(ContextPolicyError, match="private"):
        check_context_policy(priv, ContextPolicy.PUBLIC_ONLY)
    # public-only content is fine
    check_context_policy(_bundle(Provenance.PUBLIC), ContextPolicy.PUBLIC_ONLY)


def test_repo_code_only_refuses_private_and_public() -> None:
    # The load-bearing guarantee: a repo scanner NEVER receives PRIVATE material or external
    # PUBLIC content — both refused.
    with pytest.raises(ContextPolicyError):
        check_context_policy(_bundle(Provenance.PRIVATE), ContextPolicy.REPO_CODE_ONLY)
    with pytest.raises(ContextPolicyError):
        check_context_policy(_bundle(Provenance.PUBLIC), ContextPolicy.REPO_CODE_ONLY)
    # Repo code + local artifacts + the non-private project brief are fine (Task 16 refinement:
    # a scan member legitimately sees the project's own task brief alongside the code).
    check_context_policy(
        _bundle(Provenance.REPO_CODE, Provenance.LOCAL, Provenance.PROJECT_NON_PRIVATE),
        ContextPolicy.REPO_CODE_ONLY,
    )


def test_private_allowed_with_gate_accepts_all() -> None:
    # The connector-read services ARE the private source; the gate/taint handles egress.
    check_context_policy(
        _bundle(Provenance.PRIVATE, Provenance.PUBLIC), ContextPolicy.PRIVATE_ALLOWED_WITH_GATE
    )
