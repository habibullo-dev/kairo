"""Approval-first public-news PDF workflow for the allowlisted Telegram owner.

This is deliberately host-owned.  The Telegram model receives no PDF, filesystem, or delivery
tool: a deterministic intent creates an inert, exact-bound request; only a prefixed, expiring
approval capability may start one bounded public search.  Search text is rendered locally as
inert PDF text, registered as an artifact, then delivered to the already allowlisted chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

import aiosqlite
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from jarvis.persistence.db import transaction
from jarvis.tools.builtin.web import PublicSearchItem, PublicSearchResponse

_REQUEST_COLUMNS = (
    "id, query, scope, local_date, timezone, max_results, renderer_version, max_pdf_bytes, "
    "max_pages, retention, destination_hash, state, artifact_id, local_path, created_at, "
    "expires_at, resolved_at, completed_at, updated_at, error"
)
_RENDERER_VERSION = "kairo-news-brief-v1"
_MAX_RESULTS = 5
_MAX_PDF_BYTES = 8_000_000
_MAX_PAGES = 8
_MAX_PENDING_PER_HOUR = 3
_FORBIDDEN_PDF_MARKERS = (
    b"/JavaScript",
    b"/JS ",
    b"/OpenAction",
    b"/EmbeddedFiles",
    b"/Launch",
    b"/URI",
)
_NEWS_PDF_INTENT = re.compile(
    r"\b(?:news|headlines?)\b.*\b(?:pdf|report|document)\b|"
    r"\b(?:pdf|report|document)\b.*\b(?:news|headlines?)\b",
    re.IGNORECASE,
)
_TOPICS = (
    (re.compile(r"\b(?:ai|artificial intelligence)\b", re.IGNORECASE), "AI news"),
    (re.compile(r"\b(?:tech|technology)\b", re.IGNORECASE), "Technology news"),
    (re.compile(r"\b(?:business|finance|markets?)\b", re.IGNORECASE), "Business news"),
    (re.compile(r"\b(?:sport|sports)\b", re.IGNORECASE), "Sports news"),
    (re.compile(r"\b(?:world|global|international)\b", re.IGNORECASE), "World news"),
)
_FONT_LOCK = Lock()
_FONT_CACHE: tuple[str, str] | None = None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


def _token_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("ascii", errors="ignore")).hexdigest()


def _destination_hash(chat_id: str) -> str:
    return hashlib.sha256(f"telegram-owner:{chat_id}".encode()).hexdigest()


def _safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        " " if char in "\r\n\t" else char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(cleaned.split())[:limit]


@dataclass(frozen=True)
class NewsBriefRequest:
    id: int
    query: str
    scope: str
    local_date: str
    timezone: str
    max_results: int
    renderer_version: str
    max_pdf_bytes: int
    max_pages: int
    retention: str
    destination_hash: str
    state: str
    artifact_id: int | None
    local_path: str | None
    created_at: str
    expires_at: str
    resolved_at: str | None
    completed_at: str | None
    updated_at: str
    error: str | None


@dataclass(frozen=True)
class NewsBriefAuthorization:
    request: NewsBriefRequest
    approval_code: str


@dataclass(frozen=True)
class NewsBriefTokenResult:
    request: NewsBriefRequest | None
    recognized: bool


def _row_to_request(row: tuple) -> NewsBriefRequest:
    return NewsBriefRequest(*row)


def _binding(request: NewsBriefRequest) -> str:
    payload = json.dumps(
        {
            "id": request.id,
            "query": request.query,
            "scope": request.scope,
            "local_date": request.local_date,
            "timezone": request.timezone,
            "max_results": request.max_results,
            "renderer_version": request.renderer_version,
            "max_pdf_bytes": request.max_pdf_bytes,
            "max_pages": request.max_pages,
            "retention": request.retention,
            "destination_hash": request.destination_hash,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class NewsBriefStore:
    """Durable requests and namespaced, hashed, single-use approval capabilities."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self.db = db
        self.lock = lock

    async def get(self, request_id: int) -> NewsBriefRequest | None:
        row = await (
            await self.db.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM telegram_news_brief_requests WHERE id = ?",
                (request_id,),
            )
        ).fetchone()
        return _row_to_request(row) if row else None

    async def list(self, *, limit: int = 20) -> list[NewsBriefRequest]:
        rows = await (
            await self.db.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM telegram_news_brief_requests "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    async def create(
        self,
        *,
        query: str,
        scope: str,
        local_date: str,
        timezone: str,
        destination_hash: str,
        proposal_ttl_minutes: int,
        approval_ttl_minutes: int,
        now: dt.datetime | None = None,
    ) -> NewsBriefAuthorization:
        moment = now or _utc_now()
        created = _iso(moment)
        expires = _iso(moment + dt.timedelta(minutes=proposal_ttl_minutes))
        window = _iso(moment - dt.timedelta(hours=1))
        code = f"N-{secrets.token_hex(6).upper()}"
        async with transaction(self.db, self.lock):
            count_row = await (
                await self.db.execute(
                    "SELECT COUNT(*) FROM telegram_news_brief_requests WHERE created_at >= ?",
                    (window,),
                )
            ).fetchone()
            if count_row is not None and int(count_row[0]) >= _MAX_PENDING_PER_HOUR:
                raise ValueError("News PDF requests have reached their hourly limit (3).")
            active_row = await (
                await self.db.execute(
                    "SELECT COUNT(*) FROM telegram_news_brief_requests "
                    "WHERE state IN ('approved', 'running', 'rendered', 'sending')"
                )
            ).fetchone()
            if active_row is not None and int(active_row[0]) >= 1:
                raise ValueError("Another news PDF is already being prepared or delivered.")
            cursor = await self.db.execute(
                "INSERT INTO telegram_news_brief_requests "
                "(query, scope, local_date, timezone, max_results, renderer_version, "
                "max_pdf_bytes, max_pages, retention, destination_hash, state, created_at, "
                "expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'artifact', ?, "
                "'pending', ?, ?, ?)",
                (
                    query,
                    scope,
                    local_date,
                    timezone,
                    _MAX_RESULTS,
                    _RENDERER_VERSION,
                    _MAX_PDF_BYTES,
                    _MAX_PAGES,
                    destination_hash,
                    created,
                    expires,
                    created,
                ),
            )
            request_id = int(cursor.lastrowid)
            row = await (
                await self.db.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM telegram_news_brief_requests WHERE id = ?",
                    (request_id,),
                )
            ).fetchone()
            assert row is not None
            request = _row_to_request(row)
            await self.db.execute(
                "INSERT INTO telegram_news_brief_tokens "
                "(token_hash, request_id, binding_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _token_hash(code),
                    request.id,
                    _binding(request),
                    created,
                    _iso(moment + dt.timedelta(minutes=approval_ttl_minutes)),
                ),
            )
        return NewsBriefAuthorization(request=request, approval_code=code)

    async def issue_token(
        self,
        request_id: int,
        *,
        ttl_minutes: int,
        now: dt.datetime | None = None,
    ) -> NewsBriefAuthorization | None:
        moment = now or _utc_now()
        stamp = _iso(moment)
        code = f"N-{secrets.token_hex(6).upper()}"
        async with transaction(self.db, self.lock):
            row = await (
                await self.db.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM telegram_news_brief_requests WHERE id = ?",
                    (request_id,),
                )
            ).fetchone()
            request = _row_to_request(row) if row else None
            if request is None or request.state != "pending":
                return None
            if dt.datetime.fromisoformat(request.expires_at) <= moment:
                await self.db.execute(
                    "UPDATE telegram_news_brief_requests SET state = 'expired', updated_at = ? "
                    "WHERE id = ? AND state = 'pending'",
                    (stamp, request.id),
                )
                return None
            await self.db.execute(
                "UPDATE telegram_news_brief_tokens SET consumed_at = ?, resolution = 'deny' "
                "WHERE request_id = ? AND consumed_at IS NULL",
                (stamp, request.id),
            )
            await self.db.execute(
                "INSERT INTO telegram_news_brief_tokens "
                "(token_hash, request_id, binding_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _token_hash(code),
                    request.id,
                    _binding(request),
                    stamp,
                    _iso(moment + dt.timedelta(minutes=ttl_minutes)),
                ),
            )
        return NewsBriefAuthorization(request=request, approval_code=code)

    async def consume(
        self,
        code: str,
        *,
        resolution: Literal["approve", "deny"],
        now: dt.datetime | None = None,
    ) -> NewsBriefTokenResult | None:
        if not re.fullmatch(r"N-[0-9A-Fa-f]{12}", code.strip()):
            return None
        moment = now or _utc_now()
        stamp = _iso(moment)
        async with transaction(self.db, self.lock):
            row = await (
                await self.db.execute(
                    "SELECT t.id, t.binding_hash, t.expires_at, t.consumed_at, "
                    f"{', '.join(f'r.{part.strip()}' for part in _REQUEST_COLUMNS.split(','))} "
                    "FROM telegram_news_brief_tokens t "
                    "JOIN telegram_news_brief_requests r ON r.id = t.request_id "
                    "WHERE t.token_hash = ?",
                    (_token_hash(code),),
                )
            ).fetchone()
            if row is None:
                return None
            request = _row_to_request(row[4:])
            valid = (
                row[3] is None
                and dt.datetime.fromisoformat(row[2]) > moment
                and dt.datetime.fromisoformat(request.expires_at) > moment
                and request.state == "pending"
                and row[1] == _binding(request)
            )
            if not valid:
                return NewsBriefTokenResult(request=None, recognized=True)
            await self.db.execute(
                "UPDATE telegram_news_brief_tokens SET consumed_at = ?, resolution = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (stamp, resolution, row[0]),
            )
            state = "approved" if resolution == "approve" else "denied"
            await self.db.execute(
                "UPDATE telegram_news_brief_requests SET state = ?, resolved_at = ?, "
                "updated_at = ? WHERE id = ? AND state = 'pending'",
                (state, stamp, stamp, request.id),
            )
            updated = await (
                await self.db.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM telegram_news_brief_requests WHERE id = ?",
                    (request.id,),
                )
            ).fetchone()
        return NewsBriefTokenResult(
            request=_row_to_request(updated) if updated else None,
            recognized=True,
        )

    async def transition(
        self,
        request_id: int,
        *,
        expected: tuple[str, ...],
        state: str,
        artifact_id: int | None = None,
        local_path: str | None = None,
        error: str | None = None,
    ) -> bool:
        stamp = _iso(_utc_now())
        placeholders = ",".join("?" for _ in expected)
        completed = stamp if state in {"sent", "failed", "delivery_unknown", "cancelled"} else None
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE telegram_news_brief_requests SET state = ?, artifact_id = COALESCE(?, "
                "artifact_id), local_path = COALESCE(?, local_path), error = ?, completed_at = "
                "COALESCE(?, completed_at), updated_at = ? WHERE id = ? AND state IN "
                f"({placeholders})",
                (
                    state,
                    artifact_id,
                    local_path,
                    error,
                    completed,
                    stamp,
                    request_id,
                    *expected,
                ),
            )
            await self.db.commit()
        return cursor.rowcount == 1


def is_news_pdf_request(text: str) -> bool:
    value = text.strip()
    return value.casefold().startswith("/news-pdf") or bool(_NEWS_PDF_INTENT.search(value))


def _topic_from_request(text: str) -> str:
    value = text.strip()
    if value.casefold().startswith("/news-pdf"):
        topic = value[len("/news-pdf") :].strip()
        if not topic:
            return "Top news"
        topic = _safe_text(topic, 80)
        unsafe = re.search(
            r"(?:https?://|[/\\]|@|\b(?:token|password|secret|key)\b)", topic, re.I
        )
        if not topic or unsafe:
            raise ValueError("Use a short public topic after /news-pdf, without URLs or secrets.")
        return f"{topic} news"
    for pattern, topic in _TOPICS:
        if pattern.search(value):
            return topic
    return "Top news"


def render_authorization(authorization: NewsBriefAuthorization) -> str:
    request = authorization.request
    return (
        f"News brief N{request.id}\n"
        f"Coverage: {request.scope}\n"
        f"Date: {request.local_date} ({request.timezone})\n"
        f"Research: one public search, up to {request.max_results} sources\n"
        f"Output: PDF, up to {request.max_pages} pages / {request.max_pdf_bytes // 1_000_000} MB\n"
        "Retention: saved in Kairo Artifacts\n"
        "Delivery: this allowlisted Telegram chat\n\n"
        f"Approve: /approve {authorization.approval_code}\n"
        f"Deny: /deny {authorization.approval_code}\n"
        "Nothing is searched, created, or sent before approval."
    )


def _fonts() -> tuple[str, str]:
    global _FONT_CACHE
    with _FONT_LOCK:
        if _FONT_CACHE is not None:
            return _FONT_CACHE
        candidates = (
            (Path("C:/Windows/Fonts/malgun.ttf"), Path("C:/Windows/Fonts/malgunbd.ttf")),
            (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ),
            (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        )
        for regular, bold in candidates:
            if regular.is_file() and bold.is_file():
                suffix = hashlib.sha256(str(regular).encode()).hexdigest()[:8]
                regular_name, bold_name = f"KairoRegular{suffix}", f"KairoBold{suffix}"
                pdfmetrics.registerFont(TTFont(regular_name, str(regular)))
                pdfmetrics.registerFont(TTFont(bold_name, str(bold)))
                _FONT_CACHE = (regular_name, bold_name)
                return _FONT_CACHE
        _FONT_CACHE = ("Helvetica", "Helvetica-Bold")
        return _FONT_CACHE


class _CappedCanvas(canvas.Canvas):
    def __init__(self, *args, max_pages: int, **kwargs) -> None:
        self._max_pages = max_pages
        super().__init__(*args, **kwargs)

    def showPage(self) -> None:  # noqa: N802 - ReportLab API name
        if self._pageNumber > self._max_pages:
            raise ValueError(f"news PDF exceeded the {_MAX_PAGES}-page limit")
        super().showPage()


def _p(value: str) -> str:
    return html.escape(_safe_text(value, 4_000), quote=True)


def _source_label(item: PublicSearchItem) -> str:
    if not item.url:
        return "Source URL unavailable"
    try:
        parsed = urlsplit(item.url)
    except ValueError:
        return "Source URL unavailable"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "Source URL unavailable"
    return f"Source: {parsed.hostname or 'public web'} - {item.url}"


def render_news_pdf(request: NewsBriefRequest, search: PublicSearchResponse) -> bytes:
    """Render escaped public text only; no HTML, remote assets, attachments, or PDF actions."""
    if not search.results:
        raise ValueError("No usable public news sources were returned.")
    regular, bold = _fonts()
    buffer = BytesIO()
    navy = colors.HexColor("#12253F")
    blue = colors.HexColor("#2962FF")
    cyan = colors.HexColor("#32C8C6")
    pale = colors.HexColor("#F2F6FA")
    muted = colors.HexColor("#617084")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "KairoTitle",
        parent=styles["Title"],
        fontName=bold,
        fontSize=25,
        leading=30,
        textColor=navy,
        alignment=TA_LEFT,
        spaceAfter=5 * mm,
        wordWrap="CJK",
    )
    meta_style = ParagraphStyle(
        "KairoMeta",
        parent=styles["Normal"],
        fontName=regular,
        fontSize=9,
        leading=13,
        textColor=muted,
        wordWrap="CJK",
    )
    overview_style = ParagraphStyle(
        "KairoOverview",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=11,
        leading=17,
        textColor=navy,
        wordWrap="CJK",
    )
    headline_style = ParagraphStyle(
        "KairoHeadline",
        parent=styles["Heading2"],
        fontName=bold,
        fontSize=14,
        leading=19,
        textColor=navy,
        wordWrap="CJK",
        spaceAfter=2 * mm,
    )
    body_style = ParagraphStyle(
        "KairoBody",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#27384D"),
        wordWrap="CJK",
    )
    source_style = ParagraphStyle(
        "KairoSource",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=7.5,
        leading=10,
        textColor=muted,
        wordWrap="CJK",
    )

    def page_footer(pdf: canvas.Canvas, doc: SimpleDocTemplate) -> None:
        pdf.saveState()
        width, _height = A4
        pdf.setStrokeColor(colors.HexColor("#DCE4EC"))
        pdf.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        pdf.setFont(regular, 7.5)
        pdf.setFillColor(muted)
        pdf.drawString(
            18 * mm,
            10 * mm,
            "Kairo News Brief - public sources, verify developing stories",
        )
        pdf.drawRightString(width - 18 * mm, 10 * mm, f"{doc.page}")
        pdf.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
        title=f"Kairo News Brief - {request.local_date}",
        author="Kairo",
        subject="Public news brief",
        pageCompression=1,
    )
    story: list[object] = [
        Table(
            [[Paragraph("KAIRO / NEWS INTELLIGENCE", ParagraphStyle(
                "Kicker", fontName=bold, fontSize=8, leading=10, textColor=colors.white,
                alignment=TA_CENTER,
            ))]],
            colWidths=[58 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), blue),
                ("BOX", (0, 0), (-1, -1), 0, blue),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]),
        ),
        Spacer(1, 6 * mm),
        Paragraph("Today&apos;s News Brief", title_style),
        Paragraph(
            f"{_p(request.scope)} &nbsp; / &nbsp; {_p(request.local_date)} &nbsp; / &nbsp; "
            f"{len(search.results)} sourced stories",
            meta_style,
        ),
        Spacer(1, 6 * mm),
    ]
    if search.answer:
        story.extend(
            [
                Table(
                    [[
                        Paragraph(
                            f"<b>Executive snapshot</b><br/>{_p(search.answer)}",
                            overview_style,
                        )
                    ]],
                    colWidths=[A4[0] - 36 * mm],
                    style=TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), pale),
                        ("LINEBEFORE", (0, 0), (0, -1), 3, cyan),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6 * mm),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6 * mm),
                        ("TOPPADDING", (0, 0), (-1, -1), 5 * mm),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * mm),
                    ]),
                ),
                Spacer(1, 8 * mm),
            ]
        )
    for index, item in enumerate(search.results, 1):
        if index == 4 and len(search.results) >= 5:
            continuation_style = ParagraphStyle(
                "KairoContinuation",
                parent=title_style,
                fontSize=18,
                leading=22,
                spaceAfter=5 * mm,
            )
            story.extend([PageBreak(), Paragraph("More headlines", continuation_style)])
        card = [
            Paragraph(f"{index:02d} &nbsp; {_p(item.title)}", headline_style),
            Paragraph(_p(item.content) or "No summary was available from this source.", body_style),
            Spacer(1, 2.5 * mm),
            Paragraph(_p(_source_label(item)), source_style),
            Spacer(1, 4 * mm),
            HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#DCE4EC")),
            Spacer(1, 4 * mm),
        ]
        story.append(KeepTogether(card))
    story.extend(
        [
            Spacer(1, 4 * mm),
            Paragraph(
                "Prepared from a single bounded public-web search. Headlines can change quickly; "
                "open the cited source directly before acting on a developing story.",
                source_style,
            ),
        ]
    )
    doc.build(
        story,
        onFirstPage=page_footer,
        onLaterPages=page_footer,
        canvasmaker=lambda *args, **kwargs: _CappedCanvas(
            *args, max_pages=request.max_pages, **kwargs
        ),
    )
    raw = buffer.getvalue()
    if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1_024:]:
        raise ValueError("The news renderer did not produce a valid PDF.")
    if len(raw) > request.max_pdf_bytes:
        raise ValueError("The news PDF exceeded its approved size limit.")
    if any(marker in raw for marker in _FORBIDDEN_PDF_MARKERS):
        raise ValueError("The news PDF contained a forbidden interactive action.")
    return raw


SearchNews = Callable[[str, int], Awaitable[PublicSearchResponse]]
SendText = Callable[[str], Awaitable[None]]
SendDocument = Callable[[str, bytes, str], Awaitable[None]]
RegisterArtifact = Callable[[NewsBriefRequest, Path, str], Awaitable[int | None]]


class NewsBriefService:
    """Tracked state machine for exact-approved news research, rendering, and delivery."""

    def __init__(
        self,
        *,
        store: NewsBriefStore,
        search: SearchNews,
        artifact_dir: Path,
        scope: str,
        timezone: str,
        destination_chat_id: str,
        proposal_ttl_minutes: int,
        approval_ttl_minutes: int,
        register_artifact: RegisterArtifact | None = None,
    ) -> None:
        self.store = store
        self.search = search
        self.artifact_dir = artifact_dir.resolve()
        self.scope = _safe_text(scope, 120) or "Global"
        self.timezone = timezone
        self.destination_hash = _destination_hash(destination_chat_id)
        self.proposal_ttl_minutes = proposal_ttl_minutes
        self.approval_ttl_minutes = approval_ttl_minutes
        self.register_artifact = register_artifact
        self.send_text: SendText | None = None
        self.send_document: SendDocument | None = None
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def set_senders(self, *, text: SendText, document: SendDocument) -> None:
        self.send_text = text
        self.send_document = document

    async def propose(self, text: str, *, now: dt.datetime | None = None) -> str | None:
        if not is_news_pdf_request(text):
            return None
        moment = now or _utc_now()
        topic = _topic_from_request(text)
        local_date = moment.astimezone(dt.UTC).date().isoformat()
        with contextlib.suppress(Exception):
            from zoneinfo import ZoneInfo

            local_date = moment.astimezone(ZoneInfo(self.timezone)).date().isoformat()
        query = f"{topic} in {self.scope} on {local_date}"
        authorization = await self.store.create(
            query=query,
            scope=f"{topic} - {self.scope}",
            local_date=local_date,
            timezone=self.timezone,
            destination_hash=self.destination_hash,
            proposal_ttl_minutes=self.proposal_ttl_minutes,
            approval_ttl_minutes=self.approval_ttl_minutes,
            now=moment,
        )
        return render_authorization(authorization)

    async def approvals_text(self) -> str:
        blocks: list[str] = []
        for request in await self.store.list(limit=20):
            if request.state != "pending":
                continue
            authorization = await self.store.issue_token(
                request.id, ttl_minutes=self.approval_ttl_minutes
            )
            if authorization is not None:
                blocks.append(render_authorization(authorization))
            if len(blocks) >= 2:
                break
        return "\n\n---\n\n".join(blocks) if blocks else "No pending news brief approvals."

    async def jobs_text(self) -> str:
        requests = await self.store.list(limit=10)
        if not requests:
            return "No Telegram news briefs yet."
        lines = ["Telegram news briefs:"]
        for request in requests:
            lines.append(f"N{request.id} [{request.state}] {request.scope} - {request.local_date}")
        return "\n".join(lines)

    async def resolve(
        self, code: str, *, resolution: Literal["approve", "deny"]
    ) -> str | None:
        if not code.strip().upper().startswith("N-"):
            return None
        result = await self.store.consume(code, resolution=resolution)
        if result is None or not result.recognized or result.request is None:
            return "Invalid or expired news approval code. Send /approvals for a fresh code."
        request = result.request
        if resolution == "deny":
            return f"Denied news brief N{request.id}. Nothing was searched, created, or sent."
        self._spawn(request.id, self._generate(request.id))
        return (
            f"Approved news brief N{request.id}. Kairo is researching and building the PDF now; "
            "it will arrive here when ready."
        )

    async def cancel(self, value: str) -> str | None:
        match = re.fullmatch(r"N#?(\d+)", value.strip(), re.IGNORECASE)
        if match is None:
            return None
        request_id = int(match.group(1))
        task = self._tasks.pop(request_id, None)
        if task is not None:
            task.cancel()
        changed = await self.store.transition(
            request_id,
            expected=("pending", "approved", "running", "rendered"),
            state="cancelled",
            error="cancelled by owner",
        )
        return (
            f"Cancelled news brief N{request_id}."
            if changed
            else f"News brief N{request_id} is not cancellable."
        )

    def _spawn(self, request_id: int, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine, name=f"telegram-news-brief-{request_id}")
        self._tasks[request_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(request_id, None))

    async def _safe_text(self, text: str) -> None:
        if self.send_text is not None:
            with contextlib.suppress(Exception):
                await self.send_text(text)

    def _paths(self, request: NewsBriefRequest) -> tuple[Path, Path]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        filename = f"kairo-news-brief-{request.local_date}-n{request.id}.pdf"
        target = (self.artifact_dir / filename).resolve()
        if target.parent != self.artifact_dir or target.name != filename:
            raise ValueError("News artifact path escaped its managed directory.")
        return target, target.with_suffix(".pdf.tmp")

    def _existing_artifact_path(self, raw_path: str) -> Path | None:
        path = Path(raw_path).resolve()
        if path.parent != self.artifact_dir or not path.is_file() or path.is_symlink():
            return None
        return path

    @staticmethod
    def _atomic_store(target: Path, temp: Path, raw: bytes) -> None:
        if target.exists() or target.is_symlink() or temp.exists() or temp.is_symlink():
            raise ValueError("News artifact path already exists.")
        try:
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise

    async def _generate(self, request_id: int) -> None:
        target: Path | None = None
        temp: Path | None = None
        try:
            if not await self.store.transition(
                request_id, expected=("approved",), state="running"
            ):
                return
            request = await self.store.get(request_id)
            if request is None:
                return
            search = await self.search(request.query, request.max_results)
            raw = await asyncio.to_thread(render_news_pdf, request, search)
            target, temp = self._paths(request)
            await asyncio.to_thread(self._atomic_store, target, temp, raw)
            digest = hashlib.sha256(raw).hexdigest()
            artifact_id = (
                await self.register_artifact(request, target, digest)
                if self.register_artifact is not None
                else None
            )
            if not await self.store.transition(
                request.id,
                expected=("running",),
                state="rendered",
                artifact_id=artifact_id,
                local_path=str(target),
            ):
                return
            await self._deliver(request.id, raw=raw, target=target)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.store.transition(
                    request_id,
                    expected=("approved", "running"),
                    state="cancelled",
                    error="interrupted before delivery",
                )
            raise
        except Exception as exc:
            if temp is not None:
                with contextlib.suppress(OSError):
                    temp.unlink()
            if target is not None:
                current = await self.store.get(request_id)
                if current is None or current.state == "running":
                    with contextlib.suppress(OSError):
                        target.unlink()
            await self.store.transition(
                request_id,
                expected=("approved", "running", "rendered"),
                state="failed",
                error=type(exc).__name__,
            )
            await self._safe_text(
                f"News brief N{request_id} failed before delivery. Nothing was sent; retry the "
                "request when convenient."
            )

    async def _deliver(
        self, request_id: int, *, raw: bytes | None = None, target: Path | None = None
    ) -> None:
        request = await self.store.get(request_id)
        if request is None or request.state != "rendered" or request.local_path is None:
            return
        path = target or await asyncio.to_thread(
            self._existing_artifact_path, request.local_path
        )
        if path is None:
            await self.store.transition(
                request_id,
                expected=("rendered",),
                state="failed",
                error="artifact path validation failed",
            )
            return
        content = raw if raw is not None else await asyncio.to_thread(path.read_bytes)
        if (
            not content.startswith(b"%PDF-")
            or len(content) > request.max_pdf_bytes
            or any(marker in content for marker in _FORBIDDEN_PDF_MARKERS)
        ):
            await self.store.transition(
                request_id,
                expected=("rendered",),
                state="failed",
                error="artifact validation failed",
            )
            return
        if self.send_document is None:
            await self.store.transition(
                request_id,
                expected=("rendered",),
                state="failed",
                error="Telegram document sender unavailable",
            )
            return
        if not await self.store.transition(request_id, expected=("rendered",), state="sending"):
            return
        try:
            await self.send_document(
                path.name,
                content,
                f"Kairo news brief N{request.id} - {request.local_date}",
            )
        except asyncio.CancelledError:
            await self.store.transition(
                request_id,
                expected=("sending",),
                state="delivery_unknown",
                error="delivery interrupted after send began",
            )
            raise
        except Exception as exc:
            await self.store.transition(
                request_id,
                expected=("sending",),
                state="delivery_unknown",
                error=type(exc).__name__,
            )
            await self._safe_text(
                f"News brief N{request_id} was created, but Telegram delivery is uncertain. "
                "Kairo will not auto-resend it and risk a duplicate."
            )
            return
        await self.store.transition(request_id, expected=("sending",), state="sent")

    async def start(self) -> None:
        for request in reversed(await self.store.list(limit=100)):
            if request.state == "approved":
                self._spawn(request.id, self._generate(request.id))
            elif request.state == "rendered":
                self._spawn(request.id, self._deliver(request.id))
            elif request.state == "running":
                await self.store.transition(
                    request.id,
                    expected=("running",),
                    state="failed",
                    error="generation interrupted by restart",
                )
            elif request.state == "sending":
                await self.store.transition(
                    request.id,
                    expected=("sending",),
                    state="delivery_unknown",
                    error="delivery state was ambiguous after restart",
                )

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
