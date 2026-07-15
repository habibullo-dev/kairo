"""OpenAI image generation adapter — the ``generate_image`` tool (Phase 13 Task 6).

A prompt -> a PNG, saved under the managed ``data/artifacts`` root and REGISTERED as an artifact
(kind ``design``, origin_type ``openai_image``, created_by ``agent``, provenance
``untrusted_model_generated``). The image is DATA: it is never executed, committed, or applied
automatically — a human reviews it in the Library. Policy (egress, ASK, execution-only,
untrusted_model_generated) is DERIVED from the ServiceSpec. The result TEXT is framed untrusted;
the file lands only under the managed root (ArtifactStore.register path-confines + refuses
sensitive paths); a metadata-only ``service_calls`` row records units=1 image at the per-image
rate from pricing.yaml. The tool is unavailable unless the artifact store is composed — its whole
output IS a registered artifact.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from pathlib import Path

from pydantic import BaseModel, Field

from kira.observability import get_logger, log_egress
from kira.services.tooling import HttpServiceTool, ServiceHttpError, frame_output
from kira.tools.base import ToolContext, ToolResult

_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_MODEL = "gpt-image-1"
_SIZE = "1024x1024"
_QUALITY = "medium"
_log = get_logger("kira.services.image_gen")


class GenerateImageParams(BaseModel):
    prompt: str = Field(
        description="Describe the image to generate (e.g. a UI mockup, an illustration, an asset)."
    )


class GenerateImageTool(HttpServiceTool):
    service_name = "openai_image"
    name = "generate_image"
    description = (
        "Generate an image (design mockup, illustration, asset) from a text prompt via OpenAI. "
        "The image is saved to the Library as an UNTRUSTED, model-generated artifact — it is "
        "never executed, committed, or applied automatically; a human reviews it. Returns the "
        "artifact reference, not the image bytes."
    )
    Params = GenerateImageParams
    http_timeout = 120.0  # image generation is slower than a fetch/search

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        # In addition to the registry gate (flag ∧ key ∧ pricing), the artifact store must be
        # composed — the tool's entire output is a registered artifact.
        return super().is_available(context) and getattr(context, "artifacts", None) is not None

    def _project_id(self) -> int | None:
        provider = getattr(self.context, "project", None)
        return provider().project_id if provider is not None else None

    async def run(self, params: GenerateImageParams) -> ToolResult:  # type: ignore[override]
        key = self._api_key()
        if not key:
            return ToolResult(
                content="generate_image is not configured (set OPENAI_API_KEY).", is_error=True
            )
        store = getattr(self.context, "artifacts", None)
        if store is None:  # defence in depth (is_available already gates this)
            return ToolResult(
                content="generate_image needs the artifact store (unavailable here).", is_error=True
            )
        refusal = await self._preflight(1)  # project narrowing + hard cost cap, BEFORE any egress
        if refusal:
            return ToolResult(content=refusal, is_error=True)
        # Egress ledger: category only — never the prompt (it is the sensitive payload).
        log_egress(category="image_generation", destination_type="public_web")
        try:
            data = await self._request_json(
                "POST",
                _IMAGES_URL,
                headers={"Authorization": f"Bearer {key}"},
                json_body={
                    "model": _MODEL,
                    "prompt": params.prompt,
                    "size": _SIZE,
                    "quality": _QUALITY,
                    "n": 1,
                },
            )
        except ServiceHttpError as exc:
            return ToolResult(content=str(exc), is_error=True)  # friendly, no provider body

        items = data.get("data") or []
        b64 = items[0].get("b64_json") if items else None
        if not b64:
            return ToolResult(content="image generation returned no image.", is_error=True)
        try:
            png = base64.b64decode(b64, validate=True)
        except (ValueError, binascii.Error):
            return ToolResult(content="image generation returned an invalid image.", is_error=True)

        digest = hashlib.sha256(png).hexdigest()[:16]
        project_id = self._project_id()
        subdir = str(project_id) if project_id is not None else "global"
        images_dir = Path(self.context.config.data_dir) / "artifacts" / "images" / subdir
        path = images_dir / f"img_{digest}.png"
        await asyncio.to_thread(_write_png, path, png)
        await self._record_call("generate", units=1, est_cost_usd=self._service_cost(1))

        assert self.spec is not None
        artifact_id: int | None = None
        try:  # fail-soft: the file exists even if the DB write hiccups
            artifact_id = await store.register(
                origin_type="openai_image",
                kind="design",
                title=params.prompt[:120],
                created_by="agent",
                local_path=path,
                content_hash=digest,
                project_id=project_id,
                model=_MODEL,
                sensitivity=self.spec.sensitivity,
                provenance_class=self.spec.output_trust.value,  # untrusted_model_generated
                labels=["generated", "image"],
            )
        except Exception as exc:  # noqa: BLE001 - registration is best-effort; never crash a turn
            _log.warning("image_artifact_register_failed", error=str(exc))

        ref = f" (artifact #{artifact_id})" if artifact_id is not None else ""
        body = (
            f"Generated a {_SIZE} image with {_MODEL} and saved it to the Library{ref}. It is "
            "UNTRUSTED, model-generated content — review it before use; it is not executed, "
            "committed, or applied automatically."
        )
        return ToolResult(content=frame_output(self.spec, body))


def _write_png(path: Path, data: bytes) -> None:
    """Create the parent dir (under the managed artifacts root) and write the PNG. Sync, run in a
    thread from the async ``run`` so the event loop is never blocked on disk I/O."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
