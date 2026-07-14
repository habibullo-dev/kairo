"""Workstation screenshot definition-of-done (Phase 15.5 Task 9) — a standalone dev tool, NOT a
pytest test (its name doesn't match ``test_*``), like ``office_dod.py`` / ``graph_dod.py``.

Unlike the per-screen DoDs, this boots the WHOLE shell: it serves a COPY of the static dir + a
harness that stubs ``fetch`` (returns per-state seeded JSON) and ``WebSocket`` (no server socket),
sets the theme + hash, and imports the REAL ``app.js`` — so the rail, status bar, conversation
header, Daily hero + dashboard, palette, Hub, graph, and voice controls all render exactly as
shipped. Then ``analyze_overlap`` (no element past the viewport, no horizontal scroll) across
9 states × noir/light/neon × 1440/1024/390 = 81 shots.

Usage (after ``uv sync --extra browser`` + ``uv run playwright install chromium``)::

    uv run python tests/ui/workbench_dod.py

Exits non-zero on any layout violation; PNGs land under ``data/screenshots/workbench`` (gitignored).
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

from jarvis.ui.screenshots import (
    OVERLAP_PROBE_JS,
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)
from jarvis.ui.server import STATIC_DIR

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "screenshots" / "workbench"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Keep the visual DoD progress readable; each shot otherwise logs several asset requests."""

    def log_message(self, _format: str, *_args: object) -> None:
        return


_MODELS = {
    "current": "claude-opus-4-8",
    "models": [
        {
            "id": "claude-fable-5",
            "label": "Fable 5",
            "provider": "anthropic",
            "selectable": True,
            "current": False,
            "reason": "",
        },
        {
            "id": "claude-opus-4-8",
            "label": "Opus 4.8",
            "provider": "anthropic",
            "selectable": True,
            "current": True,
            "reason": "",
        },
        {
            "id": "claude-sonnet-5",
            "label": "Sonnet 5",
            "provider": "anthropic",
            "selectable": True,
            "current": False,
            "reason": "",
        },
    ],
    "external": [
        {
            "id": "openai",
            "label": "gpt-5.2",
            "provider": "openai",
            "selectable": False,
            "current": False,
            "state": "missing_credentials",
            "reason": "receives your private conversation context — not enabled for the main chat "
            "(missing_credentials)",
        },
        {
            "id": "gemini",
            "label": "gemini-3.5-flash",
            "provider": "gemini",
            "selectable": False,
            "current": False,
            "state": "disabled",
            "reason": "not enabled for the main chat",
        },
    ],
}
_CAPS = {
    "connectors": [
        {"name": "Google Calendar", "state": "connected", "exposed_to_chat": True, "reason": ""},
        {"name": "Gmail", "state": "connected", "exposed_to_chat": True, "reason": ""},
        {
            "name": "Google Drive",
            "state": "needs_reconnect",
            "exposed_to_chat": False,
            "reason": "Google sign-in expired — reconnect in the Hub.",
        },
        {
            "name": "Telegram",
            "state": "connected",
            "exposed_to_chat": False,
            "reason": "Delivers notifications; not a chat tool.",
        },
        {
            "name": "Kakao",
            "state": "not_configured",
            "exposed_to_chat": False,
            "reason": "Add Kakao in settings to receive notifications.",
        },
    ],
    "providers": [
        {"name": "Anthropic", "state": "available", "exposed_to_chat": True, "reason": ""},
        {
            "name": "openai",
            "state": "missing_credentials",
            "exposed_to_chat": False,
            "reason": "Not enabled for the main chat (would receive private context).",
        },
    ],
    "services": [
        {"name": "firecrawl", "state": "available", "exposed_to_chat": True, "reason": ""},
        {
            "name": "exa",
            "state": "disabled",
            "exposed_to_chat": False,
            "reason": "Service disabled.",
        },
    ],
    "voice": {"state": "off", "exposed_to_chat": False, "reason": "Voice is off — enable it."},
    "mcp": {"state": "not_configured", "exposed_to_chat": False, "reason": "No MCP client yet."},
    "summary": "Google Calendar, Gmail · 1 service · voice off",
}
_DIGEST = {
    "summary": "3 events today, 5 unread emails, 2 tasks due.",
    "sections": [
        {"title": "Schedule", "status": "ok", "items": [1, 2, 3]},
        {"title": "Email", "status": "ok", "items": [1, 2, 3, 4, 5]},
    ],
    "suggested_actions": ["Reply to the design thread", "Confirm the 3pm meeting"],
}
_ARTIFACTS = [
    {
        "id": 1,
        "title": "Security review report",
        "kind": "orchestration",
        "pinned": True,
        "has_content": False,
        "created_at": "2026-07-08T10:00:00+00:00",
    },
    {
        "id": 2,
        "title": "Weekly digest",
        "kind": "digest",
        "has_content": True,
        "created_at": "2026-07-09T08:00:00+00:00",
    },
]
_RUN = {
    "id": 1,
    "title": "Security · review",
    "workflow": "security_review",
    "team": "security",
    "status": "ok",
    "actual_cost_usd": 0.42,
    "finished_at": "2026-07-08T10:00:00+00:00",
    "action_items": [
        {
            "title": "Add a recovery check",
            "goal": "Cover the stale approval path before release.",
            "priority": "high",
        },
        {
            "title": "Verify mobile layout",
            "goal": "Re-run the narrow visual harness after the change.",
            "priority": "medium",
        },
    ],
}
_RUN_DETAIL = {
    "run": {
        **_RUN,
        "verdict": "accept",
        "estimated_cost_usd": 0.55,
        "budget_usd": 2.0,
        "synthesis_summary": (
            "The team found two recoverable quality issues and no release blocker."
        ),
        "synthesis_findings": [
            {"member": "qa_lead", "title": "QA Lead", "finding": "Regression checks passed."},
            {
                "member": "eval_reader",
                "title": "Eval Reader",
                "finding": "Evaluation freshness is current.",
            },
            {
                "member": "ui_tester",
                "title": "UI Tester",
                "finding": "No visual overlap detected.",
            },
        ],
        "action_items": [
            {
                "title": "Add a recovery check",
                "goal": "Cover the stale approval path before release.",
                "priority": "high",
            },
            {
                "title": "Verify mobile layout",
                "goal": "Re-run the narrow visual harness after the change.",
                "priority": "medium",
            },
        ],
        "verdict_rationale": "The scoped QA evidence supports acceptance.",
        "context_manifest": [],
    },
    "members": [
        {
            "title": "qa:qa_lead",
            "role": "qa",
            "stage": "council",
            "status": "ok",
            "iterations": 2,
            "denied_count": 0,
            "cost_usd": 0.1,
            "models": ["anthropic · claude-sonnet-5"],
        },
        {
            "title": "qa:eval_reader",
            "role": "utility",
            "stage": "council",
            "status": "ok",
            "iterations": 2,
            "denied_count": 0,
            "cost_usd": 0.06,
            "models": ["anthropic · claude-haiku-4-5"],
        },
        {
            "title": "qa:ui_tester",
            "role": "qa",
            "stage": "council",
            "status": "ok",
            "iterations": 2,
            "denied_count": 0,
            "cost_usd": 0.1,
            "models": ["anthropic · claude-sonnet-5"],
        },
    ],
}
_PROJECT = {
    "id": 1,
    "name": "Kairo",
    "slug": "kairo",
    "description": "Local-first AI workstation",
    "icon": "K",
    "color": "#7cc4ff",
    "status": "active",
    "pinned": True,
}
_PROJECT_OVERVIEW = {
    "projects": [
        {
            **_PROJECT,
            "label": "Coding",
            "health": {
                "open_tasks": 3,
                "sessions_week": 5,
                "month_spend_usd": 2.4,
                "last_run": {"status": "ok", "verdict": "PASS"},
            },
        }
    ],
    "archived": [],
    "active_project_id": 1,
}
_WORKSPACE = {
    "project": _PROJECT,
    "health": {"month_spend_usd": 2.4},
    "recent_artifacts": _ARTIFACTS,
    "recent_runs": [_RUN],
}
_STUDIO = {
    "active_project_id": 1,
    "teams": [
        {
            "id": "security",
            "name": "Security",
            "icon": "◈",
            "description": "Review changes and risk.",
            "default_workflows": ["security_review"],
            "members": [
                {
                    "id": "sec_lead",
                    "title": "Security Lead",
                    "route_role": "reviewer",
                    "capability": "read_only",
                    "tools": ["search"],
                    "services": [],
                }
            ],
        }
    ],
    "workflows": [{"id": "security_review", "title": "Security review", "teams": ["security"]}],
    "model_routes": [
        {"role": "planner", "model": "claude-fable-5", "provider": "anthropic"},
        {"role": "reviewer", "model": "claude-sonnet-5", "provider": "anthropic"},
    ],
    "services": [],
}
_COSTS = {
    "today": {"cost_usd": 0.42, "calls": 4},
    "week": {"cost_usd": 1.2, "calls": 14},
    "month": {"cost_usd": 2.4, "calls": 28},
    "limits": {
        "soft_warn_usd_per_run": 1,
        "hard_stop_usd_per_run": 5,
        "confirm_above_usd": 2,
        "project_monthly_usd": 25,
    },
    "budget_warning": {"cap_usd": 25, "month_spend_usd": 2.4, "level": "ok"},
    "by_project": [{"project": "Kairo", "cost_usd": 2.4, "calls": 28}],
    "by_model": [{"model": "claude-sonnet-5", "cost_usd": 2.4, "calls": 28}],
    "by_provider": [],
    "by_team": [],
    "by_role": [],
    "by_stage": [],
    "by_purpose": [],
    "by_service": [],
}
_MARKDOWN_MESSAGE = """## Release checklist

Here is the **focused** release checklist:

- Verify the Gate flow
- Run the focused tests

> Keep approval on screen.

```sh
uv run pytest tests/unit -q
```

Read the [release notes](https://example.com/release-notes)."""


def _base() -> dict:
    return {
        "_hash": "chat",
        "/api/runner": {
            "runner_available": True,
            "runner_running": True,
            "turn_busy": False,
            "global_turn_busy": False,
            "background_busy": False,
            "mode": "approval",
            "project": None,
            "today_spend_usd": 0.42,
            "ledger_degraded": False,
            "pending_approvals": 0,
            "session_id": None,
            "context_revision": 1,
            "session_title": None,
            "model": "claude-opus-4-8",
            "effort": "high",
        },
        "/api/voice/status": {
            "enabled": False,
            "listening": "idle",
            "meeting": "idle",
            "meeting_recording": False,
            "meeting_recording_epoch": "workbench-process",
            "meeting_revision": 0,
            "meeting_recording_revision": 0,
            "meeting_available": False,
            "meeting_reason": "Voice and Knowledge are off.",
            "reason": "Voice is off.",
            "stt": "local",
            "tts": "local",
            "playback": False,
        },
        "/api/notices": {"notices": []},
        "/api/tasks": [],
        "/api/models": _MODELS,
        "/api/capabilities": _CAPS,
        "/api/projects": {
            "projects": [{"id": 1, "name": "Kairo"}, {"id": 2, "name": "Website"}],
            "active_project_id": None,
        },
        "/api/projects/overview": _PROJECT_OVERVIEW,
        "/api/workspace/1": _WORKSPACE,
        "/api/studio": _STUDIO,
        "/api/orchestration": {"runs": [_RUN]},
        "/api/orchestration/1": _RUN_DETAIL,
        "/api/costs": _COSTS,
        "/api/roi": {"roi": [], "hourly_rate_usd": 75},
        "/api/settings": {},
        "/api/sessions": {
            "sessions": [
                {
                    "id": 5,
                    "title": "Design review",
                    "updated_at": "2026-07-09T09:00:00+00:00",
                    "pinned": False,
                },
                {
                    "id": 4,
                    "title": "Debugging the parser",
                    "updated_at": "2026-07-08T14:00:00+00:00",
                    "pinned": True,
                },
            ]
        },
        "/api/chat/knowledge": {
            "project_id": None,
            "source_count": 0,
            "sources": [],
            "graph": {"available": False, "nodes": [], "edge_count": 0, "truncated": False},
        },
        "/api/daily": {
            "digest": _DIGEST,
            "recent_artifacts": _ARTIFACTS,
            "latest_run": _RUN,
            "repos": [],
            "evals": {"ever_run": True, "stale": False, "verdict": "PASS"},
            "kb_review_count": 0,
            "demo": False,
            "capabilities": _CAPS,
        },
        "/api/hub": {
            "providers": {"anthropic": True, "voyage": True, "openai": False},
            "egress": {"audio_bytes": 0, "text_chars": 0},
            "capabilities": _CAPS,
            "mcp": {"connected": False, "note": "not connected — future phase"},
        },
        "/api/graph/search": {"results": []},
        "/api/vault": {
            "stats": {"sources": 4, "chunks": 12, "unreviewed": 0},
            "unreviewed": [],
            "project_id": None,
            "project_readiness": None,
        },
        "_default": {},
    }


def _seed_for(state: str) -> dict:
    s = _base()
    r = s["/api/runner"]
    if state == "daily-populated":
        s["_hash"] = "daily"
    elif state == "daily-empty":
        s["_hash"] = "daily"
        s["/api/daily"] = {
            "digest": None,
            "recent_artifacts": [],
            "latest_run": None,
            "repos": [],
            "evals": {"ever_run": False},
            "demo": False,
            "capabilities": _CAPS,
        }
        s["/api/sessions"] = {"sessions": []}
    elif state == "chat-fresh":
        s["_hash"] = "chat"
    elif state == "projects":
        s["_hash"] = "projects"
        r["project"] = {"id": 1, "name": "Kairo"}
    elif state == "workspace-overview":
        s["_hash"] = "workspace/1"
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
    elif state == "workspace-tasks":
        s["_hash"] = "workspace/1/tasks"
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
    elif state == "workspace-vault":
        s["_hash"] = "workspace/1/vault"
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["/api/vault"] = {
            "stats": {"sources": 4, "chunks": 12, "unreviewed": 0},
            "unreviewed": [],
            "project_id": 1,
            "project_readiness": {
                "project_id": 1,
                "sources": 4,
                "indexed_chunks": 12,
                "graph_available": True,
                "folder_links": 7,
                "import_links": 3,
                "ready": True,
                "detail": (
                    "Relevant sections and verified local dependencies are available "
                    "to project chat."
                ),
            },
        }
        s["/api/chat/knowledge"] = {
            "project_id": 1,
            "source_count": 4,
            "sources": [
                {
                    "id": 12,
                    "title": "src/app.py",
                    "kind": "file",
                    "mime": "text/plain",
                    "byte_size": 2048,
                    "review_status": "reviewed",
                    "created_at": "2026-07-09T09:00:00+00:00",
                },
                {
                    "id": 11,
                    "title": "src/core.py",
                    "kind": "file",
                    "mime": "text/plain",
                    "byte_size": 1890,
                    "review_status": "reviewed",
                    "created_at": "2026-07-09T08:00:00+00:00",
                },
            ],
            "graph": {"available": True, "nodes": [], "edge_count": 9, "truncated": False},
        }
    elif state == "studio":
        s["_hash"] = "studio"
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
    elif state == "studio-result":
        s["_hash"] = "studio/1"
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
    elif state == "costs":
        s["_hash"] = "costs"
        r["project"] = {"id": 1, "name": "Kairo"}
    elif state == "settings":
        s["_hash"] = "settings"
    elif state == "meetings":
        s["_hash"] = "meetings"
        r["session_id"] = 5
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["/api/voice/status"] = {
            "enabled": True,
            "listening": "idle",
            "meeting": "idle",
            "meeting_recording": False,
            "meeting_recording_epoch": "workbench-process",
            "meeting_revision": 0,
            "meeting_recording_revision": 0,
            "meeting_available": True,
            "meeting_reason": "",
            "reason": "",
            "stt": "local",
            "tts": "local",
            "playback": False,
        }
    elif state == "chat-project":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/sessions/5"] = {
            "messages": [
                {"role": "user", "text": "Summarize the security review findings."},
                {
                    "role": "assistant",
                    "text": "Three findings: a hardcoded token, TLS verification "
                    "disabled, and a credential-shaped literal. Details on "
                    "screen; none exfiltrated.",
                },
            ]
        }
    elif state == "chat-markdown":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Release checklist"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/sessions/5"] = {
            "messages": [
                {"role": "user", "text": "What should ship with the release?"},
                {"role": "assistant", "text": _MARKDOWN_MESSAGE},
            ]
        }
    elif state == "chat-history":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["_trigger"] = "history"
    elif state == "chat-files":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/chat/files"] = {
            "files": [
                {
                    "id": 10,
                    "title": "Architecture brief.pdf",
                    "kind": "file",
                    "mime": "application/pdf",
                    "byte_size": 2048,
                    "review_status": "reviewed",
                    "created_at": "2026-07-09T09:00:00+00:00",
                },
            ]
        }
        s["_trigger"] = "files"
    elif state == "chat-outputs":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/chat/outputs"] = {
            "artifacts": [
                {
                    "id": 7,
                    "title": "Release report.pdf",
                    "kind": "report",
                    "has_content": True,
                    "created_at": "2026-07-09T09:00:00+00:00",
                },
            ]
        }
        s["_trigger"] = "outputs"
    elif state == "chat-knowledge":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/chat/knowledge"] = {
            "project_id": 1,
            "source_count": 3,
            "sources": [
                {
                    "id": 12,
                    "title": "Architecture brief.pdf",
                    "kind": "file",
                    "mime": "application/pdf",
                    "byte_size": 2048,
                    "review_status": "reviewed",
                    "created_at": "2026-07-09T09:00:00+00:00",
                },
                {
                    "id": 11,
                    "title": "src/routing.ts",
                    "kind": "file",
                    "mime": "text/plain",
                    "byte_size": 1890,
                    "review_status": "reviewed",
                    "created_at": "2026-07-09T08:00:00+00:00",
                },
            ],
            "graph": {
                "available": True,
                "edge_count": 4,
                "truncated": False,
                "nodes": [
                    {
                        "id": "project:1",
                        "kind": "project",
                        "label": "Kairo",
                        "degree": 4,
                        "trust_class": "trusted_local",
                    },
                    {
                        "id": "source:12",
                        "kind": "source",
                        "label": "Architecture brief.pdf",
                        "degree": 1,
                        "trust_class": "trusted_local",
                    },
                    {
                        "id": "source:11",
                        "kind": "source",
                        "label": "src/routing.ts",
                        "degree": 1,
                        "trust_class": "trusted_local",
                    },
                    {
                        "id": "memory:4",
                        "kind": "memory",
                        "label": "Routing decision",
                        "degree": 1,
                        "trust_class": "trusted_local",
                    },
                ],
            },
        }
        s["_trigger"] = "knowledge"
    elif state == "chat-approval":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Terminal task"
        s["/api/projects"]["active_project_id"] = 1
        s["_trigger"] = "approval"
    elif state == "model-selector":
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["_trigger"] = "model"
    elif state == "palette":
        s["_trigger"] = "palette"
    elif state == "hub-truth":
        s["_hash"] = "hub"
    elif state == "graph-discovery":
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["_hash"] = "workspace/1/graph"
        s["/api/workspace/1/graph"] = {
            "nodes": [],
            "edges": [],
            "counts": {"by_kind": {}},
            "focus": "project:1",
            "project_id": 1,
        }
        s["/api/workspace/1"] = {"project": {"id": 1, "name": "Kairo"}}
    elif state == "voice":
        s["/api/voice/status"] = {
            "enabled": True,
            "listening": "idle",
            "meeting": "idle",
            "meeting_recording": False,
            "meeting_recording_epoch": "workbench-process",
            "meeting_revision": 0,
            "meeting_recording_revision": 0,
            "reason": "",
            "stt": "openai",
            "tts": "openai",
            "playback": True,
        }
        s["/api/capabilities"] = {
            **_CAPS,
            "voice": {"state": "on", "exposed_to_chat": True, "reason": ""},
        }
    return s


STATES = [
    "daily-empty",
    "daily-populated",
    "chat-fresh",
    "chat-project",
    "chat-markdown",
    "chat-history",
    "chat-files",
    "chat-outputs",
    "chat-knowledge",
    "chat-approval",
    "projects",
    "workspace-overview",
    "workspace-tasks",
    "workspace-vault",
    "studio",
    "studio-result",
    "costs",
    "settings",
    "meetings",
    "model-selector",
    "palette",
    "hub-truth",
    "graph-discovery",
    "voice",
]

HARNESS = """<!doctype html><html lang="en" data-theme="noir" data-density="comfortable"
data-layout="focused"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/kairo.css"></head>
<body>%BODY%
<script>
(function () {
  var q = new URLSearchParams(location.search);
  var th = q.get('theme') || 'noir';
  try { localStorage.setItem('kairo:appearance', JSON.stringify({ theme: th })); } catch (e) {}
  window.WebSocket = function () {
    var socket = {
      readyState: 3, send: function () {}, close: function () {}, addEventListener: function () {},
      set onopen(f) { this._onopen = f; }, set onmessage(f) { this._onmessage = f; },
      set onclose(f) { this._onclose = f; }, set onerror(f) { this._onerror = f; }
    };
    window.__WB_SOCKET__ = socket;
    return socket;
  };
  var real = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    var u = (typeof url === 'string') ? url : url.url;
    if (u.indexOf('__wb_') !== -1 || u.indexOf('/static/') !== -1) return real(url, opts);
    var seed = window.__SEED__ || {};
    var path = u.split('?')[0].replace(location.origin, '');
    var body = (path in seed) ? seed[path] : ('_default' in seed ? seed['_default'] : {});
    return Promise.resolve(new Response(JSON.stringify(body),
      { status: 200, headers: { 'content-type': 'application/json' } }));
  };
})();
</script>
<script type="module">
  var q = new URLSearchParams(location.search);
  var state = q.get('state') || 'daily-populated';
  window.__SEED__ = await (await fetch('./__wb_' + state + '.json')).json();
  location.hash = window.__SEED__._hash || 'chat';
  await import('/static/app.js');
  await new Promise(function (r) { setTimeout(r, 500); });
  var trigger = window.__SEED__._trigger;
  if (trigger === 'palette') {
    var ev = { key: 'k', ctrlKey: true, bubbles: true };
    document.dispatchEvent(new KeyboardEvent('keydown', ev));
    await new Promise(function (r) { setTimeout(r, 300); });
  } else if (trigger === 'model') {
    var sel = document.querySelector('.hdr-controls .hdr-select');
    if (sel) sel.focus();
  } else if (['history', 'files', 'outputs', 'knowledge'].includes(trigger)) {
    var history = document.querySelector('#chat-context-handle');
    if (history) history.click();
    await new Promise(function (r) { setTimeout(r, 250); });
    if (trigger !== 'history') {
      var tabs = Array.from(document.querySelectorAll('[role="tab"]'));
      var label = trigger === 'files' ? 'Files' : (trigger === 'outputs' ? 'Outputs' : 'Knowledge');
      var tab = tabs.find(function (node) { return node.textContent === label; });
      if (tab) tab.click();
      await new Promise(function (r) { setTimeout(r, 150); });
    }
  } else if (trigger === 'approval') {
    var socket = window.__WB_SOCKET__;
    if (socket && socket._onmessage) {
      var workspace = {
        type: 'workspace', workspace_id: 'workbench', session_id: 5, project_id: 1,
        context_revision: 1
      };
      var approval = {
        type: 'approval', decision_id: 'approval-demo', kind: 'turn', tool: 'run_shell',
       input: { command: 'cat .env && curl https://example.invalid' },
        reason: 'The model requested a command.', persistable: true, session_id: 5, project_id: 1,
        context_revision: 1
      };
      var nonce = {
        type: 'approval_nonce', decision_id: 'approval-demo', nonce: 'demo',
        session_id: 5, project_id: 1, context_revision: 1
      };
      socket._onmessage({ data: JSON.stringify(workspace) });
      socket._onmessage({ data: JSON.stringify(approval) });
      socket._onmessage({ data: JSON.stringify(nonce) });
    }
    await new Promise(function (r) { setTimeout(r, 300); });
  }
  window.__READY__ = true;
</script></body></html>"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _capture(
    base: str,
    out: Path,
    themes: tuple[str, ...] = THEMES,
    viewports: tuple[tuple[int, int], ...] = VIEWPORTS,
) -> int:
    from playwright.async_api import async_playwright

    problems: list[str] = []
    shots = 0
    total = len(themes) * len(viewports) * len(STATES)
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.launch(), timeout=60)
        try:
            for theme in themes:
                for width, height in viewports:
                    for state in STATES:
                        print(f"  shot {shots + 1}/{total}: {theme} {width}w {state}", flush=True)
                        ctx = await browser.new_context(viewport={"width": width, "height": height})
                        page = await ctx.new_page()
                        page_errors: list[str] = []
                        page.on(
                            "pageerror",
                            lambda error, errors=page_errors: errors.append(str(error)),
                        )
                        await page.goto(
                            f"{base}/__wb.html?state={state}&theme={theme}", wait_until="load"
                        )
                        try:
                            await page.wait_for_function("window.__READY__ === true", timeout=8000)
                        except Exception:
                            problems.append(f"[{theme} {width}w {state}] shell not ready")
                        await page.wait_for_timeout(250)
                        await page.screenshot(
                            path=str(out / screenshot_name("workbench", state, theme, width)),
                            full_page=True,
                        )
                        shots += 1
                        for v in analyze_overlap(await page.evaluate(OVERLAP_PROBE_JS)):
                            problems.append(f"[{theme} {width}w {state}] {v}")
                        for error in page_errors:
                            problems.append(f"[{theme} {width}w {state}] page error: {error}")
                        await ctx.close()
        finally:
            await browser.close()
    print(f"\ncaptured {shots} workbench shots -> {out}")
    if problems:
        print(f"{len(problems)} layout violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(
        f"GREEN: no layout violations across states x themes x viewports "
        f"({len(STATES)} x {len(themes)} x {len(viewports)})"
    )
    return 0


async def main() -> int:
    raw_args = tuple(sys.argv[1:])
    width_args = tuple(
        arg.removeprefix("--width=") for arg in raw_args if arg.startswith("--width=")
    )
    requested = tuple(arg for arg in raw_args if not arg.startswith("--width="))
    unknown = set(requested).difference(THEMES)
    if unknown:
        raise SystemExit(f"unknown theme(s): {', '.join(sorted(unknown))}")
    known_widths = {str(width) for width, _height in VIEWPORTS}
    invalid_widths = set(width_args).difference(known_widths)
    if invalid_widths:
        raise SystemExit(f"unknown viewport width(s): {', '.join(sorted(invalid_widths))}")
    themes = requested or THEMES
    viewports = tuple(v for v in VIEWPORTS if not width_args or str(v[0]) in width_args)
    work = Path(tempfile.mkdtemp(prefix="workbench-dod-"))
    try:
        static = work / "static"
        shutil.copytree(STATIC_DIR, static / "static")  # served under /static (absolute refs)
        # Body of index.html (rail/status/main/overlay) — reused verbatim so the shell is real.
        # (index.html's header comment itself contains the text "<body>", so split on the LAST
        # occurrence — the real opening tag — not the first.)
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        body = index.split("<body>")[-1].rsplit("<script", 1)[0]
        (static / "__wb.html").write_text(HARNESS.replace("%BODY%", body), encoding="utf-8")
        for st in STATES:
            (static / f"__wb_{st}.json").write_text(json.dumps(_seed_for(st)), encoding="utf-8")
        port = _free_port()
        handler = functools.partial(_QuietHandler, directory=str(static))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            return await _capture(f"http://127.0.0.1:{port}", OUT, themes, viewports)
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main()))
