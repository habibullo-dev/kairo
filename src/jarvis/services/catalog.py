"""The SERVICE_CATALOG — every candidate team service, classified as a code constant.

This is the Team Tool Intelligence Matrix as data. Rows exist for services we do NOT
implement in 10B (priority ``later``/``avoid``) so the UI can show them as deferred; a row is
NOT an enablement. Enforcement is DERIVED from these fields (see :mod:`.registry` and the
orchestration engine), never hand-tuned per service:

* ``egress`` / ``write`` / ``dangerous`` → the tool's gate + taint ClassVars.
* ``stages`` → which orchestration stages may hold it (council/review are read-only floors).
* ``context_policy`` (B1) → what context the service may RECEIVE (public tools never get
  private project content).
* ``output_trust`` (B2) → how its output is framed before a model reuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ContextPolicy(StrEnum):
    """What context a service may RECEIVE (amendment B1)."""

    PUBLIC_ONLY = "public_only"  # external web/research — never private project content
    PROJECT_NON_PRIVATE = "project_non_private"  # project metadata, no private memory/mail
    REPO_CODE_ONLY = "repo_code_only"  # source files only (scanners)
    LOCAL_ONLY = "local_only"  # never leaves the box (converters, local inspect)
    PRIVATE_ALLOWED_WITH_GATE = "private_allowed_with_gate"  # the private source itself (drive)
    NEVER_PRIVATE = "never_private"  # explicit hard no


class OutputTrust(StrEnum):
    """How a service's OUTPUT is classified before a model reuses it (amendment B2)."""

    TRUSTED_LOCAL_SCAN = "trusted_local_scan"  # deterministic local read — unframed OK
    UNTRUSTED_EXTERNAL_CONTENT = "untrusted_external_content"  # fetched web/doc content
    UNTRUSTED_MODEL_GENERATED = "untrusted_model_generated"  # another model's output
    SECURITY_FINDING_UNTRUSTED = "security_finding_untrusted"  # a finding that quotes code
    DERIVED_SUMMARY = "derived_summary"  # a summary over the above


@dataclass(frozen=True)
class ServiceSpec:
    """One catalog row. ``credential_env`` empty ⇒ no key needed. ``pricing`` ∈
    fixed_zero (known free) | metered (needs a pricing.yaml services entry) | unknown."""

    name: str
    teams: tuple[str, ...]
    kind: str  # native | cli | mcp | browser | ritual
    hosted: bool
    credential_env: tuple[str, ...]
    pricing: str  # fixed_zero | metered | unknown
    sensitivity: str  # low | med | high
    egress: bool
    write: bool
    dangerous: bool
    stages: frozenset[str]  # ⊆ {council, review, execution}
    permission_default: str  # allow | ask | deny
    context_policy: ContextPolicy
    output_trust: OutputTrust
    priority: str  # now | later | avoid
    note: str = ""


_ALL = frozenset({"council", "review", "execution"})
_RE = frozenset({"review", "execution"})
_E = frozenset({"execution"})


def _s(**kw: object) -> ServiceSpec:
    return ServiceSpec(**kw)  # type: ignore[arg-type]


#: The catalog. Only priority=="now" services get adapters in 10B (Task 16), and each is still
#: behind a feature flag (default OFF). Everything else is documentation the UI renders as
#: "deferred" until a future task builds the adapter + enables the flag.
SERVICE_CATALOG: dict[str, ServiceSpec] = {
    s.name: s
    for s in (
        # --- Research (external web/research = public_only, egress, execution-stage only) ---
        _s(
            name="firecrawl",
            teams=("research",),
            kind="native",
            hosted=True,
            credential_env=("FIRECRAWL_API_KEY",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="now",  # Phase 13 Task 3: firecrawl_scrape adapter shipped
        ),
        _s(
            name="exa",
            teams=("research",),
            kind="native",
            hosted=True,
            credential_env=("EXA_API_KEY",),
            pricing="metered",
            sensitivity="low",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="jina_reader",
            teams=("research",),
            kind="native",
            hosted=True,
            credential_env=("JINA_API_KEY",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="searxng",
            teams=("research",),
            kind="cli",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="low",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
            note="local install but proxies queries to public engines ⇒ egress",
        ),
        _s(
            name="obsidian_mcp",
            teams=("research", "pm"),
            kind="mcp",
            hosted=False,
            credential_env=("OBSIDIAN_API_KEY",),
            pricing="fixed_zero",
            sensitivity="high",
            egress=False,
            write=True,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.LOCAL_ONLY,
            output_trust=OutputTrust.DERIVED_SUMMARY,
            priority="later",
            note="native wiki covers this now; no MCP client in 10B",
        ),
        _s(
            name="notebooklm",
            teams=("research",),
            kind="mcp",
            hosted=True,
            credential_env=("NOTEBOOKLM_TOKEN",),
            pricing="unknown",
            sensitivity="high",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="deny",
            context_policy=ContextPolicy.NEVER_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="avoid",
            note="B5: future only — org auth, opaque pricing, high sensitivity",
        ),
        # --- Frontend / UX ---
        _s(
            name="playwright_local",
            teams=("frontend", "qa"),
            kind="native",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="med",
            egress=False,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.LOCAL_ONLY,
            output_trust=OutputTrust.TRUSTED_LOCAL_SCAN,
            priority="now",
            note="B3: localhost + inspect-only (screenshot/DOM/a11y/visual-diff); no click/type. "
            "Execution-stage only (ASK-gated) so it never enters the read-only council/review "
            "floor — held by a write-capable member.",
        ),
        _s(
            name="figma_mcp",
            teams=("frontend",),
            kind="mcp",
            hosted=True,
            credential_env=("FIGMA_TOKEN",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_RE,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="openai_image",
            teams=("frontend",),
            kind="native",
            hosted=True,
            credential_env=("OPENAI_API_KEY",),
            pricing="metered",
            sensitivity="low",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_MODEL_GENERATED,
            priority="later",
        ),
        _s(
            name="browserbase",
            teams=("frontend", "qa"),
            kind="native",
            hosted=True,
            credential_env=("BROWSERBASE_API_KEY",),
            pricing="metered",
            sensitivity="high",
            egress=True,
            write=True,
            dangerous=True,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="browser_mcp",
            teams=("frontend",),
            kind="mcp",
            hosted=False,
            credential_env=(),
            pricing="unknown",
            sensitivity="high",
            egress=True,
            write=True,
            dangerous=True,
            stages=_E,
            permission_default="deny",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="avoid",
            note="B5: generic browser — avoid until an MCP layer + gating exist",
        ),
        _s(
            name="google_stitch",
            teams=("frontend", "pm"),
            kind="mcp",  # official Stitch MCP integration (wired when Kairo's MCP client lands)
            hosted=True,
            # Documented key: GOOGLE_STITCH_API_KEY (namespaced to the Google product). Presence-
            # checked by the ServiceRegistry via env; NEVER surfaced as a value (name-only in UI).
            credential_env=("GOOGLE_STITCH_API_KEY",),
            pricing="unknown",  # fail closed until real pricing is known (a metered API ⇒ blocked)
            sensitivity="med",
            egress=True,  # prompts + design context go to Google
            write=False,
            dangerous=False,
            # Requested design/council/review; council is EXCLUDED because Stitch is egress and the
            # read-only council/review floor admits no egress tool (10B non-negotiable). Available
            # at review (design critique) + execution (variant generation).
            stages=_RE,
            permission_default="ask",  # Plan/Approval only — service tools are never Auto-approved
            context_policy=ContextPolicy.PROJECT_NON_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_MODEL_GENERATED,
            priority="later",
            note=(
                "Google Stitch — hosted design-GENERATION service (official MCP). A REAL "
                "Frontend/Product service, DISABLED by default: it needs Kairo's MCP-client "
                "layer (not built yet, ADR-0015) + a review of the official Stitch MCP package "
                "before enablement — no unofficial wrappers. Frontend may ask it for design "
                "variants, DESIGN.md, design tokens, layout references, and screen flows. Output "
                "imports as Kairo artifacts (produced_by=google_stitch, "
                "trust=untrusted_model_generated) — NEVER executed or committed directly; "
                "Claude/Opus adapts the design into Kairo's frontend. NEVER send private project "
                "data, secrets, customer data, source code, or internal screenshots without "
                "explicit Gate approval. Key GOOGLE_STITCH_API_KEY is presence-checked only, "
                "never exposed in the UI/read models."
            ),
        ),
        # --- Backend / Data ---
        _s(
            name="github_mcp",
            teams=("backend", "pm", "ops"),
            kind="cli",
            hosted=True,
            credential_env=("GITHUB_TOKEN",),
            pricing="fixed_zero",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_RE,
            permission_default="ask",
            context_policy=ContextPolicy.PROJECT_NON_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
            note="read-only (gh) first; writes are a separate dangerous step",
        ),
        _s(
            name="docker_mcp",
            teams=("backend", "ops"),
            kind="cli",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="high",
            egress=False,
            write=True,
            dangerous=True,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.LOCAL_ONLY,
            output_trust=OutputTrust.TRUSTED_LOCAL_SCAN,
            priority="later",
        ),
        _s(
            name="supabase_mcp",
            teams=("backend",),
            kind="mcp",
            hosted=True,
            credential_env=("SUPABASE_KEY",),
            pricing="metered",
            sensitivity="high",
            egress=True,
            write=True,
            dangerous=True,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PROJECT_NON_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="sqlite_ro",
            teams=("backend",),
            kind="native",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="high",
            egress=False,
            write=False,
            dangerous=False,
            stages=_RE,
            permission_default="ask",
            context_policy=ContextPolicy.LOCAL_ONLY,
            output_trust=OutputTrust.TRUSTED_LOCAL_SCAN,
            priority="later",
        ),
        # --- Security (local scanners = repo_code_only, findings framed untrusted) ---
        _s(
            name="semgrep",
            teams=("security",),
            kind="cli",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="med",
            egress=False,
            write=False,
            dangerous=False,
            stages=_ALL,
            permission_default="allow",
            context_policy=ContextPolicy.REPO_CODE_ONLY,
            output_trust=OutputTrust.SECURITY_FINDING_UNTRUSTED,
            priority="now",
            note="B4: excludes Kairo sensitive paths; offline rules",
        ),
        _s(
            name="gitleaks",
            teams=("security",),
            kind="cli",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="high",
            egress=False,
            write=False,
            dangerous=False,
            stages=_ALL,
            permission_default="allow",
            context_policy=ContextPolicy.REPO_CODE_ONLY,
            output_trust=OutputTrust.SECURITY_FINDING_UNTRUSTED,
            priority="now",
            note="B4: sensitive-path exclusions; findings redacted to file:line + rule id",
        ),
        _s(
            name="codeql",
            teams=("security",),
            kind="cli",
            hosted=False,
            credential_env=(),
            pricing="fixed_zero",
            sensitivity="med",
            egress=False,
            write=False,
            dangerous=False,
            stages=_RE,
            permission_default="ask",
            context_policy=ContextPolicy.REPO_CODE_ONLY,
            output_trust=OutputTrust.SECURITY_FINDING_UNTRUSTED,
            priority="later",
        ),
        _s(
            name="promptfoo",
            teams=("security", "qa"),
            kind="cli",
            hosted=False,
            credential_env=("ANTHROPIC_API_KEY",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_MODEL_GENERATED,
            priority="later",
            note="calls LLMs ⇒ egress + LLM spend",
        ),
        # --- QA / Eval ---
        _s(
            name="langsmith",
            teams=("qa",),
            kind="native",
            hosted=True,
            credential_env=("LANGSMITH_API_KEY",),
            pricing="metered",
            sensitivity="high",
            egress=True,
            write=False,
            dangerous=False,
            stages=_E,
            permission_default="deny",
            context_policy=ContextPolicy.NEVER_PRIVATE,
            output_trust=OutputTrust.DERIVED_SUMMARY,
            priority="avoid",
            note="B5-adjacent: ships trace CONTENT off-box; revisit only with consent+redaction",
        ),
        _s(
            name="browserstack",
            teams=("qa",),
            kind="native",
            hosted=True,
            credential_env=("BROWSERSTACK_KEY",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=True,
            dangerous=True,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PUBLIC_ONLY,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        # --- Product / PM & Ops (mostly native/existing; writes deferred) ---
        _s(
            name="github_actions",
            teams=("ops", "pm"),
            kind="cli",
            hosted=True,
            credential_env=("GITHUB_TOKEN",),
            pricing="fixed_zero",
            sensitivity="med",
            egress=True,
            write=False,
            dangerous=False,
            stages=_RE,
            permission_default="ask",
            context_policy=ContextPolicy.PROJECT_NON_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
        _s(
            name="linear",
            teams=("pm",),
            kind="mcp",
            hosted=True,
            credential_env=("LINEAR_API_KEY",),
            pricing="metered",
            sensitivity="med",
            egress=True,
            write=True,
            dangerous=False,
            stages=_E,
            permission_default="ask",
            context_policy=ContextPolicy.PROJECT_NON_PRIVATE,
            output_trust=OutputTrust.UNTRUSTED_EXTERNAL_CONTENT,
            priority="later",
        ),
    )
}

_PRIORITIES = frozenset({"now", "later", "avoid"})
_KINDS = frozenset({"native", "cli", "mcp", "browser", "ritual"})


def get_spec(name: str) -> ServiceSpec | None:
    return SERVICE_CATALOG.get(name)
