"""Single-owner identity and durable authentication for the local workstation.

This is deliberately a closed, one-human system rather than a general user/role framework.
Enrollment and recovery consume short-lived purpose-bound grants; application sessions can be
issued only by successful enrollment, login, recovery, or password step-up.  The database stores
Argon2id verifier records and SHA-256 token digests, never passwords or replayable bearer values.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import math
import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite
from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from kira.persistence.db import transaction

OWNER_ID = 1
IDLE_SESSION_DAYS = 30
ABSOLUTE_SESSION_DAYS = 90
SESSION_TOUCH_HOURS = 24
STEP_UP_MINUTES = 5
AUTH_GRANT_MINUTES = 10
LOGIN_FAILURES_BEFORE_LOCK = 5
LOGIN_MAX_LOCK_SECONDS = 15 * 60

_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{2,63}$")
_MIN_PASSWORD_CHARS = 15
_MAX_PASSWORD_BYTES = 1024
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012345",
        "correcthorsebatterystaple",
        "letmeinletmeinletmein",
        "passwordpassword",
        "qwertyqwertyqwerty",
    }
)


class OwnerAuthError(ValueError):
    """Base class for safe owner-auth validation failures."""


class OwnerAlreadyEnrolledError(OwnerAuthError):
    """The singleton owner transition was already committed."""


class OwnerGrantError(OwnerAuthError):
    """A bootstrap grant was absent, expired, consumed, or used for the wrong purpose."""


class OwnerLoginThrottledError(OwnerAuthError):
    """Durable brute-force backoff is currently active."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(retry_after_seconds, 1)
        super().__init__("Login temporarily unavailable")


@dataclass(frozen=True)
class OwnerProfile:
    id: int
    username: str
    credential_epoch: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IssuedOwnerSession:
    token: str
    idle_expires_at: str
    absolute_expires_at: str
    cookie_max_age: int


@dataclass(frozen=True)
class OwnerAuthOutcome:
    profile: OwnerProfile
    session: IssuedOwnerSession


@dataclass(frozen=True)
class OwnerSessionState:
    token_hash: str
    owner_id: int
    credential_epoch: int
    idle_expires_at: str
    absolute_expires_at: str
    fresh: bool
    renew_cookie: bool
    cookie_max_age: int


@dataclass(frozen=True)
class IssuedAuthGrant:
    token: str
    scope: str
    expires_at: str


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_time(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Argon2PasswordHasher:
    """Argon2id PHC records with production defaults and cheap injectable test settings."""

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost: int = 64 * 1024,
        parallelism: int = 1,
    ) -> None:
        self._hasher = _Argon2Hasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, record: str) -> bool:
        try:
            return self._hasher.verify(record, password)
        except (InvalidHashError, VerificationError):
            return False

    def needs_rehash(self, record: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(record)
        except InvalidHashError:
            return False


class OwnerAuthService:
    """Transactional singleton-owner, grant, password, and session lifecycle."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        lock: asyncio.Lock,
        *,
        hasher: Argon2PasswordHasher | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self.db = db
        self.lock = lock
        self.hasher = hasher or Argon2PasswordHasher()
        self.clock = clock
        self._hash_slots = asyncio.Semaphore(2)
        # Login state is intentionally discoverable through /setup vs /login redirects, but a
        # dummy verifier still avoids turning an accidental login call into a cheap oracle.
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(24))

    def _now(self) -> dt.datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            raise ValueError("owner auth clock must be timezone-aware")
        return moment.astimezone(dt.UTC)

    @staticmethod
    def _profile(row) -> OwnerProfile:  # noqa: ANN001 - aiosqlite row protocol
        return OwnerProfile(
            id=int(row[0]),
            username=str(row[1]),
            credential_epoch=int(row[2]),
            created_at=str(row[3]),
            updated_at=str(row[4]),
        )

    async def profile(self) -> OwnerProfile | None:
        row = await (
            await self.db.execute(
                "SELECT id, username, credential_epoch, created_at, updated_at "
                "FROM owner_accounts WHERE id = 1"
            )
        ).fetchone()
        return self._profile(row) if row is not None else None

    async def is_enrolled(self) -> bool:
        return await self.profile() is not None

    @staticmethod
    def validate_username(username: str) -> str:
        normalized = unicodedata.normalize("NFC", username).strip()
        if not _USERNAME.fullmatch(normalized):
            raise OwnerAuthError(
                "Username must be 3–64 characters using letters, numbers, or . _ @ + -"
            )
        return normalized

    @staticmethod
    def _normalize_password(password: str, *, new: bool) -> str:
        normalized = unicodedata.normalize("NFC", password)
        encoded = normalized.encode("utf-8")
        if not encoded or len(encoded) > _MAX_PASSWORD_BYTES:
            raise OwnerAuthError("Invalid credentials")
        if new:
            if len(normalized) < _MIN_PASSWORD_CHARS:
                raise OwnerAuthError("Password must be at least 15 characters")
            common_key = re.sub(r"[^a-z0-9]", "", normalized.casefold())
            if common_key in _COMMON_PASSWORDS:
                raise OwnerAuthError("Choose a less common password")
        return normalized

    async def _hash_password(self, password: str) -> str:
        async with self._hash_slots:
            return await asyncio.to_thread(self.hasher.hash, password)

    async def _verify_password(self, password: str, record: str) -> bool:
        async with self._hash_slots:
            return await asyncio.to_thread(self.hasher.verify, password, record)

    async def issue_auth_grant(self, scope: str) -> IssuedAuthGrant:
        if scope not in {"enroll", "recover"}:
            raise OwnerGrantError("Invalid authentication grant scope")
        now = self._now()
        expires = now + dt.timedelta(minutes=AUTH_GRANT_MINUTES)
        token = secrets.token_urlsafe(32)
        async with self.lock:
            await self.db.execute(
                "INSERT INTO owner_auth_grants "
                "(grant_hash, scope, created_at, expires_at, consumed_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (_digest(token), scope, now.isoformat(), expires.isoformat()),
            )
            await self.db.commit()
        return IssuedAuthGrant(token=token, scope=scope, expires_at=expires.isoformat())

    async def auth_grant_valid(self, token: str | None, scope: str) -> bool:
        if not token or scope not in {"enroll", "recover"}:
            return False
        row = await (
            await self.db.execute(
                "SELECT expires_at, consumed_at FROM owner_auth_grants "
                "WHERE grant_hash = ? AND scope = ?",
                (_digest(token), scope),
            )
        ).fetchone()
        expires = _parse_time(row[0]) if row is not None else None
        return bool(row is not None and row[1] is None and expires and self._now() < expires)

    async def _consume_grant(self, token: str, scope: str, now: dt.datetime) -> None:
        """Consume inside an already-held database transaction."""
        row = await (
            await self.db.execute(
                "SELECT expires_at, consumed_at FROM owner_auth_grants "
                "WHERE grant_hash = ? AND scope = ?",
                (_digest(token), scope),
            )
        ).fetchone()
        expires = _parse_time(row[0]) if row is not None else None
        if row is None or row[1] is not None or expires is None or now >= expires:
            raise OwnerGrantError("Authentication grant is invalid or expired")
        cursor = await self.db.execute(
            "UPDATE owner_auth_grants SET consumed_at = ? "
            "WHERE grant_hash = ? AND scope = ? AND consumed_at IS NULL",
            (now.isoformat(), _digest(token), scope),
        )
        if cursor.rowcount != 1:
            raise OwnerGrantError("Authentication grant was already consumed")

    async def _insert_session(
        self,
        *,
        now: dt.datetime,
        credential_epoch: int,
        auth_method: str = "password",
        absolute_cap: dt.datetime | None = None,
        stepped_up: bool = True,
    ) -> IssuedOwnerSession:
        """Insert inside an already-held transaction; never expose unauthenticated issuance."""
        absolute = absolute_cap or now + dt.timedelta(days=ABSOLUTE_SESSION_DAYS)
        idle = min(now + dt.timedelta(days=IDLE_SESSION_DAYS), absolute)
        step_up_until = now + dt.timedelta(minutes=STEP_UP_MINUTES) if stepped_up else now
        token = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO owner_sessions "
            "(token_hash, owner_id, credential_epoch, auth_method, created_at, last_seen_at, "
            "idle_expires_at, absolute_expires_at, step_up_until, revoked_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                _digest(token),
                credential_epoch,
                auth_method,
                now.isoformat(),
                now.isoformat(),
                idle.isoformat(),
                absolute.isoformat(),
                step_up_until.isoformat(),
            ),
        )
        max_age = max(1, math.ceil((min(idle, absolute) - now).total_seconds()))
        return IssuedOwnerSession(token, idle.isoformat(), absolute.isoformat(), max_age)

    async def enroll(self, grant_token: str, username: str, password: str) -> OwnerAuthOutcome:
        if await self.is_enrolled():
            raise OwnerAlreadyEnrolledError("Owner enrollment is already closed")
        if not await self.auth_grant_valid(grant_token, "enroll"):
            raise OwnerGrantError("Authentication grant is invalid or expired")
        normalized_username = self.validate_username(username)
        normalized_password = self._normalize_password(password, new=True)
        password_hash = await self._hash_password(normalized_password)
        now = self._now()
        async with transaction(self.db, self.lock):
            await self._consume_grant(grant_token, "enroll", now)
            exists = await (
                await self.db.execute("SELECT 1 FROM owner_accounts WHERE id = 1")
            ).fetchone()
            if exists is not None:
                raise OwnerAlreadyEnrolledError("Owner enrollment is already closed")
            await self.db.execute(
                "INSERT INTO owner_accounts "
                "(id, username, credential_epoch, failed_attempts, locked_until, created_at, "
                "updated_at) VALUES (1, ?, 1, 0, NULL, ?, ?)",
                (normalized_username, now.isoformat(), now.isoformat()),
            )
            await self.db.execute(
                "INSERT INTO owner_password_credentials "
                "(owner_id, password_hash, created_at, updated_at) VALUES (1, ?, ?, ?)",
                (password_hash, now.isoformat(), now.isoformat()),
            )
            session = await self._insert_session(now=now, credential_epoch=1)
        profile = await self.profile()
        if profile is None:
            raise RuntimeError("owner enrollment did not persist")
        return OwnerAuthOutcome(profile=profile, session=session)

    async def _credential_row(self):  # noqa: ANN202 - private aiosqlite row protocol
        return await (
            await self.db.execute(
                "SELECT a.username, a.credential_epoch, a.failed_attempts, a.locked_until, "
                "p.password_hash FROM owner_accounts a "
                "JOIN owner_password_credentials p ON p.owner_id = a.id WHERE a.id = 1"
            )
        ).fetchone()

    def _retry_after(self, locked_until: object) -> int:
        locked = _parse_time(locked_until)
        if locked_until is not None and locked is None:
            return LOGIN_MAX_LOCK_SECONDS
        if locked is None:
            return 0
        return max(0, math.ceil((locked - self._now()).total_seconds()))

    async def _record_failure(self) -> int:
        now = self._now()
        async with transaction(self.db, self.lock):
            row = await (
                await self.db.execute(
                    "SELECT failed_attempts FROM owner_accounts WHERE id = 1"
                )
            ).fetchone()
            if row is None:
                return 0
            attempts = int(row[0]) + 1
            delay = 0
            if attempts >= LOGIN_FAILURES_BEFORE_LOCK:
                delay = min(
                    30 * (2 ** (attempts - LOGIN_FAILURES_BEFORE_LOCK)),
                    LOGIN_MAX_LOCK_SECONDS,
                )
            locked_until = (now + dt.timedelta(seconds=delay)).isoformat() if delay else None
            await self.db.execute(
                "UPDATE owner_accounts SET failed_attempts = ?, locked_until = ? WHERE id = 1",
                (attempts, locked_until),
            )
        return delay

    async def login(self, username: str, password: str) -> OwnerAuthOutcome | None:
        try:
            normalized_username = self.validate_username(username)
            normalized_password = self._normalize_password(password, new=False)
        except OwnerAuthError:
            if await self.is_enrolled():
                await self._record_failure()
            return None
        row = await self._credential_row()
        record = str(row[4]) if row is not None else self._dummy_hash
        if row is not None:
            retry_after = self._retry_after(row[3])
            if retry_after:
                raise OwnerLoginThrottledError(retry_after)
        password_ok = await self._verify_password(normalized_password, record)
        username_ok = bool(
            row is not None
            and secrets.compare_digest(normalized_username.casefold(), str(row[0]).casefold())
        )
        if row is None or not password_ok or not username_ok:
            if row is not None:
                delay = await self._record_failure()
                if delay:
                    raise OwnerLoginThrottledError(delay)
            return None

        replacement = None
        if self.hasher.needs_rehash(record):
            replacement = await self._hash_password(normalized_password)
        now = self._now()
        async with transaction(self.db, self.lock):
            current = await (
                await self.db.execute(
                    "SELECT a.credential_epoch, a.locked_until, p.password_hash "
                    "FROM owner_accounts a JOIN owner_password_credentials p ON p.owner_id = a.id "
                    "WHERE a.id = 1"
                )
            ).fetchone()
            if current is None or str(current[2]) != record:
                return None
            retry_after = self._retry_after(current[1])
            if retry_after:
                raise OwnerLoginThrottledError(retry_after)
            await self.db.execute(
                "UPDATE owner_accounts SET failed_attempts = 0, locked_until = NULL WHERE id = 1"
            )
            if replacement is not None:
                await self.db.execute(
                    "UPDATE owner_password_credentials SET password_hash = ?, updated_at = ? "
                    "WHERE owner_id = 1",
                    (replacement, now.isoformat()),
                )
            session = await self._insert_session(now=now, credential_epoch=int(current[0]))
        profile = await self.profile()
        return OwnerAuthOutcome(profile, session) if profile is not None else None

    async def verify_owner_password(self, password: str) -> bool:
        """Verify the enrolled owner's password without creating a browser session.

        Offline maintenance uses this narrower primitive after acquiring the process-wide
        instance lock.  It shares the durable login throttle and opportunistic Argon2 rehash,
        but deliberately has no token/session side effect.
        """
        try:
            normalized = self._normalize_password(password, new=False)
        except OwnerAuthError:
            if await self.is_enrolled():
                await self._record_failure()
            return False
        row = await self._credential_row()
        record = str(row[4]) if row is not None else self._dummy_hash
        if row is not None:
            retry_after = self._retry_after(row[3])
            if retry_after:
                raise OwnerLoginThrottledError(retry_after)
        password_ok = await self._verify_password(normalized, record)
        if row is None or not password_ok:
            if row is not None:
                delay = await self._record_failure()
                if delay:
                    raise OwnerLoginThrottledError(delay)
            return False

        replacement = None
        if self.hasher.needs_rehash(record):
            replacement = await self._hash_password(normalized)
        now = self._now()
        async with transaction(self.db, self.lock):
            current = await (
                await self.db.execute(
                    "SELECT a.locked_until, p.password_hash FROM owner_accounts a "
                    "JOIN owner_password_credentials p ON p.owner_id = a.id WHERE a.id = 1"
                )
            ).fetchone()
            if current is None or str(current[1]) != record:
                return False
            retry_after = self._retry_after(current[0])
            if retry_after:
                raise OwnerLoginThrottledError(retry_after)
            await self.db.execute(
                "UPDATE owner_accounts SET failed_attempts = 0, locked_until = NULL WHERE id = 1"
            )
            if replacement is not None:
                await self.db.execute(
                    "UPDATE owner_password_credentials SET password_hash = ?, updated_at = ? "
                    "WHERE owner_id = 1",
                    (replacement, now.isoformat()),
                )
        return True

    async def recover(self, grant_token: str, new_password: str) -> OwnerAuthOutcome:
        if not await self.auth_grant_valid(grant_token, "recover"):
            raise OwnerGrantError("Authentication grant is invalid or expired")
        normalized = self._normalize_password(new_password, new=True)
        password_hash = await self._hash_password(normalized)
        now = self._now()
        async with transaction(self.db, self.lock):
            await self._consume_grant(grant_token, "recover", now)
            row = await (
                await self.db.execute(
                    "SELECT credential_epoch FROM owner_accounts WHERE id = 1"
                )
            ).fetchone()
            if row is None:
                raise OwnerAuthError("Owner enrollment is required")
            new_epoch = int(row[0]) + 1
            await self.db.execute(
                "UPDATE owner_accounts SET credential_epoch = ?, failed_attempts = 0, "
                "locked_until = NULL, updated_at = ? WHERE id = 1",
                (new_epoch, now.isoformat()),
            )
            await self.db.execute(
                "UPDATE owner_password_credentials SET password_hash = ?, updated_at = ? "
                "WHERE owner_id = 1",
                (password_hash, now.isoformat()),
            )
            await self.db.execute(
                "UPDATE owner_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (now.isoformat(),),
            )
            session = await self._insert_session(now=now, credential_epoch=new_epoch)
        profile = await self.profile()
        if profile is None:
            raise RuntimeError("owner recovery did not persist")
        return OwnerAuthOutcome(profile, session)

    async def validate_session(self, token: str | None) -> OwnerSessionState | None:
        if not token:
            return None
        token_hash = _digest(token)
        now = self._now()
        async with self.lock:
            row = await (
                await self.db.execute(
                    "SELECT s.owner_id, s.credential_epoch, s.last_seen_at, s.idle_expires_at, "
                    "s.absolute_expires_at, s.step_up_until, s.revoked_at, a.credential_epoch "
                    "FROM owner_sessions s JOIN owner_accounts a ON a.id = s.owner_id "
                    "WHERE s.token_hash = ?",
                    (token_hash,),
                )
            ).fetchone()
            if row is None or row[6] is not None or int(row[1]) != int(row[7]):
                return None
            last_seen = _parse_time(row[2])
            idle = _parse_time(row[3])
            absolute = _parse_time(row[4])
            step_up_until = _parse_time(row[5])
            if None in {last_seen, idle, absolute, step_up_until}:
                return None
            assert last_seen is not None and idle is not None
            assert absolute is not None and step_up_until is not None
            if now >= idle or now >= absolute:
                await self.db.execute(
                    "UPDATE owner_sessions SET revoked_at = ? "
                    "WHERE token_hash = ? AND revoked_at IS NULL",
                    (now.isoformat(), token_hash),
                )
                await self.db.commit()
                return None
            renew = now - last_seen >= dt.timedelta(hours=SESSION_TOUCH_HOURS)
            if renew:
                new_idle = min(now + dt.timedelta(days=IDLE_SESSION_DAYS), absolute)
                cursor = await self.db.execute(
                    "UPDATE owner_sessions SET last_seen_at = ?, idle_expires_at = ? "
                    "WHERE token_hash = ? AND revoked_at IS NULL AND credential_epoch = ? "
                    "AND idle_expires_at = ? AND absolute_expires_at = ?",
                    (
                        now.isoformat(),
                        new_idle.isoformat(),
                        token_hash,
                        int(row[1]),
                        str(row[3]),
                        str(row[4]),
                    ),
                )
                await self.db.commit()
                if cursor.rowcount != 1:
                    return None
                idle = new_idle
        max_age = max(1, math.ceil((min(idle, absolute) - now).total_seconds()))
        return OwnerSessionState(
            token_hash=token_hash,
            owner_id=int(row[0]),
            credential_epoch=int(row[1]),
            idle_expires_at=idle.isoformat(),
            absolute_expires_at=absolute.isoformat(),
            fresh=now < step_up_until,
            renew_cookie=renew,
            cookie_max_age=max_age,
        )

    async def step_up(self, token: str, password: str) -> IssuedOwnerSession | None:
        state = await self.validate_session(token)
        if state is None:
            return None
        try:
            normalized = self._normalize_password(password, new=False)
        except OwnerAuthError:
            await self._record_failure()
            return None
        credential = await self._credential_row()
        if credential is None:
            return None
        retry_after = self._retry_after(credential[3])
        if retry_after:
            raise OwnerLoginThrottledError(retry_after)
        if not await self._verify_password(normalized, str(credential[4])):
            delay = await self._record_failure()
            if delay:
                raise OwnerLoginThrottledError(delay)
            return None

        now = self._now()
        async with transaction(self.db, self.lock):
            current = await (
                await self.db.execute(
                    "SELECT credential_epoch, idle_expires_at, absolute_expires_at, revoked_at "
                    "FROM owner_sessions WHERE token_hash = ?",
                    (_digest(token),),
                )
            ).fetchone()
            if (
                current is None
                or current[3] is not None
                or int(current[0]) != state.credential_epoch
            ):
                return None
            idle = _parse_time(current[1])
            absolute = _parse_time(current[2])
            if idle is None or absolute is None or now >= idle or now >= absolute:
                return None
            await self.db.execute(
                "UPDATE owner_sessions SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (now.isoformat(), _digest(token)),
            )
            await self.db.execute(
                "UPDATE owner_accounts SET failed_attempts = 0, locked_until = NULL WHERE id = 1"
            )
            return await self._insert_session(
                now=now,
                credential_epoch=state.credential_epoch,
                absolute_cap=absolute,
                stepped_up=True,
            )

    async def revoke_session(self, token: str) -> bool:
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE owner_sessions SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (self._now().isoformat(), _digest(token)),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def revoke_all_sessions(self) -> int:
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE owner_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (self._now().isoformat(),),
            )
            await self.db.commit()
        return max(cursor.rowcount, 0)
