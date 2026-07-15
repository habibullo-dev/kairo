"""Single-owner grants, credentials, throttling, sessions, recovery, and step-up."""

from __future__ import annotations

import datetime as dt

import pytest

from kira.persistence.db import connect
from kira.persistence.sessions import SessionStore
from kira.ui.owner_auth import (
    ABSOLUTE_SESSION_DAYS,
    Argon2PasswordHasher,
    OwnerAlreadyEnrolledError,
    OwnerAuthError,
    OwnerAuthService,
    OwnerGrantError,
    OwnerLoginThrottledError,
)

PASSWORD = "A unique owner passphrase 2026!"


async def _service(tmp_path):
    db = await connect(tmp_path / "owner.db")
    store = SessionStore(db)
    now = [dt.datetime(2026, 7, 14, tzinfo=dt.UTC)]
    service = OwnerAuthService(
        db,
        store.lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: now[0],
    )
    return db, service, now


async def _enroll(service: OwnerAuthService):
    grant = await service.issue_auth_grant("enroll")
    return await service.enroll(grant.token, "habib", PASSWORD)


async def test_enrollment_is_grant_bound_singleton_and_argon2_only(tmp_path) -> None:
    db, service, _now = await _service(tmp_path)
    try:
        with pytest.raises(OwnerGrantError):
            await service.enroll("wrong", "habib", PASSWORD)
        outcome = await _enroll(service)
        assert outcome.profile.username == "habib"
        row = await (
            await db.execute(
                "SELECT a.username, p.password_hash FROM owner_accounts a "
                "JOIN owner_password_credentials p ON p.owner_id = a.id"
            )
        ).fetchone()
        assert row is not None and row[0] == "habib"
        assert PASSWORD not in row[1]
        assert row[1].startswith("$argon2id$")
        assert await service.validate_session(outcome.session.token) is not None

        second_grant = await service.issue_auth_grant("enroll")
        with pytest.raises(OwnerAlreadyEnrolledError):
            await service.enroll(second_grant.token, "other", "Another unique passphrase 2026!")
    finally:
        await db.close()


async def test_grants_are_scoped_one_use_and_expiring(tmp_path) -> None:
    db, service, now = await _service(tmp_path)
    try:
        grant = await service.issue_auth_grant("enroll")
        assert await service.auth_grant_valid(grant.token, "enroll")
        assert not await service.auth_grant_valid(grant.token, "recover")
        await service.enroll(grant.token, "habib", PASSWORD)
        assert not await service.auth_grant_valid(grant.token, "enroll")
        with pytest.raises(OwnerAlreadyEnrolledError):
            await service.enroll(grant.token, "habib", PASSWORD)

        recovery = await service.issue_auth_grant("recover")
        now[0] += dt.timedelta(minutes=11)
        assert not await service.auth_grant_valid(recovery.token, "recover")
        with pytest.raises(OwnerGrantError):
            await service.recover(recovery.token, "A replacement passphrase 2026!")
    finally:
        await db.close()


async def test_password_policy_normalizes_and_rejects_common_or_oversized_values(tmp_path) -> None:
    db, service, _now = await _service(tmp_path)
    try:
        grant = await service.issue_auth_grant("enroll")
        with pytest.raises(OwnerAuthError, match="15"):
            await service.enroll(grant.token, "habib", "too short")
        with pytest.raises(OwnerAuthError, match="common"):
            await service.enroll(grant.token, "habib", "correct horse battery staple")
        with pytest.raises(OwnerAuthError, match="Invalid"):
            await service.enroll(grant.token, "habib", "x" * 1025)
        assert not await service.is_enrolled()
    finally:
        await db.close()


async def test_login_is_generic_rehashes_and_durably_throttles(tmp_path) -> None:
    db, service, now = await _service(tmp_path)
    try:
        await _enroll(service)
        result = await service.login("HABIB", PASSWORD)
        assert result is not None and result.profile.username == "habib"
        assert await service.login("unknown", PASSWORD) is None

        for _ in range(3):
            assert await service.login("habib", "A wrong but sufficiently long passphrase") is None
        with pytest.raises(OwnerLoginThrottledError) as blocked:
            await service.login("habib", "A wrong but sufficiently long passphrase")
        assert blocked.value.retry_after_seconds == 30
        row = await (
            await db.execute("SELECT failed_attempts, locked_until FROM owner_accounts")
        ).fetchone()
        assert row is not None and row[0] == 5 and row[1] is not None

        now[0] += dt.timedelta(seconds=31)
        assert await service.login("habib", PASSWORD) is not None
        row = await (
            await db.execute("SELECT failed_attempts, locked_until FROM owner_accounts")
        ).fetchone()
        assert row == (0, None)
    finally:
        await db.close()


async def test_offline_password_verification_issues_no_session(tmp_path) -> None:
    db, service, _now = await _service(tmp_path)
    try:
        enrolled = await _enroll(service)
        before = await (await db.execute("SELECT COUNT(*) FROM owner_sessions")).fetchone()
        assert before == (1,)

        assert not await service.verify_owner_password("A wrong maintenance password")
        assert await service.verify_owner_password(PASSWORD)
        after = await (await db.execute("SELECT COUNT(*) FROM owner_sessions")).fetchone()
        assert after == before
        assert await service.validate_session(enrolled.session.token) is not None
    finally:
        await db.close()


async def test_session_rolls_idle_but_never_absolute_and_stores_no_bearer(tmp_path) -> None:
    db, service, now = await _service(tmp_path)
    try:
        outcome = await _enroll(service)
        token = outcome.session.token
        row = await (
            await db.execute("SELECT token_hash, absolute_expires_at FROM owner_sessions")
        ).fetchone()
        assert row is not None and token not in row[0]
        original_absolute = row[1]

        state = await service.validate_session(token)
        assert state is not None and state.fresh and not state.renew_cookie
        now[0] += dt.timedelta(hours=25)
        renewed = await service.validate_session(token)
        assert renewed is not None and renewed.renew_cookie
        assert renewed.absolute_expires_at == original_absolute

        now[0] = dt.datetime(2026, 7, 14, tzinfo=dt.UTC) + dt.timedelta(
            days=ABSOLUTE_SESSION_DAYS, seconds=1
        )
        assert await service.validate_session(token) is None
    finally:
        await db.close()


async def test_step_up_rotates_sid_and_preserves_absolute_deadline(tmp_path) -> None:
    db, service, now = await _service(tmp_path)
    try:
        outcome = await _enroll(service)
        old = outcome.session
        now[0] += dt.timedelta(minutes=6)
        state = await service.validate_session(old.token)
        assert state is not None and not state.fresh

        replacement = await service.step_up(old.token, PASSWORD)
        assert replacement is not None and replacement.token != old.token
        assert replacement.absolute_expires_at == old.absolute_expires_at
        assert await service.validate_session(old.token) is None
        new_state = await service.validate_session(replacement.token)
        assert new_state is not None and new_state.fresh
    finally:
        await db.close()


async def test_recovery_bumps_epoch_revokes_all_and_returns_one_new_session(tmp_path) -> None:
    db, service, _now = await _service(tmp_path)
    try:
        enrolled = await _enroll(service)
        second = await service.login("habib", PASSWORD)
        assert second is not None
        grant = await service.issue_auth_grant("recover")
        recovered = await service.recover(grant.token, "A replacement passphrase 2026!")

        assert recovered.profile.credential_epoch == 2
        assert await service.validate_session(enrolled.session.token) is None
        assert await service.validate_session(second.session.token) is None
        assert await service.validate_session(recovered.session.token) is not None
        assert await service.login("habib", PASSWORD) is None
        assert await service.login("habib", "A replacement passphrase 2026!") is not None
    finally:
        await db.close()
