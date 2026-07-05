"""Trigger tests: table-driven next-fire math, DST, validation messages."""

from __future__ import annotations

import datetime as dt

import pytest

from jarvis.scheduler.triggers import compute_next, validate

UTC = dt.UTC


def aware(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


# --- validate ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "spec", "tz", "fragment"),
    [
        ("bogus", "x", "UTC", "unknown schedule kind"),
        ("once", "not-a-date", "UTC", "ISO-8601"),
        ("once", "2026-07-07T09:00:00", "Mars/Olympus", "unknown timezone"),
        ("cron", "not a cron", "UTC", "invalid cron"),
        ("cron", "0 9 * *", "UTC", "invalid cron"),  # 4 fields
        ("interval", "abc", "UTC", "whole number"),
        ("interval", "59", "UTC", "at least 60"),
    ],
)
def test_validate_rejects_with_readable_message(
    kind: str, spec: str, tz: str, fragment: str
) -> None:
    error = validate(kind, spec, tz)
    assert error is not None and fragment in error


@pytest.mark.parametrize(
    ("kind", "spec", "tz"),
    [
        ("once", "2026-07-07T09:00:00", "Europe/Berlin"),  # naive: task's own zone
        ("once", "2026-07-07T09:00:00+02:00", "UTC"),
        ("cron", "0 9 * * *", "America/New_York"),
        ("cron", "*/15 * * * 1-5", "UTC"),
        ("interval", "60", "UTC"),
        ("interval", "3600", "Asia/Tokyo"),
    ],
)
def test_validate_accepts_good_specs(kind: str, spec: str, tz: str) -> None:
    assert validate(kind, spec, tz) is None


# --- compute_next ------------------------------------------------------------


def test_once_future_returns_utc_instant() -> None:
    fire = compute_next("once", "2026-07-07T09:00:00+02:00", "UTC", after=aware(2026, 7, 6))
    assert fire == aware(2026, 7, 7, 7, 0)  # 09:00+02:00 == 07:00Z
    assert fire.tzinfo == UTC


def test_once_naive_spec_means_task_timezone() -> None:
    # 09:00 naive in Berlin (UTC+2 in July) == 07:00 UTC
    fire = compute_next("once", "2026-07-07T09:00:00", "Europe/Berlin", after=aware(2026, 7, 6))
    assert fire == aware(2026, 7, 7, 7, 0)


def test_once_in_past_returns_none() -> None:
    spec = "2026-07-06T09:00:00+00:00"
    assert compute_next("once", spec, "UTC", after=aware(2026, 7, 6, 10)) is None
    # exactly-at-after is not strictly after -> None
    assert compute_next("once", spec, "UTC", after=aware(2026, 7, 6, 9)) is None


def test_cron_next_fire_in_utc() -> None:
    fire = compute_next("cron", "0 9 * * *", "UTC", after=aware(2026, 7, 6, 8))
    assert fire == aware(2026, 7, 6, 9)
    # past today's 09:00 -> tomorrow's
    fire = compute_next("cron", "0 9 * * *", "UTC", after=aware(2026, 7, 6, 9, 30))
    assert fire == aware(2026, 7, 7, 9)


def test_cron_is_strictly_after() -> None:
    # advancing from the fire time itself must not return the same instant
    fire = compute_next("cron", "0 9 * * *", "UTC", after=aware(2026, 7, 6, 9))
    assert fire == aware(2026, 7, 7, 9)


def test_cron_evaluated_in_task_timezone() -> None:
    # 09:00 in New York (EDT, UTC-4 in July) == 13:00 UTC
    fire = compute_next("cron", "0 9 * * *", "America/New_York", after=aware(2026, 7, 6, 12))
    assert fire == aware(2026, 7, 6, 13)


def test_cron_across_dst_spring_forward() -> None:
    # US spring-forward: Sun 2026-03-08, clocks 02:00 EST -> 03:00 EDT.
    # Noon cron: Sat noon is 17:00Z (EST, UTC-5); Sun noon is 16:00Z (EDT, UTC-4).
    sat_noon = compute_next("cron", "0 12 * * *", "America/New_York", after=aware(2026, 3, 7))
    assert sat_noon == aware(2026, 3, 7, 17)
    sun_noon = compute_next("cron", "0 12 * * *", "America/New_York", after=sat_noon)
    assert sun_noon == aware(2026, 3, 8, 16)  # same wall-clock time, new UTC offset


def test_interval_advances_from_scheduled_time_not_completion() -> None:
    # anchored to `after` (the serviced fire time): run duration can't drift cadence
    fire = compute_next("interval", "3600", "UTC", after=aware(2026, 7, 6, 9))
    assert fire == aware(2026, 7, 6, 10)


def test_compute_next_requires_aware_after() -> None:
    with pytest.raises(ValueError, match="aware"):
        compute_next("once", "2026-07-07T09:00:00", "UTC", after=dt.datetime(2026, 7, 6))  # noqa: DTZ001
