"""NoticeBoard: background events finally reach the browser (Phase 9 Task 5).

The BackgroundRunner's ``notify`` was console-only, so a job finishing or a reminder firing was
invisible in the UI. The board is a small bounded, *process-local* ring; each project-scoped post
is delivered only to live WebSocket workspaces at the same project scope and is readable through
the matching ``GET /api/notices`` view. It is deliberately current-session activity, not durable
history. ``post`` is synchronous (the runner's ``Notify`` is sync) and never raises off-loop — in
a unit context with no running loop it simply skips delivery.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final

_ALL_PROJECTS: Final = object()


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class NoticeBoard:
    def __init__(
        self,
        *,
        maxlen: int = 200,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
        publish: Callable[[int | None, dict], Awaitable[None]] | None = None,
        now: Callable[[], str] = _now_iso,
    ) -> None:
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._broadcast = broadcast
        self._publish = publish
        self._now = now

    def post(self, text: str, *, kind: str = "info", project_id: int | None = None) -> dict:
        self._seq += 1
        notice = {
            "seq": self._seq,
            "at": self._now(),
            "kind": kind,
            "text": text,
            "project_id": project_id,
        }
        self._items.append(notice)
        self._try_broadcast(notice)
        return notice

    def tail(self, n: int = 50, *, project_id: int | None | object = _ALL_PROJECTS) -> list[dict]:
        items = [
            item
            for item in self._items
            if project_id is _ALL_PROJECTS or item["project_id"] == project_id
        ]
        return items[-n:] if n > 0 else items

    def _try_broadcast(self, notice: dict) -> None:
        if self._publish is None and self._broadcast is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (unit/REPL context) — queue-only, never raise
        # Nest under "notice" so the notice's own ``kind`` (info/task) can't clobber the WS
        # envelope discriminator the client routes on (msg.kind === "notice"). Project-aware
        # production delivery is exact; the legacy broadcast seam remains for unit callers that
        # do not compose workspace-aware connections.
        envelope = {"kind": "notice", "notice": notice}
        if self._publish is not None:
            loop.create_task(self._publish(notice["project_id"], envelope))
        elif self._broadcast is not None:
            loop.create_task(self._broadcast(envelope))
