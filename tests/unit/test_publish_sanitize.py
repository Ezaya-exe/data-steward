"""Unit tests for publish sanitization (Phase 6a).

Hostile strings must come out inert: no pings, no issue links, no HTML, no
Markdown links, no secrets and no unbounded size. These tests pin the exact
failure class the publisher must never leak into a PR body.
"""

from __future__ import annotations

from dqs.publish.sanitize import (
    MAX_BLOCK_CHARS,
    MAX_INLINE_CHARS,
    TRUNCATION_MARKER,
    fenced,
    sanitize_block,
    sanitize_document,
    sanitize_text,
    truncate,
)

HOSTILE = (
    "hello @octocat, see #1 and #1234; <script>alert('x')</script> "
    "[click](https://evil.example/pwn) ![img](https://evil.example/i.png) "
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"
)


def test_hostile_text_is_neutralized_redacted_and_truncated() -> None:
    result = sanitize_text(HOSTILE, limit=200)

    assert "@octocat" not in result
    assert "#1" not in result
    assert "#1234" not in result
    assert "<script>" not in result
    assert "](https://evil" not in result
    assert "ghp_" not in result
    assert "***redacted***" in result
    # The visible content is still there (zero-width separators only).
    assert "hello @" in result
    assert len(result) <= 200


def test_huge_string_is_capped_with_a_marker() -> None:
    result = sanitize_block("A" * 100_000, limit=MAX_BLOCK_CHARS)

    assert len(result) == MAX_BLOCK_CHARS
    assert result.endswith(TRUNCATION_MARKER)
    assert result.startswith("A")


def test_mentions_inside_emails_and_identifiers_survive() -> None:
    text = "mail user@example.com and @octocat and C@3"
    result = sanitize_text(text)

    assert "user@example.com" in result
    assert "@octocat" not in result


def test_issue_reference_neutralization_does_not_touch_headings() -> None:
    result = sanitize_document("## Heading\n\nsee #42 for context")

    assert result.startswith("## Heading")
    assert "#42" not in result


def test_explicit_settings_secrets_are_redacted_by_value() -> None:
    result = sanitize_text("the token is s3cr3t-value", secrets=["s3cr3t-value"])

    assert "s3cr3t-value" not in result


def test_fenced_block_cannot_be_closed_by_hostile_text() -> None:
    result = fenced("before ``` after", language="sql")

    assert result.startswith("```sql\n")
    assert result.endswith("\n```")
    assert result.count("```") == 2


def test_truncate_rejects_a_non_positive_limit() -> None:
    try:
        truncate("x", 0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("truncate accepted a zero limit")


def test_inline_default_limit_is_applied() -> None:
    assert len(sanitize_text("B" * 5000)) == MAX_INLINE_CHARS
