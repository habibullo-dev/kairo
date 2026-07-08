"""OpenAI image generation adapter — the ``generate_image`` tool (Phase 13 Task 6). Keyless: an
injected ``httpx.MockTransport`` returns a fake base64 image (no network, no key). Pins the
request shape, that the PNG lands ONLY under the managed artifacts root and is registered as an
UNTRUSTED, model-generated artifact (never executed/committed), the framed result, the per-image
cost row, and that the tool is unavailable without the artifact store."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.config import load_config
from jarvis.observability.ledger import ServiceLedger
from jarvis.persistence.artifacts import ArtifactStore
from jarvis.persistence.db import connect
from jarvis.services.image_gen import GenerateImageTool
from jarvis.tools.base import ToolContext

_OPEN: list = []
_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_PNG = b"\x89PNG\r\n\x1a\n-fake-image-bytes"


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    GenerateImageTool.transport = None


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


def _write_pricing(tmp_path: Path, *, image: bool = True) -> None:
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(exist_ok=True)
    services = "  openai_image: {unit: image, usd_per_unit: 0.07}\n" if image else ""
    (cfgdir / "pricing.yaml").write_text(
        "schema_version: test\n"
        "models:\n"
        "  anthropic:\n"
        "    claude-opus-4-8: {input: 5.0, output: 25.0}\n"
        "services:\n" + services,
        encoding="utf-8",
    )


def _cfg(tmp_path: Path, *, enabled=("openai_image",), key="oai-key", priced=True):
    _write_pricing(tmp_path, image=priced)
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = list(enabled)
    cfg.secrets = cfg.secrets.model_copy(update={"openai_api_key": key})
    return cfg


async def _store(cfg) -> ArtifactStore:
    db = await connect(cfg.data_dir / "art.db")
    _OPEN.append(db)
    return ArtifactStore(
        db, asyncio.Lock(), data_dir=cfg.data_dir,
        managed_roots={"artifacts": cfg.data_dir / "artifacts"},
    )


def _handler(response: httpx.Response, captured: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return response

    return handle


def _ok(png: bytes = _PNG) -> httpx.Response:
    return httpx.Response(
        200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]}
    )


async def _run(cfg, store, handler, *, ledger=None, prompt="a blue login screen mockup"):
    GenerateImageTool.transport = httpx.MockTransport(handler)
    ctx = ToolContext(config=cfg, service_ledger=ledger, artifacts=store)
    tool = GenerateImageTool(ctx)
    return await tool.run(tool.Params(prompt=prompt))


# --- availability (needs flag ∧ key ∧ pricing ∧ artifact store) -------------


async def test_available_requires_store_and_registry(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = await _store(cfg)
    assert GenerateImageTool.is_available(ToolContext(config=cfg, artifacts=store))
    assert not GenerateImageTool.is_available(ToolContext(config=cfg, artifacts=None))  # no store
    assert not GenerateImageTool.is_available(
        ToolContext(config=_cfg(tmp_path, enabled=()), artifacts=store)  # flag off
    )
    assert not GenerateImageTool.is_available(
        ToolContext(config=_cfg(tmp_path, key=""), artifacts=store)  # no key
    )
    assert not GenerateImageTool.is_available(
        ToolContext(config=_cfg(tmp_path, priced=False), artifacts=store)  # unpriced
    )


# --- request shape ----------------------------------------------------------


async def test_request_shape(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    captured: dict = {}
    await _run(cfg, await _store(cfg), _handler(_ok(), captured), prompt="hero banner")
    req = captured["request"]
    assert req.method == "POST" and str(req.url) == _IMAGES_URL
    assert req.headers["Authorization"] == "Bearer oai-key"
    body = json.loads(req.content)
    assert body["model"] == "gpt-image-1" and body["prompt"] == "hero banner"
    assert body["size"] == "1024x1024" and body["n"] == 1


# --- the PNG is saved under the managed root + registered untrusted ----------


async def test_saves_png_and_registers_untrusted_artifact(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = await _store(cfg)
    out = await _run(cfg, store, _handler(_ok(), {}))
    assert not out.is_error

    arts = await store.list()
    assert len(arts) == 1
    a = arts[0]
    assert a.kind == "design" and a.origin_type == "openai_image"
    assert a.created_by == "agent" and a.model == "gpt-image-1"
    assert a.provenance_class == "untrusted_model_generated"  # never trusted
    # the file exists, under the managed data/artifacts root, and holds the decoded bytes
    path = store.content_path(a)
    assert path is not None and path.is_file()
    assert path.read_bytes() == _PNG
    assert (cfg.data_dir / "artifacts") in path.parents


async def test_result_text_is_framed_and_calls_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    out = await _run(cfg, await _store(cfg), _handler(_ok(), {}))
    # result is framed untrusted_model_generated and is a REFERENCE — no image bytes, no exec.
    assert out.content.startswith("--- begin untrusted_model_generated")
    assert "not executed" in out.content and "Library" in out.content


# --- cost + errors ----------------------------------------------------------


async def test_records_one_image_with_real_cost(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = await _store(cfg)
    db = await connect(tmp_path / "svc.db")
    _OPEN.append(db)
    ledger = ServiceLedger(db, asyncio.Lock(), "test")
    await _run(cfg, store, _handler(_ok(), {}), ledger=ledger)
    cur = await db.execute("SELECT service, operation, units, est_cost_usd FROM service_calls")
    assert await cur.fetchall() == [("openai_image", "generate", 1.0, 0.07)]


async def test_4xx_is_friendly_never_the_body(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    resp = httpx.Response(400, json={"error": {"message": "moderation LEAK_DETAIL_XYZ"}})
    out = await _run(cfg, await _store(cfg), _handler(resp, {}))
    assert out.is_error and "HTTP 400" in out.content
    assert "LEAK_DETAIL_XYZ" not in out.content
    assert await (await _store(cfg)).list() == []  # nothing registered on failure


async def test_no_image_in_response_is_error(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = await _store(cfg)
    out = await _run(cfg, store, _handler(httpx.Response(200, json={"data": []}), {}))
    assert out.is_error and "no image" in out.content
    assert await store.list() == []  # nothing saved/registered
