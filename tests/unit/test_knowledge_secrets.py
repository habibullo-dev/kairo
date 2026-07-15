"""High-confidence secret redaction never returns the matched value."""

from __future__ import annotations

import dataclasses

import pytest

from kira.knowledge.secrets import scan_text


def _metadata(result) -> str:
    return repr(dataclasses.asdict(result))


def test_provider_tokens_are_redacted_without_value_retention() -> None:
    anthropic = "sk-ant-" + "a" * 40
    github = "ghp_" + "B" * 36
    aws = "AKIA" + "C" * 16
    result = scan_text(f"one {anthropic}\ntwo {github}\nthree {aws}")
    assert result.total_hits == 3 and not result.truncated
    assert [(hit.rule, hit.line) for hit in result.hits] == [
        ("anthropic_key", 1),
        ("github_token", 2),
        ("aws_access_key", 3),
    ]
    for secret in (anthropic, github, aws):
        assert secret not in result.redacted_text
        assert secret not in _metadata(result)


def test_literal_credential_assignments_redact_but_placeholders_and_code_do_not() -> None:
    text = """\
api_key: realvalue123456789
password = \"correct-horse-battery-staple\"
client_secret: your_secret
access_token = os.getenv(\"TOKEN\")
api_key: ${API_KEY}
"""
    result = scan_text(text)
    assert result.total_hits == 2
    assert "realvalue123456789" not in result.redacted_text
    assert "correct-horse-battery-staple" not in result.redacted_text
    assert "your_secret" in result.redacted_text
    assert 'os.getenv("TOKEN")' in result.redacted_text
    assert "${API_KEY}" in result.redacted_text


def test_private_key_block_is_removed_and_line_positions_remain_stable() -> None:
    private = """-----BEGIN PRIVATE KEY-----
very-secret-line
another-secret-line
-----END PRIVATE KEY-----"""
    token = "sk-proj-" + "z" * 30
    result = scan_text(f"header\n{private}\n{token}\nfooter")
    assert "very-secret-line" not in result.redacted_text
    assert "another-secret-line" not in result.redacted_text
    assert [(hit.rule, hit.line) for hit in result.hits] == [
        ("private_key", 2),
        ("openai_key", 6),
    ]
    assert result.redacted_text.count("\n") == 6


def test_hit_metadata_is_capped_but_all_values_are_redacted() -> None:
    values = ["ghp_" + char * 36 for char in "ABC"]
    result = scan_text("\n".join(values), max_hits=2)
    assert result.total_hits == 3 and result.truncated
    assert len(result.hits) == 2
    assert all(value not in result.redacted_text for value in values)


def test_repeated_unterminated_private_key_markers_have_bounded_scans() -> None:
    text = "\n".join("-----BEGIN PRIVATE KEY-----" for _ in range(2_000))
    result = scan_text(text)
    assert result.redacted_text == text
    assert result.total_hits == 0


def test_nonpositive_hit_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        scan_text("safe", max_hits=0)
