"""Bounded, deterministic verification for scheduled job final answers.

The scheduler cannot infer that a model completed useful work merely because the
provider returned ``end_turn``.  This module deliberately implements just one
auditable contract: required literal phrases in the final answer.  It is not a
claim that an external side effect happened, and it never executes a command,
evaluates a regular expression, or asks another model to judge an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

_VERSION = 1
_KIND: Literal["contains_all"] = "contains_all"
_MAX_TERMS = 8
_MAX_TERM_CHARS = 160
_MAX_TOTAL_CHARS = 640


@dataclass(frozen=True)
class VerificationContract:
    """A small expected-output check owned by a scheduled *job*.

    Terms are compared case-insensitively as literal substrings.  The stored
    representation accepts no unknown fields so future contract types cannot
    silently acquire evaluator authority on an older host.
    """

    terms: tuple[str, ...]
    kind: Literal["contains_all"] = _KIND

    @classmethod
    def contains_all(cls, values: object) -> VerificationContract:
        if not isinstance(values, (list, tuple)):
            raise ValueError("verification terms must be a list of text values")
        if not values:
            raise ValueError("verification needs at least one required phrase")
        if len(values) > _MAX_TERMS:
            raise ValueError(f"verification supports at most {_MAX_TERMS} required phrases")
        terms: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError("each verification phrase must be text")
            term = value.strip()
            if not term:
                raise ValueError("verification phrases cannot be blank")
            if len(term) > _MAX_TERM_CHARS:
                raise ValueError(
                    f"each verification phrase must be at most {_MAX_TERM_CHARS} characters"
                )
            folded = term.casefold()
            if folded in seen:
                raise ValueError("verification phrases must be distinct")
            seen.add(folded)
            terms.append(term)
        if sum(len(term) for term in terms) > _MAX_TOTAL_CHARS:
            raise ValueError(
                f"verification phrases must total at most {_MAX_TOTAL_CHARS} characters"
            )
        return cls(terms=tuple(terms))

    def to_json(self) -> str:
        return json.dumps(
            {"v": _VERSION, "kind": self.kind, "terms": list(self.terms)},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, encoded: object) -> VerificationContract | None:
        if encoded in (None, "", "{}"):
            return None
        if not isinstance(encoded, str):
            raise ValueError("invalid stored verification contract")
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid stored verification contract") from exc
        if not isinstance(value, dict) or set(value) != {"v", "kind", "terms"}:
            raise ValueError("invalid stored verification contract")
        if value["v"] != _VERSION or value["kind"] != _KIND:
            raise ValueError("unsupported stored verification contract")
        return cls.contains_all(value["terms"])


@dataclass(frozen=True)
class VerificationResult:
    """Metadata-only result retained with the task run; never includes terms or output."""

    status: Literal["passed", "failed"]
    summary: str


def verify_final_text(contract: VerificationContract, text: str) -> VerificationResult:
    """Evaluate one bounded literal contract against the final model answer."""
    answer = text.casefold()
    missing = sum(term.casefold() not in answer for term in contract.terms)
    total = len(contract.terms)
    if missing:
        return VerificationResult(
            status="failed",
            summary=f"required-output check missing {missing} of {total} phrase(s)",
        )
    return VerificationResult(
        status="passed", summary=f"required-output check matched {total} phrase(s)"
    )
