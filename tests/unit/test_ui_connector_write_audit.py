"""The Notifications connector-write audit remains evidence-only."""

from kira.ui.server import STATIC_DIR

GATE = (STATIC_DIR / "screens" / "gate.js").read_text(encoding="utf-8")


def test_gate_reads_and_safely_renders_metadata_only_connector_writes() -> None:
    assert 'id="gate-connector-writes"' in GATE
    assert 'api.get("/api/connector-writes")' in GATE
    assert "fillConnectorWrites" in GATE
    assert "write.provider, write.verb, write.status, write.at" in GATE
    audit = GATE.split("async function fillConnectorWrites", 1)[1].split("function actionBtn", 1)[0]
    assert "innerHTML" not in audit
    for forbidden in ("remote_id", "rollback_ref", "egress_ref", "trace_id", "request", "preview"):
        assert forbidden not in audit
