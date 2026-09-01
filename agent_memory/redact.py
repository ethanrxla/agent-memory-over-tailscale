"""Secret redaction.

Applied on the source device before any event is sent to the hub, so raw
credentials never reach the shared database. Patterns are ordered most
specific first; the generic assignment rule runs last so that a recognised
token type keeps its descriptive placeholder.
"""

from __future__ import annotations

import os
import re

PLACEHOLDER = "[REDACTED:{label}]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("nvidia-key", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}")),
    ("auth-header", re.compile(r"(?i)\bauthorization\s*:\s*\S+")),
    ("aws-secret", re.compile(r"(?i)\baws_secret_access_key\s*[:=]\s*\S+")),
    # Generic KEY=VALUE / KEY: VALUE assignments (.env bodies, config dumps).
    # The keyword is matched as part of an identifier rather than as a whole
    # word, so DB_PASSWORD=, MY_API_KEY= and stripe.secret_key= all match --
    # a \b before "password" fails against the underscore in DB_PASSWORD.
    ("credential", re.compile(
        r"(?i)[A-Za-z0-9_.\-]*"
        r"(?:password|passwd|pwd|secret|api[_\-]?key|access[_\-]?token|"
        r"auth[_\-]?token|client[_\-]?secret|private[_\-]?key|shared[_\-]?key|"
        r"credential|passphrase)"
        r"[A-Za-z0-9_.\-]*"
        r"\s*[:=]\s*[\"']?([^\s\"',;]{6,})[\"']?"
    )),
]


def _extra_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Extra patterns from AGENT_MEMORY_REDACT_EXTRA (newline or ';;' separated)."""
    raw = os.getenv("AGENT_MEMORY_REDACT_EXTRA", "")
    if not raw.strip():
        return []
    parts = [p.strip() for p in re.split(r";;|\n", raw) if p.strip()]
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for part in parts:
        try:
            compiled.append(("custom", re.compile(part)))
        except re.error:
            continue
    return compiled


def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions)."""
    if not text:
        return text, 0

    count = 0
    result = text
    for label, pattern in _PATTERNS + _extra_patterns():
        if label == "credential":
            def _sub_credential(match: re.Match[str]) -> str:
                nonlocal count
                count += 1
                whole = match.group(0)
                value = match.group(1)
                return whole.replace(value, PLACEHOLDER.format(label="credential"))

            result, _ = pattern.subn(_sub_credential, result)
        else:
            def _sub(match: re.Match[str], _label: str = label) -> str:
                nonlocal count
                count += 1
                return PLACEHOLDER.format(label=_label)

            result, _ = pattern.subn(_sub, result)
    return result, count


def redact_text(text: str) -> str:
    return redact(text)[0]


def path_is_denied(path: str, deny: list[str]) -> bool:
    """True when a transcript path falls under a configured deny-listed root."""
    if not deny:
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
    except OSError:
        resolved = path
    for root in deny:
        root = root.strip()
        if not root:
            continue
        try:
            root_resolved = os.path.realpath(os.path.expanduser(root))
        except OSError:
            root_resolved = root
        if resolved == root_resolved or resolved.startswith(root_resolved.rstrip("/") + "/"):
            return True
    return False
