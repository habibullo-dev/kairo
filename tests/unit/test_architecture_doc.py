"""Contract pins for the current Kira architecture map."""

import re

from jarvis import __version__
from jarvis.attention.builders import JOBS
from jarvis.attention.dreaming import DREAMING_TOOLS
from jarvis.attention.readmodel import attention_queue
from jarvis.cli.reset import CONFIRMATION_PHRASE
from jarvis.config import (
    ChatConfig,
    LoggingConfig,
    ProjectIntelligenceConfig,
    TelegramRemoteControlConfig,
    TelegramRemoteOperatorConfig,
)
from jarvis.models.providers import PROVIDER_CATALOG, TRUSTED_AUTHORITY_PROVIDERS
from jarvis.models.registry import ModelRegistry
from jarvis.models.roles import (
    DEFAULT_ROUTES,
    FINAL_AUTHORITY_ROLES,
    PRIVATE_CONTEXT_ROLES,
    TOOL_CAPABLE_ROLES,
)
from jarvis.observability.logging import CANONICAL_LOG_PREFIX, LEGACY_LOG_PREFIXES
from jarvis.permissions.approvals import NEVER_PERSIST
from jarvis.permissions.modes import Mode, ModeState
from jarvis.permissions.unattended import HARD_DENY
from jarvis.persistence import backup as backup_module
from jarvis.persistence.database_identity import DATABASE_FILENAME, LEGACY_DATABASE_FILENAME
from jarvis.persistence.migrations import latest_version
from jarvis.persistence.sessions import REFLECTABLE_KINDS
from jarvis.remote.operator import RemoteLiveSearchTool, RemoteProposalTool
from jarvis.routing.policy import ALL_TIERS, SAFE_DEFAULT, RoutingMode
from jarvis.routing.router import RoutingState
from jarvis.ui.owner_auth import (
    ABSOLUTE_SESSION_DAYS,
    AUTH_GRANT_MINUTES,
    IDLE_SESSION_DAYS,
    SESSION_TOUCH_HOURS,
    STEP_UP_MINUTES,
)
from jarvis.ui.server import STATIC_DIR
from jarvis.ui.state import ALLOWED_MODEL_IDS

REPOSITORY_ROOT = STATIC_DIR.parents[3]
ARCHITECTURE_PATH = REPOSITORY_ROOT / "docs" / "architecture.md"
ARCHITECTURE = ARCHITECTURE_PATH.read_text(encoding="utf-8")
NORMALIZED = " ".join(ARCHITECTURE.split())


def test_architecture_reports_the_current_kira_baseline() -> None:
    for claim in (
        "# Kira Architecture",
        f"Kira {__version__}",
        f"schema v{latest_version()}",
        "Phase 16 Tasks 1–9",
        "Checkpoint K",
        "not scheduled",
        f"data/{DATABASE_FILENAME}",
        f"logs/{CANONICAL_LOG_PREFIX}-YYYY-MM-DD.jsonl",
        "Chat-first",
        "Restore is not supported",
        "uv run kira doctor",
        "uv run kira eval gate",
        "deterministic keyless replay",
        "provider-call-free",
    ):
        assert claim in ARCHITECTURE

    for stale_brand in ("kairo", "cairo"):
        assert re.search(rf"\b{stale_brand}\b", ARCHITECTURE, flags=re.IGNORECASE) is None
    assert "Jarvis" not in ARCHITECTURE
    for stale_claim in (
        "uv run jarvis",
        "logs/jarvis",
        "schema baseline is v31",
        "every `ask` becomes",
        "~40-line",
        "a second connection would deadlock",
        "AttentionReadModel",
        "globally serialized",
        "Lab is view-only",
        "regenerates thresholds",
        "cross-revision deltas",
    ):
        assert stale_claim not in ARCHITECTURE


def test_architecture_keeps_deliberate_compatibility_identity_explicit() -> None:
    assert "lowercase `jarvis` import namespace" in NORMALIZED
    assert "`jarvis/paths.py`" in ARCHITECTURE
    assert f"data/{LEGACY_DATABASE_FILENAME}" in ARCHITECTURE
    assert "`jarvis-YYYY-MM-DD.jsonl`" in ARCHITECTURE
    assert "leave a small compatibility guard" in NORMALIZED
    assert len(re.findall(r"\bjarvis\b", ARCHITECTURE, flags=re.IGNORECASE)) == 4


def test_architecture_pins_current_authority_and_session_boundaries() -> None:
    for tool in NEVER_PERSIST:
        assert f"`{tool}`" in ARCHITECTURE
    for tool in HARD_DENY:
        assert f"`{tool}`" in ARCHITECTURE

    for claim in (
        "optional parking path instead stops before any tool in that assistant batch executes",
        "one-use resolution must claim the bound continuation",
        "resolution of an attention row grants no source authority",
        "single-use connection-bound nonce",
        "separate attended route approves and executes the stored payload",
        "permanently excludes child transcripts",
        "scheduler.reflect_job_sessions",
        "one aiosqlite connection and one asyncio write lock",
    ):
        assert claim in NORMALIZED

    assert {"interactive", "task"} == REFLECTABLE_KINDS


def test_architecture_pins_model_routing_and_budget_boundaries() -> None:
    registry = ModelRegistry({"reviewer": {"model": "settings", "effort": "low"}})
    route = registry.route(
        "reviewer",
        project_routes={"reviewer": {"model": "project"}},
        run_routes={"reviewer": {"model": "run"}},
    )
    assert (route.model, route.effort) == ("run", "low")
    assert set(DEFAULT_ROUTES) >= (
        FINAL_AUTHORITY_ROLES | PRIVATE_CONTEXT_ROLES | TOOL_CAPABLE_ROLES
    )
    assert {"planner", "judge"} == FINAL_AUTHORITY_ROLES
    assert {"utility"} == PRIVATE_CONTEXT_ROLES
    assert {"coder"} == TOOL_CAPABLE_ROLES
    assert {"anthropic"} == TRUSTED_AUTHORITY_PROVIDERS

    assert RoutingMode is not Mode
    assert {mode.value for mode in RoutingMode} == {"auto", "manual"}
    assert {mode.value for mode in Mode} == {"plan", "approval", "auto"}
    assert RoutingState().mode() is RoutingMode.AUTO
    assert ModeState().current() is Mode.APPROVAL
    assert {
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    } == ALLOWED_MODEL_IDS

    assert [(tier.key, tier.provider, tier.model) for tier in ALL_TIERS] == [
        ("simple", "gemini", "gemini-2.5-flash"),
        ("simple_tooled", "anthropic", "claude-haiku-4-5-20251001"),
        ("judgment", "anthropic", "claude-sonnet-5"),
        ("deep", "anthropic", "claude-opus-4-8"),
        ("planning", "anthropic", "claude-fable-5"),
    ]
    assert SAFE_DEFAULT.model == "claude-sonnet-5"
    assert all(PROVIDER_CATALOG[tier.provider].private_ok for tier in ALL_TIERS)
    assert {name for name, spec in PROVIDER_CATALOG.items() if spec.private_ok} == {
        "anthropic",
        "gemini",
        "openai",
    }

    chat = ChatConfig()
    assert (
        chat.max_iterations,
        chat.max_output_tokens,
        chat.hard_stop_usd_per_turn,
        chat.input_token_margin,
    ) == (8, 4096, 0.75, 512)
    for claim in (
        "built-in defaults ← settings ← project ← run",
        "Interactive routing is a separate axis from Gate permission mode",
        "Pricing is a different boundary",
        "refuses an unknown exact-model price or projected over-cap call before spending",
        "turning it off preserves the pre-feature request",
    ):
        assert claim in NORMALIZED


def test_architecture_pins_ui_auth_and_attention_truth() -> None:
    assert (AUTH_GRANT_MINUTES, IDLE_SESSION_DAYS, ABSOLUTE_SESSION_DAYS) == (10, 30, 90)
    assert (SESSION_TOUCH_HOURS, STEP_UP_MINUTES) == (24, 5)
    assert attention_queue.__name__ == "attention_queue"
    assert "`attention_queue(...)`" in ARCHITECTURE
    for claim in (
        "Unified Notifications deliberately renders both durable and current-session sources",
        "live Gate ASKs and the process-local `NoticeBoard` do not become restart history",
        "credential presence is boolean and credential values never cross the wire",
        "Lab cannot execute evals",
        "UI shutdown currently does not iterate them for reflection",
        "separately labeled process-bound recovery URL",
        "Overview/Chats/Artifacts/Memory/Tasks/Vault/Studio/Office/Graph/Costs/Activity",
    ):
        assert claim in NORMALIZED


def test_architecture_pins_remote_and_dreaming_limits() -> None:
    remote = TelegramRemoteControlConfig()
    operator = TelegramRemoteOperatorConfig()
    assert f"{remote.conversation_context_turns} delivered turns" in NORMALIZED
    assert f"{remote.conversation_context_max_chars:,} characters" in NORMALIZED
    assert f"{remote.reference_context_ttl_minutes}-minute reference slot" in NORMALIZED
    assert f"{len(DREAMING_TOOLS)}-tool" in ARCHITECTURE
    assert RemoteProposalTool.name == "remote_propose_work"
    assert RemoteLiveSearchTool.name == "remote_live_search"
    assert operator.enabled is False
    assert operator.approval_ttl_minutes == 15
    assert operator.proposal_ttl_minutes == 30
    assert operator.max_active_jobs == 3
    assert operator.live_web_search_max_results == 5
    assert operator.allowed_tools == [
        "read_file",
        "list_dir",
        "glob_search",
        "write_file",
        "run_shell",
    ]

    for visible_name in (
        "morning briefing",
        "nightly review",
        "bottleneck",
        "ROI summary",
        "self-improvement",
    ):
        assert visible_name in ARCHITECTURE
    assert set(JOBS) == {
        "morning_briefing",
        "nightly_review",
        "bottleneck",
        "roi_summary",
        "self_improvement",
    }
    assert "tools=[]" in ARCHITECTURE

    for claim in (
        "exactly one positive decimal private `allowed_chat_id`",
        "outbound notification chat id grants no inbound authority",
        "audits egress without logging the query",
        "never replays Telegram messages or work",
        "no more than three approved jobs may be active at once",
    ):
        assert claim in NORMALIZED


def test_architecture_pins_project_intelligence_persistence_and_logging() -> None:
    intelligence = ProjectIntelligenceConfig()
    assert intelligence.enabled is False
    assert intelligence.analyze_after_import is True
    assert intelligence.max_cost_usd == 5.0
    assert intelligence.max_attempts == 2
    for claim in (
        "importing project content alone never authorizes cloud fan-out",
        "(project_id, snapshot_hash, profile_version)",
        "startup runs its reconciliation only after the host's orchestration orphan sweep",
        "no shell, host-filesystem write, egress, or remediation authority",
        "verified same-inode interrupted publication",
        "snapshot failure blocks migration",
    ):
        assert claim in NORMALIZED

    assert backup_module._SCHEMA_VERSION == 2
    assert backup_module._FORMAT == "kira-backup"
    assert backup_module._APPLICATION == "Kira"
    assert backup_module._INCLUDED_DIRECTORIES == ("knowledge", "artifacts")
    assert backup_module._EXCLUDED_PATTERNS == (
        ".env",
        "data/connectors/**",
        "**/*token*",
        "**/*secret*",
        "**/*credential*",
        "logs/**",
    )
    assert CONFIRMATION_PHRASE in ARCHITECTURE
    for claim in (
        "only `evals/history.jsonl`",
        "it is not a signature or MAC",
        "archive remains private",
        "External knowledge or log roots require a second exact-path confirmation",
        "fresh owner step-up",
    ):
        assert claim in NORMALIZED

    logging = LoggingConfig()
    assert (logging.max_bytes, logging.backup_count, logging.retention_days) == (
        10 * 1024 * 1024,
        3,
        30,
    )
    assert LEGACY_LOG_PREFIXES == ("jarvis",)
    for claim in (
        "10 MiB active segment, up to three gzip archives per day, and 30-day retention",
        "Mapping `tool_call.input` is reduced to `{redacted, keys, key_count}`",
        "Model and service ledgers",
        "unknown or unpriced work remains explicitly unknown rather than becoming $0",
    ):
        assert claim in NORMALIZED


def test_architecture_pins_eval_command_semantics() -> None:
    assert "finite positive `--max-cost-usd`" in ARCHITECTURE
    assert "Failed calibration marks judge scoring JUDGE-INVALID" in NORMALIZED
    assert "deterministic checks still decide the gate" in NORMALIZED
    assert "matching local history" in NORMALIZED
    assert "does not rewrite thresholds" in NORMALIZED
    command = (
        "uv run kira eval gate --suite core --scenario permission_denied --runs 1 "
        "--no-judge --live --max-cost-usd 1.00"
    )
    assert command in NORMALIZED
    assert (REPOSITORY_ROOT / "tests" / "evals" / "scenarios" / "permission_denied.yaml").is_file()


def test_architecture_local_markdown_links_resolve() -> None:
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", ARCHITECTURE)
    assert targets
    for raw_target in targets:
        target = raw_target.split("#", 1)[0]
        if not target or "://" in target:
            continue
        resolved = (ARCHITECTURE_PATH.parent / target).resolve()
        assert resolved.exists(), f"broken architecture link: {raw_target}"
