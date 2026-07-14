"""High-confidence secret redaction for project knowledge sent to cloud models.

This is a deliberately small preflight, not a claim of full DLP.  Matches retain only a rule name
and line number; the suspected value is never returned, logged, or persisted.  Callers receive a
same-purpose redacted string suitable for embedding/model context and honest coverage metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = "[REDACTED_SECRET:{rule}]"

# Values with provider-specific structure are high-confidence enough to redact wherever they
# occur.  Keep these expressions linear and bounded; project uploads can be large.
_TOKEN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>(?:(?:RSA|EC|DSA|OPENSSH) )?PRIVATE KEY)-----"
    # Real PEM private keys are only a few KiB.  A bound keeps repeated unterminated BEGIN
    # markers in untrusted uploads from causing quadratic scan-to-EOF backtracking.
    r".{0,8192}?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)

# Literal assignments catch private keys that do not have a provider-specific prefix.  The value
# grammar intentionally excludes code expressions such as ``os.getenv(...)`` and object access.
_ASSIGNMENT = re.compile(
    r"(?im)(?P<prefix>"
    r"(?P<keyquote>['\"]?)"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key|"
    r"password|passwd)"
    r"(?P=keyquote)\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[A-Za-z0-9_./+=:@-]{8,})"
    r"(?P=quote)"
    r"(?![A-Za-z0-9_./+=:@-])"
)

_PLACEHOLDERS = {
    "changeme",
    "dummy",
    "example",
    "fake",
    "none",
    "null",
    "password",
    "placeholder",
    "redacted",
    "replace-me",
    "replace_me",
    "test",
    "your-api-key",
    "your_api_key",
    "your-secret",
    "your_secret",
}


@dataclass(frozen=True)
class SecretHit:
    rule: str
    line: int


@dataclass(frozen=True)
class SecretScanResult:
    redacted_text: str
    hits: tuple[SecretHit, ...]
    total_hits: int
    truncated: bool

    @property
    def suspected(self) -> bool:
        return self.total_hits > 0


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _PLACEHOLDERS:
        return True
    return (
        lowered.startswith(("${", "$env", "process.env", "os.getenv", "env[", "{{"))
        or lowered.endswith(("_here", "-here"))
        or "example.com" in lowered
    )


def scan_text(text: str, *, max_hits: int = 1_000) -> SecretScanResult:
    """Redact high-confidence secret literals without retaining their values.

    ``max_hits`` bounds returned metadata only; every recognized value is still redacted.  A
    non-positive cap is rejected so callers cannot accidentally request redaction without any
    evidence that it occurred.
    """

    if max_hits <= 0:
        raise ValueError("max_hits must be positive")
    hits: list[SecretHit] = []
    total = 0

    def record(rule: str, source: str, offset: int) -> None:
        nonlocal total
        total += 1
        if len(hits) < max_hits:
            hits.append(SecretHit(rule=rule, line=_line(source, offset)))

    def private_replacement(match: re.Match[str]) -> str:
        record("private_key", text, match.start())
        # Preserve the number of lines so later evidence line numbers remain useful.
        return _MARKER.format(rule="private_key") + ("\n" * match.group(0).count("\n"))

    redacted = _PRIVATE_KEY.sub(private_replacement, text)

    for rule, pattern in _TOKEN_RULES:
        source = redacted

        def token_replacement(
            match: re.Match[str], *, _rule: str = rule, _source: str = source
        ) -> str:
            record(_rule, _source, match.start())
            return _MARKER.format(rule=_rule)

        redacted = pattern.sub(token_replacement, redacted)

    source = redacted

    def assignment_replacement(match: re.Match[str]) -> str:
        value = match.group("value")
        if _is_placeholder(value) or value.startswith("[REDACTED_SECRET:"):
            return match.group(0)
        record("credential_assignment", source, match.start("value"))
        quote = match.group("quote") or ""
        marker = _MARKER.format(rule="credential_assignment")
        return f"{match.group('prefix')}{quote}{marker}{quote}"

    redacted = _ASSIGNMENT.sub(assignment_replacement, redacted)
    return SecretScanResult(
        redacted_text=redacted,
        hits=tuple(hits),
        total_hits=total,
        truncated=total > len(hits),
    )
