"""Attendee/contact resolution (Phase 12 Task 3). Keyless, pure.

The load-bearing pin: an ambiguous attendee can never reach a preview/draft — require_resolved
refuses until every entry is a real address, so no intent is ever created for a guessed recipient.
"""

from __future__ import annotations

import pytest

from jarvis.actions.resolve import (
    AttendeesUnresolved,
    is_email,
    normalize_email,
    require_resolved,
    resolve_attendees,
)


def test_plain_addresses_resolve() -> None:
    r = resolve_attendees(["alice@example.com", "bob@work.co.uk"])
    assert r.ok
    assert r.resolved == ("alice@example.com", "bob@work.co.uk")
    assert r.ambiguous == ()


def test_display_name_form_is_extracted() -> None:
    r = resolve_attendees(["Alice Smith <alice@example.com>"])
    assert r.ok and r.resolved == ("alice@example.com",)


def test_bare_names_are_ambiguous() -> None:
    r = resolve_attendees(["Bob", "my manager", "the team"])
    assert not r.ok
    assert r.resolved == ()
    assert r.ambiguous == ("Bob", "my manager", "the team")


def test_mixed_input_flags_only_the_ambiguous() -> None:
    r = resolve_attendees(["alice@example.com", "Bob"])
    assert not r.ok
    assert r.resolved == ("alice@example.com",)
    assert r.ambiguous == ("Bob",)


def test_domain_is_lowercased_and_dupes_removed() -> None:
    r = resolve_attendees(["Alice@Example.COM", "alice@example.com", "Alice <alice@example.com>"])
    assert r.resolved == ("Alice@example.com",)  # local case kept, domain lowered, de-duped


def test_malformed_addresses_are_ambiguous() -> None:
    # No domain dot, trailing dot-dot, missing local/domain, spaces — none are valid addresses.
    for bad in ("alice@localhost", "a..b@example.com", "@example.com", "alice@", "a b@x.com"):
        assert is_email(bad) is False
        assert resolve_attendees([bad]).ambiguous == (bad,)


def test_require_resolved_returns_addresses_when_all_valid() -> None:
    assert require_resolved(["alice@example.com", "b@x.io"]) == ("alice@example.com", "b@x.io")


def test_require_resolved_refuses_ambiguous_so_no_intent_is_built() -> None:
    # THE pin: the drafting path calls require_resolved; an ambiguous attendee makes it raise
    # BEFORE any request/preview/intent exists. The exception carries the ambiguous entries + a
    # user-facing ASK.
    with pytest.raises(AttendeesUnresolved) as exc:
        require_resolved(["alice@example.com", "Bob", "Carol"])
    assert exc.value.ambiguous == ("Bob", "Carol")
    assert "Bob" in str(exc.value) and "email address" in str(exc.value)


def test_empty_entries_ignored() -> None:
    r = resolve_attendees(["", "  ", "alice@example.com"])
    assert r.ok and r.resolved == ("alice@example.com",)


def test_normalize_email_helper() -> None:
    assert normalize_email("  Bob <BOB@Example.COM> ") == "BOB@example.com"
