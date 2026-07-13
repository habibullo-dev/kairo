"""Composition test: Telegram remote chat has a separate, stateless zero-tool loop."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from tests.unit.test_telegram_remote import _TelegramHttp, _update

from jarvis.cli.repl import _build_telegram_remote_control
from jarvis.config import load_config
from jarvis.core import FakeClient, text_message
from jarvis.observability.cost import load_pricing
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.tools import ToolRegistry


async def test_remote_wiring_uses_utility_model_and_exposes_no_tools_or_history(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.remote_control.enabled = True
    config.connectors.telegram.remote_control.allowed_chat_id = "123"
    config.secrets = config.secrets.model_copy(update={"telegram_bot_token": "BOT-CANARY"})
    db = await connect(tmp_path / "remote.db")
    try:
        fake = FakeClient([text_message("safe remote reply")])
        store = SessionStore(db)
        repl = SimpleNamespace(
            tasks=None,
            turn_lock=asyncio.Lock(),
            registry=ToolRegistry(),
            client=fake,
            executor=object(),
            gate=object(),
            cost_ledger=SimpleNamespace(pricing=load_pricing(Path("config/pricing.yaml"))),
        )
        controller = _build_telegram_remote_control(
            config,
            repl=repl,
            store=store,
            runner=None,
            console=Console(file=io.StringIO()),
        )
        assert controller is not None
        http = _TelegramHttp([[], [_update(1, text="what should I focus on?")]])
        await controller.poll_once(http=http)  # bootstrap only
        assert await controller.poll_once(http=http) == 1

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["model"] == config.models.utility
        assert call["tools"] == []
        assert call["messages"] == [{"role": "user", "content": "what should I focus on?"}]
        assert call["max_tokens"] == 1_200
        assert "NO tools" in call["system"]
        assert http.sent[0]["text"] == "safe remote reply"
    finally:
        await db.close()
