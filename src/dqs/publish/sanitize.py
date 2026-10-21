"""Sanitization for text that reaches a published PR/issue (§12).

Why: a PR body mixes trusted template text with model prose, source-data
samples and dbt logs. All three can contain hostile text. GitHub renders
Markdown, resolves `@mentions` and links `#123` issue references, so an
injected string could ping a person, cross-link an unrelated issue, or hide a
phishing link behind link syntax. This module is the single place that makes
untrusted text inert before it is embedded in a document.

The transformations are deliberately deterministic and lossy: redact known
secrets and token shapes, neutralize mentions/issue refs with a zero-width
space (visually identical, not parseable as a mention/link), escape HTML and
Markdown link brackets, then truncate to a hard character cap. There is no
"trusted" bypass.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from dqs.agents.llm import redact_secrets

MAX_INLINE_CHARS = 1_200
MAX_BLOCK_CHARS = 1_500
MAX_DOCUMENT_CHARS = 60_000
TRUNCATION_MARKER = " …[truncated]"

# `@name` is neutralized unless it is part of an email/identifier. A zero-width
# space keeps the rendered text readable while stopping GitHub's mention parser.
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.+@-])@([A-Za-z0-9][A-Za-z0-9_-]*)")
# `#123` is an issue/PR reference; headings (`##`) and URL fragments are left
# alone because the preceding character is not a boundary.
_ISSUE_REF_RE = re.compile(r"(?<![\w#&])#(\d{1,7})\b")
_ZERO_WIDTH_SPACE = "\u200b"


def truncate(text: str, limit: int) -> str:
    """Hard-cap a string, keeping a marker that says it was cut."""
    if limit <= 0:
        raise ValueError("truncation limit must be positive")
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:limit]
    return text[: limit - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER


def _neutralize(text: str) -> str:
    text = _MENTION_RE.sub(lambda match: f"@{_ZERO_WIDTH_SPACE}{match.group(1)}", text)
    return _ISSUE_REF_RE.sub(lambda match: f"#{_ZERO_WIDTH_SPACE}{match.group(1)}", text)


def _escape_markdown_links(text: str) -> str:
    # Escape the `](` boundary first (before brackets gain backslashes), then
    # every bracket. `[label](url)` becomes `\[label\]\(url)` and can no
    # longer be parsed as a link or image.
    text = text.replace("](", "]\\(")
    return text.replace("[", r"\[").replace("]", r"\]")


def _escape_html(text: str) -> str:
    # quote=False keeps prose readable; angle brackets and ampersands are the
    # characters that can start an HTML tag/entity.
    return html.escape(text, quote=False)


def _clean(value: str, *, secrets: Sequence[str]) -> str:
    redacted = redact_secrets(str(value), secrets=list(secrets))
    return _escape_html(_escape_markdown_links(_neutralize(redacted)))


def sanitize_text(value: str, *, limit: int = MAX_INLINE_CHARS, secrets: Sequence[str] = ()) -> str:
    """Sanitize one inline field (a heading value, table cell, bullet)."""
    return truncate(_clean(value, secrets=secrets), limit)


def sanitize_block(value: str, *, limit: int = MAX_BLOCK_CHARS, secrets: Sequence[str] = ()) -> str:
    """Sanitize a multi-line field that will live inside a fenced code block."""
    return truncate(_clean(value, secrets=secrets), limit)


def sanitize_document(
    markdown: str, *, limit: int = MAX_DOCUMENT_CHARS, secrets: Sequence[str] = ()
) -> str:
    """Sanitize a complete Markdown document (the issue body).

    The document's own structure (headings, tables, lists) is preserved; only
    untrusted characters are neutralized. The publisher never accepts raw
    HTML, so escaping the whole document is safe and closes the `<script>` /
    `<img onerror=...>` class of injection.
    """
    return truncate(_clean(markdown, secrets=secrets), limit)


def fenced(text: str, *, language: str = "text") -> str:
    """Wrap already-sanitized text in a fence that hostile text cannot close."""
    body = text.replace("```", "'''")
    return f"```{language}\n{body}\n```"


__all__ = [
    "MAX_BLOCK_CHARS",
    "MAX_DOCUMENT_CHARS",
    "MAX_INLINE_CHARS",
    "TRUNCATION_MARKER",
    "fenced",
    "sanitize_block",
    "sanitize_document",
    "sanitize_text",
    "truncate",
]
