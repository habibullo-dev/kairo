"""Slice 1: the primary chat route remains a thin UI over the existing attended turn path."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
CONVERSATION = (STATIC_DIR / "screens" / "conversation.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")


def test_chat_is_the_default_primary_route_with_a_clear_nav_entry() -> None:
    assert 'href="#chat" data-screen="chat"' in HTML
    assert 'href="#trace" data-screen="trace"' in HTML and "debug-only" in HTML
    assert 'href="#lab" data-screen="lab"' in HTML and "debug-only" in HTML
    assert 'route: "chat"' in APP
    assert 'name: parts[0] || "chat"' in APP
    assert "chat: renderChat" in APP
    assert ".rail .debug-only { display: none; }" in CSS


def test_chat_reuses_existing_turn_and_conversation_state() -> None:
    assert 'api.post("/api/turn"' in CONVERSATION
    assert "onConversationEvent(state, evt)" in APP
    assert 'from "./conversation.js"' in CHAT
    assert 'from "./conversation.js"' not in DAILY
    assert "/api/approvals" not in CHAT
    assert "/api/model" not in CHAT


def test_cancelled_streaming_drafts_do_not_survive_in_the_live_chat() -> None:
    # A partial provider stream has no completed model response to persist; the browser removes it
    # before adding the durable `(stopped)` marker. Tool-start/deny events settle valid text first.
    assert (
        'state.chat = state.chat.filter((item) => item.role !== "assistant" || !item.live)' in APP
    )
    assert "function settleLiveAssistantDrafts(state)" in CONVERSATION
    assert CONVERSATION.count("settleLiveAssistantDrafts(state);") >= 3


def test_failed_turn_copy_is_redacted_and_matches_the_durable_marker() -> None:
    assert '"(unable to complete this turn)"' in APP
    error_start = APP.index('if (msg.kind === "turn_cancelled"')
    error_end = APP.index("// The server already")
    error_handler = APP[error_start:error_end]
    assert "msg.error" not in error_handler
    assert "state.chat = state.chat.filter" in error_handler


def test_chat_has_readable_full_height_composer_and_context_controls() -> None:
    for token in (
        'id="chat-input"',
        'class="chat-send"',
        'id="chat-convo-header"',
        "chat-composer-toolbar",
        'id="chat-pending"',
        'id="chat-attach"',
        'id="chat-file-input"',
        "chat-attachments",
        'id="chat-mic"',
        'id="chat-voice-cancel"',
        'id="chat-turn-cancel"',
    ):
        assert token in CHAT
    assert "chat-intro" not in CHAT
    for token in (".screen.chat-screen", ".chat-shell", ".chat-thread", ".chat-composer"):
        assert token in CSS
    assert ".chat-composer .convo-header" in CSS
    # app.js resets route classes for every render; Chat restores its own class before the
    # construction guard so a streamed response cannot collapse the primary layout.
    layout_class = 'container.classList.add("chat-screen")'
    construction_guard = 'if (!container.querySelector("#chat-input"))'
    assert CHAT.index(layout_class) < CHAT.index(construction_guard)
    assert 'event.key === "Enter" && !event.shiftKey' in CHAT
    assert "Send <span" not in CHAT


def test_chat_stop_cancels_only_the_current_turn() -> None:
    assert 'api.post("/api/turn/cancel", {' in CHAT
    assert "expected_context: expectedContext" in CHAT
    assert "turn_id: turnId" in CHAT
    assert "Background jobs keep running." in CHAT
    assert 'api.post("/api/runner/pause"' not in CHAT
    assert ".chat-turn-cancel" in CSS


def test_chat_stop_recovers_when_cancel_races_or_the_network_fails() -> None:
    # A 200 response is not itself a successful cancellation: the server returns
    # {cancelled:false} when the attended turn completed in the request race. Both that response
    # and a rejected fetch must restore a usable control after a fresh status read.
    assert "result.ok && result.data.cancelled" in CHAT
    assert "api.runnerStatus({ refresh: true })" in CHAT
    assert "if (runner && !runner.turn_busy) api.state.turnCancelling = false;" in CHAT
    assert "This turn has already finished." in CHAT
    assert "Kairo couldn't stop this turn. Please try again." in CHAT


def test_chat_message_rendering_remains_text_only() -> None:
    assert "document.createTextNode" in CONVERSATION
    assert "code.textContent" in CONVERSATION
    assert "innerHTML" not in CONVERSATION


def test_chat_shows_safe_subagent_progress_without_child_payloads() -> None:
    # Delegated work is visible in the attended conversation, but tool input, previews, and
    # streamed child text never become a second untrusted content channel.
    for token in (
        'evt.type === "subagent_event"',
        'evt.type === "subagent_completed"',
        "addSubagentActivity",
        "addSubagentCompletion",
        "subagent-line",
        "drafting a response",
        "compactLabel",
    ):
        assert token in CONVERSATION
    assert "inner.input" not in CONVERSATION
    assert "inner.preview" not in CONVERSATION
    assert "line.textContent" in CONVERSATION
    assert "settleLiveAssistantDrafts(state);" in CONVERSATION


def test_chat_rehydrates_recorded_delegation_summaries_without_child_bodies() -> None:
    assert "function hydrateTranscript(transcript)" in APP
    assert "transcript.delegations" in APP
    assert "recorded delegated work" in APP
    assert "child event timeline" in APP
    hydration = APP[APP.index("function hydrateTranscript") : APP.index("// --- WebSocket")]
    assert "prompt" not in hydration
    assert "result_text" not in hydration


def test_idle_global_chrome_collapses_without_removing_active_controls() -> None:
    assert "facts.workActive || facts.paused" in APP
    assert 'status.classList.toggle("is-working", facts.workActive || controlling)' in APP
    assert 'status.classList.toggle("has-global-work", facts.workActive' in APP
    assert ".status { grid-area: status; display: none; }" in CSS
    assert ".status.status-active" in CSS
    assert ".status.is-paused #st-resume" in CSS


def test_global_runner_control_is_truthful_shared_and_reconciled() -> None:
    assert ">Stop all</button>" in HTML
    assert "Stop all chats and pause schedules" in HTML
    assert ">Resume schedules</button>" in HTML
    assert "Stopped chats stay stopped." in HTML
    assert 'id="ap-stop-all"' in HTML
    assert 'id="runner-control-feedback" role="status" aria-live="polite"' in HTML
    for token in (
        "runner_available",
        "global_turn_busy",
        "background_busy",
        "runnerControlOperation",
        'document.querySelectorAll("[data-runner-control]")',
        "RUNNER_CONTROL_RECONCILE_TIMEOUT_MS",
        "timeoutMs: RUNNER_CONTROL_RECONCILE_TIMEOUT_MS",
        'msg.kind === "runner_state"',
        "refreshRunnerStatus({ refreshChatHeader: true })",
        "clearTurnPendingApprovals();",
    ):
        assert token in APP
    # A missing scheduler cannot produce Resume, but process-wide live chat work remains stoppable.
    assert (
        "pauseAvailable: workActive || turnApprovalPending || "
        "(statusCurrent && runnerAvailable)" in APP
    )
    assert "resumeVisible = operation ? resuming : (facts.resumeAvailable && facts.paused)" in APP
    assert 'const TURN_APPROVAL_KIND = "turn"' in APP
    assert 'const SUBAGENT_APPROVAL_KIND = "subagent"' in APP
    assert "turnDecisionIds: turnPendingDecisionIds()" in APP
    assert "subagentDecisionIds: subagentPendingDecisionIds()" in APP
    assert 'snapshot = await api.get("/api/approvals", { signal: controller.signal })' in APP
    assert 'else if (!facts.statusCurrent) runnerText = "Runner status is unavailable"' in APP
    assert 'status.classList.toggle("is-stopping", stopping)' in APP
    assert 'status.classList.toggle("is-resuming", resuming)' in APP
    assert "mergeRunnerControlResponse" not in APP
    assert ".status.has-global-work #st-stop" in CSS
    assert "Schedules are paused" in DAILY
    assert "Kairo is working in another chat" in DAILY


def test_chat_voice_is_review_first_and_uses_the_existing_safe_controller() -> None:
    assert "api.toggleVoiceCapture" in CHAT
    assert "api.cancelVoiceCapture" in CHAT
    assert 'mode || "dictation"' in CHAT
    assert 'status.textContent = disabled ? "Voice unavailable"' in CHAT
    assert 'api.post("/api/turn",' not in CHAT  # voice never submits a raw chat turn


def test_composer_omits_unsupported_effort_and_uses_a_quiet_context_shelf() -> None:
    header = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
    assert "if (!supported) return null;" in header
    assert "n/a for this model" not in header
    assert "Auto-managed" not in header
    assert "hdr-model-menu" in header
    assert "☰" not in header  # no duplicate/hamburger control in the composer
    for token in ("chat-history-panel", "chat-history-layer", "chat-context-handle", "Library"):
        assert token in CHAT or token in CSS or token in header


def test_chat_uses_plain_project_language_and_a_time_aware_welcome() -> None:
    header = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
    for token in ("No project", "Untitled"):
        assert token in header
    for token in ("Good morning.", "Good afternoon.", "Good evening.", "hints"):
        assert token in CHAT


def test_chat_shelf_has_scoped_files_and_honest_project_outputs() -> None:
    for token in (
        "/api/chat/files",
        "/api/chat/outputs",
        "/api/chat/knowledge",
        "Files",
        "Outputs",
        "Knowledge",
        "Project outputs",
        "Open full graph",
        "Attached folders",
    ):
        assert token in CHAT
    assert "source_session_id" not in CHAT  # the browser never chooses the scope
    assert "api.download" in CHAT


def test_chat_knowledge_shelf_is_project_bound_and_metadata_only() -> None:
    for token in (
        "Choose a project",
        "Project files",
        "Knowledge connections",
        "uv run jarvis graph rebuild",
        "window.location.hash",
    ):
        assert token in CHAT
    assert "local_path" not in CHAT
    assert "markdown_path" not in CHAT


def test_chat_file_button_uses_the_scoped_local_upload_path() -> None:
    for token in (
        "chat-attach",
        "chat-file-input",
        "chat-folder-input",
        "webkitdirectory",
        "relative_path",
        "finalize",
        "projectImport",
        "FormData",
        "api.upload",
        "PROJECT_IMPORT_CONCURRENCY",
        "PROJECT_IMPORT_MAX_FILES",
        "data/connectors",
    ):
        assert token in CHAT
    assert '"/api/chat/attachments"' in CHAT
    assert "chatAttachments" in APP
    for token in (
        "project-import-progress",
        "Analyzing your project files",
        "Building your local knowledge map",
        "Graphify is connecting folders and files",
        'importState.stage = "graph"',
    ):
        assert token in CHAT


def test_chat_knowledge_uses_the_safe_shared_source_tree() -> None:
    assert '"../ui/source-tree.js"' in CHAT
    assert "renderSourceTree(sources)" in CHAT
    assert '"/api/chat/knowledge/detach"' in CHAT
    assert "Remove this folder from the project?" in CHAT
    assert "cleared_chunks" in CHAT
