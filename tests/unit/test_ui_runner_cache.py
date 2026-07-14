"""Runner status has one cache owner; secondary surfaces consume that shared truth."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HEADER = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
PALETTE = (STATIC_DIR / "ui" / "palette.js").read_text(encoding="utf-8")
CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
SETTINGS = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")


def test_app_owns_one_deduplicated_runner_status_cache() -> None:
    assert "let runnerStatusRequest = null;" in APP
    assert "async runnerStatus({ refresh = false } = {})" in APP
    assert 'api.get("/api/runner", { signal: controller.signal })' in APP
    assert "runnerStatusRequestGeneration === generation" in APP
    assert "runnerStatusAbort?.abort()" in APP
    assert "state.runnerStatusError = runner == null;" in APP
    assert "runnerStatusGeneration === generation && state.runnerStatusError" in APP
    assert "api.runnerStatus({ refresh: true })" in APP


def test_secondary_surfaces_use_the_shared_runner_cache() -> None:
    for source in (HEADER, PALETTE, CHAT, SETTINGS):
        assert "runnerStatus" in source
        assert 'get("/api/runner")' not in source


def test_mutations_and_partial_statuses_cannot_present_stale_state_as_writable() -> None:
    assert "refreshHeader({ refreshRunner: true })" in HEADER
    assert "await api.runnerStatus({ refresh: true });" in PALETTE
    assert "await refreshHeader();" in PALETTE
    assert "Project status unavailable" in HEADER
    assert "Model status unavailable" in HEADER
    assert "Mode status unavailable" in HEADER
    assert "disabled: unavailable" in HEADER


def test_runner_read_recovery_rerenders_previously_disabled_header() -> None:
    assert "const runnerWasUnavailable = state.runnerStatusError;" in APP
    assert "if (runnerWasUnavailable || refreshChatHeader) refreshHeader();" in APP
