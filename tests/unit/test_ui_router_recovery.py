"""Static contract for the shell's async route ownership and recovery boundary."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")
DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
SETTINGS = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")
WORKSPACE = (STATIC_DIR / "screens" / "workspace.js").read_text(encoding="utf-8")
HEADER = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
COSTS = (STATIC_DIR / "screens" / "costs.js").read_text(encoding="utf-8")
GATE = (STATIC_DIR / "screens" / "gate.js").read_text(encoding="utf-8")
CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
MEETINGS = (STATIC_DIR / "screens" / "meetings.js").read_text(encoding="utf-8")
STUDIO = (STATIC_DIR / "screens" / "studio.js").read_text(encoding="utf-8")


def test_route_identity_includes_server_owned_scope_and_hash_arguments() -> None:
    route_key = APP[APP.index("function routeKey()") : APP.index("function routeLabel(")]
    for value in (
        "workspaceId",
        "state.context?.session_id",
        "state.context?.project_id",
        "state.route",
        "state.routeArgs",
    ):
        assert value in route_key


def test_runner_status_reads_are_owned_by_the_workspace_generation() -> None:
    runner = APP[APP.index("async runnerStatus") : APP.index("async post(")]
    assert "runnerStatusRequestGeneration === generation" in runner
    assert "runnerStatusAbort?.abort()" in runner
    assert "generation !== contextGeneration" in runner
    assert "runnerStatusGeneration === generation" in runner
    assert APP.count("advanceContextGeneration()") >= 4


def test_transcript_hydration_is_keyed_and_invalidated_by_live_authority() -> None:
    hydration = APP[
        APP.index("let rehydratedConversationKey") : APP.index("async function refreshVoiceStatus")
    ]
    assert "conversationHydrationRevision" in hydration
    assert "conversationHydrationKey()" in hydration
    assert "key !== conversationHydrationKey()" in hydration
    assert "rehydratedConversationKey = null" in hydration
    assert "clearWorkspaceLocalState()" in APP
    assert APP.count("clearAuthorityLocalState(") >= 4
    assert "state.parkedTaskApprovals.clear()" in APP
    assert "_parkedTaskDialog?.dismiss?.()" in APP
    assert "authorityIsCurrent(token)" in APP
    assert "ownsExactSuccessor" in APP
    assert "authorityGeneration === startingAuthority + 1" in APP
    assert "state.context?.context_revision === startingContext.context_revision + 1" in APP
    assert "await api.runnerStatus({ refresh: true })" in APP
    assert "api.authorityIsCurrent(authorityToken)" in CHAT
    assert "while (importIsCurrent()" in CHAT
    assert CHAT.count("if (!importIsCurrent()) return;") >= 5
    assert "state.chat !== emptyChat || state.chat.length" in APP
    assert "authorityIsCurrent(authorityToken)" in CHAT


def test_initial_route_reads_are_newest_wins_and_required_failures_recover() -> None:
    assert 'const ROUTE_RENDER_STALE = Symbol("route-render-stale")' in APP
    assert 'const API_READ_OUTCOME = Symbol("api-read-outcome")' in APP
    assert "context.initial && !isCurrent()" in APP
    assert "scoped.getRequired" in APP
    assert "scoped.refreshRoute" in APP
    assert "required && context.initial && wrapped && outcome.failure" in APP
    assert "EXPECTED_UNAVAILABLE_READ_STATUSES = new Set([404, 503])" in APP
    assert "!EXPECTED_UNAVAILABLE_READ_STATUSES.has(r.status)" in APP
    assert "result.then" in APP
    assert "error === ROUTE_RENDER_STALE" in APP
    assert "renderRouteFailure(container, error)" in APP
    assert "INITIAL_ROUTE_READ_TIMEOUT_MS" in APP
    assert 'new Error("initial route render timed out")' in APP
    assert "boundedInitialRouteRender(result, controller)" in APP
    assert "controller.abort()" in APP


def test_dictation_retargets_only_within_the_same_authority() -> None:
    capture = APP[APP.index("async toggleVoiceCapture") : APP.index("cancelVoiceCapture()")]
    assert "const navigationToken = navigationGeneration" in capture
    assert "navigationToken !== navigationGeneration" in capture
    assert "api.restoreTurnDraft(transcript)" in capture
    assert 'document.getElementById("chat-input") !== input' in CHAT
    assert "api.restoreTurnDraft(transcript)" in CHAT


def test_optional_reads_keep_composite_screens_available() -> None:
    scoped = APP[APP.index("function scopedRouteApi") : APP.index("function renderRouteLoading")]
    assert "scoped.get =" in scoped
    assert "scoped.getRequired =" in scoped
    assert 'api.get("/api/agents")' in GATE
    assert 'api.getRequired("/api/agents")' not in GATE
    assert 'api.getRequired("/api/costs")' in COSTS
    assert 'api.get("/api/roi")' in COSTS
    assert "route-partial-failure" in COSTS


def test_navigation_replaces_only_changed_or_explicitly_retried_routes() -> None:
    assert "if (routeChanged || reset)" in APP
    assert "container = replaceRouteContainer(container)" in APP
    assert 'container.className = "screen"' in APP
    assert 'aria-current", "page"' in APP
    assert "activeRouteController.abort()" in APP
    assert "shellLoading?.isConnected" in APP


def test_context_lifecycle_rerenders_every_route_and_redirects_stale_workspaces() -> None:
    assert APP.count("rerenderAfterContextChange") >= 3
    lifecycle = APP[
        APP.index("function rerenderAfterContextChange") : APP.index("function renderRoute(")
    ]
    assert 'state.route === "workspace"' in lifecycle
    assert 'history.replaceState(null, "", nextHash)' in lifecycle
    assert "navigate({ preserveIntent: true })" in lifecycle
    assert "pollStatus({ refreshChatHeader: sameWorkspaceContext })" in APP


def test_loading_and_failure_states_are_accessible_and_actionable() -> None:
    for token in (
        'status.setAttribute("role", "status")',
        'status.setAttribute("aria-live", "polite")',
        'failure.setAttribute("role", "alert")',
        'retry.textContent = "Try again"',
        "renderRoute({ reset: true })",
        'container.setAttribute("aria-busy", "true")',
        'container.setAttribute("aria-busy", "false")',
    ):
        assert token in APP
    assert ".route-state" in CSS and ".route-state-actions" in CSS
    assert "prefers-reduced-motion: reduce" in CSS


def test_unreturned_and_nested_async_renderers_check_route_ownership() -> None:
    assert "api.renderIsCurrent()" in DAILY
    assert "api.renderIsCurrent()" in SETTINGS
    assert "api.renderIsCurrent()" in HEADER
    assert WORKSPACE.count("api.renderIsCurrent()") >= 2
    assert "_refreshRevision" in HEADER
    assert "briefingReadRevision" in DAILY
    assert "tasksReadRevision" in DAILY
    assert "_statusAuthorityToken" in SETTINGS
    assert "inFlightAuthorityToken" in MEETINGS
    assert "S.confirmation !== confirmation" in STUDIO
    assert "confirmation.params" in STUDIO


def test_error_ui_never_interpolates_exception_detail() -> None:
    failure = APP[APP.index("function renderRouteFailure") : APP.index("function refreshIfActive")]
    assert "error.message" not in failure
    assert "error.stack" not in failure
    assert "Check the connection and try again." in failure


def test_project_service_change_refreshes_only_authoritative_truth_surfaces() -> None:
    handler = APP[
        APP.index('if (msg.kind === "project_services_changed")')
        : APP.index('if (msg.kind === "project_changed")')
    ]
    assert 'busEmit("project_services_changed", msg)' in handler
    assert "dismissProjectServiceAccess()" in handler
    assert "refreshHeader()" in handler
    assert "renderRoute()" in handler
    for route in ("daily", "hub", "projects", "settings", "studio", "workspace"):
        assert f'"{route}"' in handler
    assert "msg.services" not in handler
    handler_start = APP.index('if (msg.kind === "project_services_changed")')
    workspace_filter = APP.index(
        'if (msg.workspace_id && msg.workspace_id !== workspaceId) return;'
    )
    context_filter = APP.index('if (msg.session_id != null && !acceptsContext(msg)')
    assert workspace_filter < handler_start
    assert context_filter < handler_start
