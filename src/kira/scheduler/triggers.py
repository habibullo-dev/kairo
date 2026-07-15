"""Pure next-fire computation over APScheduler's trigger classes.

This is the only module that imports APScheduler. We deliberately do NOT run its
scheduler (`AsyncIOScheduler` would be a second source of scheduling truth to
keep consistent with SQLite forever); we take exactly the hard part — cron/DST
math — behind two pure functions the rest of the phase can table-test:

* :func:`validate` — shape-check a schedule spec, returning a human-readable
  error (surfaced to the model as a tool error, so it must be actionable).
* :func:`compute_next` — the next fire time *strictly after* ``after``.
  Strictness matters: the service advances a task by passing the fire time it
  just serviced; a >= comparison would return the same instant forever.

Everything is timezone-aware end to end: ``after`` must be aware, returns are
aware UTC (the storage convention), and cron is evaluated in the task's own IANA
zone — "9am daily" is human intent in a place, not a UTC offset.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

#: Floor for interval tasks — a model-authored "every 5 seconds" job would burn a
#: model call per interval forever; sub-minute cadence is never what a human meant.
MIN_INTERVAL_SECONDS = 60

_KINDS = ("once", "cron", "interval")


def _zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)  # raises for unknown zones; validate() turns that into text


def _parse_once(spec: str, zone: ZoneInfo) -> _dt.datetime:
    """Parse an ISO-8601 instant; a naive value means the task's own timezone."""
    parsed = _dt.datetime.fromisoformat(spec)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def validate(schedule_kind: str, spec: str, tz: str) -> str | None:
    """Return a human-readable problem with the spec, or None if it's usable."""
    if schedule_kind not in _KINDS:
        return f"unknown schedule kind {schedule_kind!r} (expected one of {', '.join(_KINDS)})"
    try:
        zone = _zone(tz)
    except Exception:
        return f"unknown timezone {tz!r} (expected an IANA zone like 'Europe/Berlin')"
    if schedule_kind == "once":
        try:
            _parse_once(spec, zone)
        except ValueError:
            return f"{spec!r} is not a valid ISO-8601 datetime (e.g. '2026-07-07T09:00:00')"
        return None
    if schedule_kind == "cron":
        try:
            CronTrigger.from_crontab(spec, timezone=zone)
        except ValueError as exc:
            return f"invalid cron expression {spec!r}: {exc}"
        return None
    # interval
    try:
        seconds = int(spec)
    except ValueError:
        return f"interval must be a whole number of seconds, got {spec!r}"
    if seconds < MIN_INTERVAL_SECONDS:
        return f"interval must be at least {MIN_INTERVAL_SECONDS} seconds, got {seconds}"
    return None


def compute_next(
    schedule_kind: str, spec: str, tz: str, *, after: _dt.datetime
) -> _dt.datetime | None:
    """The next fire time strictly after ``after``, as an aware-UTC datetime.

    Returns None when the schedule has no future occurrence (a ``once`` whose
    instant has passed). ``after`` must be timezone-aware; specs are assumed to
    have passed :func:`validate` (invalid ones raise).
    """
    if after.tzinfo is None:
        raise ValueError("compute_next requires an aware 'after' datetime")
    zone = _zone(tz)

    if schedule_kind == "once":
        instant = _parse_once(spec, zone)
        return instant.astimezone(_dt.UTC) if instant > after else None

    if schedule_kind == "cron":
        trigger = CronTrigger.from_crontab(spec, timezone=zone)
        # CronTrigger's next fire is >= now; nudge past 'after' for strictness.
        now_local = (after + _dt.timedelta(microseconds=1)).astimezone(zone)
        fire = trigger.get_next_fire_time(None, now_local)
        return fire.astimezone(_dt.UTC) if fire is not None else None

    if schedule_kind == "interval":
        trigger = IntervalTrigger(seconds=int(spec), timezone=zone)
        # With a previous fire time, IntervalTrigger returns previous + interval —
        # anchored to the *scheduled* time, so run duration never drifts the cadence.
        fire = trigger.get_next_fire_time(after.astimezone(zone), after.astimezone(zone))
        return fire.astimezone(_dt.UTC) if fire is not None else None

    raise ValueError(f"unknown schedule kind {schedule_kind!r}")
