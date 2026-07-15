"""Schema v31 pins singleton identity, digest-only sessions, and scoped grants."""

import aiosqlite
import pytest

from kira.persistence import migrations as M


async def _build_v30() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 30:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v31_adds_owner_auth_schema_idempotently() -> None:
    db = await _build_v30()
    try:
        await db.executescript(M._SCHEMA_V31)
        await db.executescript(M._SCHEMA_V31)
        owner_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(owner_accounts)")
            ).fetchall()
        }
        password_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(owner_password_credentials)")
            ).fetchall()
        }
        passkey_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(owner_passkey_credentials)")
            ).fetchall()
        }
        session_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(owner_sessions)")
            ).fetchall()
        }
        grant_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(owner_auth_grants)")
            ).fetchall()
        }

        assert {"id", "username", "credential_epoch", "failed_attempts"} <= owner_columns
        assert {"owner_id", "password_hash"} <= password_columns
        assert {
            "credential_id",
            "public_key",
            "sign_count",
            "transports_json",
            "user_verified",
            "backup_eligible",
        } <= passkey_columns
        assert {
            "token_hash",
            "credential_epoch",
            "idle_expires_at",
            "absolute_expires_at",
            "step_up_until",
        } <= session_columns
        assert {"grant_hash", "scope", "expires_at", "consumed_at"} <= grant_columns
        assert not (session_columns & {"token", "cookie", "session_id"})

        now = "2026-07-14T00:00:00+00:00"
        await db.execute(
            "INSERT INTO owner_accounts "
            "(id, username, credential_epoch, failed_attempts, created_at, updated_at) "
            "VALUES (1, 'owner', 1, 0, ?, ?)",
            (now, now),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO owner_accounts "
                "(id, username, credential_epoch, failed_attempts, created_at, updated_at) "
                "VALUES (2, 'other', 1, 0, ?, ?)",
                (now, now),
            )
    finally:
        await db.close()
