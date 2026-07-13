"""Studio polish pins (Phase 11 T12) — the head-reviewer (Fable) badge, the shared escaper, the
model+provider route chip, and the no-new-authority invariant. Presentational polish over the
existing Phase 10B orchestration flow (unchanged)."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

STUDIO = (STATIC_DIR / "screens" / "studio.js").read_text(encoding="utf-8")


def test_head_reviewer_visibly_badged() -> None:
    # The head synthesizer/verdict (Fable, an engine stage) is badged on the roster, the live
    # verdict, and a run's synthesis — three call sites.
    assert "head-badge" in STUDIO and "Fable" in STUDIO
    assert STUDIO.count("headBadge(S.head)") == 3


def test_uses_shared_escaper_no_local_dup() -> None:
    assert 'import { esc } from "../ui/dom.js"' in STUDIO
    assert "function esc(" not in STUDIO  # the local escaper was removed


def test_roster_shows_model_and_provider() -> None:
    assert "routeLabel" in STUDIO and "provider" in STUDIO


def test_studio_adds_no_new_authority() -> None:
    # Studio's only mutation is the existing gated orchestration run; no turn / no other route.
    assert "/api/orchestration/run" in STUDIO
    assert "/api/turn" not in STUDIO


def test_studio_does_not_claim_roi_before_review_acceptance() -> None:
    assert 'roi.outcome === "review_accepted"' in STUDIO
    assert "Time-saved value is not claimed." in STUDIO


def test_studio_describes_skill_manifest_as_recorded_evidence_only() -> None:
    assert "Skill packs recorded at run start" in STUDIO
    assert "recorded skills:" in STUDIO
    assert "Recorded metadata does not prove prompt injection" in STUDIO
    assert "Shadow mode records manifests without injecting guidance." in STUDIO
