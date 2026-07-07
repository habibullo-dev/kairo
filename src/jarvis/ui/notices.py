"""NoticeBoard: background events finally reach the browser (Phase 9 Task 5).

The BackgroundRunner's ``notify`` was console-only, so a job finishing or a reminder firing was
invisible in the UI. The board is a small bounded ring the runner's notify also posts to; each
post broadcasts ``{"kind": "notice", ...}`` to live WebSocket clients (when a loop is running)
and is readable via ``GET /api/notices`` for the Daily screen. ``post`` is synchronous (the
runner's ``Notify`` is sync) and never raises off-loop — in a unit context with no running loop
it simply skips the broadcast.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections import deque
from collections.abc import Awaitable, Callable


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class NoticeBoard:
    def __init__(
        self,
        *,
        maxlen: int = 200,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
        now: Callable[[], str] = _now_iso,
    ) -> None:
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._broadcast = broadcast
        self._now = now

    def post(self, text: str, *, kind: str = "info") -> dict:
        self._seq += 1
        notice = {"seq": self._seq, "at": self._now(), "kind": kind, "text": text}
        self._items.append(notice)
        self._try_broadcast(notice)
        return notice

    def tail(self, n: int = 50) -> list[dict]:
        items = list(self._items)
        return items[-n:] if n > 0 else items

    def _try_broadcast(self, notice: dict) -> None:
        if self._broadcast is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (unit/REPL context) — queue-only, never raise
        # Nest under "notice" so the notice's own ``kind`` (info/task) can't clobber the WS
        # envelope discriminator the client routes on (msg.kind === "notice").
        loop.create_task(self._broadcast({"kind": "notice", "notice": notice}))
