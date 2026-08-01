"""Structured failure codes for the IO layer.

A failure carries three machine-readable signals besides the message:

``error_code``  what went wrong, from the table below
``retryable``   whether repeating the *same* call can succeed
``hint``        what to do instead when a retry is pointless

The rendered ``error`` string stays ``"<error_code>: <message>"`` so a caller
that only reads text still sees the code.
"""

from __future__ import annotations

from typing import Any

from tools.schemas import ToolReply

# --- codes -----------------------------------------------------------------

INVALID_REQUEST = "invalid_request"     # arguments unusable (empty query, bad url)
BLOCKED_URL = "blocked_url"             # url refused by the network policy
AUTH_FAILED = "auth_failed"             # missing/rejected API key
FORBIDDEN = "forbidden"                 # source refused us (403, paywall, bot wall)
NOT_FOUND = "not_found"                 # 404/410
RATE_LIMITED = "rate_limited"           # 429, or our own limiter gave up
TIMEOUT = "timeout"                     # no response in time
SERVER_ERROR = "server_error"           # 5xx
NETWORK_ERROR = "network_error"         # connection/DNS/TLS failure
TOO_MANY_REDIRECTS = "too_many_redirects"
INVALID_RESPONSE = "invalid_response"   # response was not the shape we expect
UNSUPPORTED_CONTENT = "unsupported_content"  # not html or pdf
PARSE_FAILED = "parse_failed"           # pdf/html parser could not read it
EMPTY_CONTENT = "empty_content"         # parsed fine, no body text came out
FIXTURE_MISSING = "fixture_missing"     # mock mode, deliberately unmatched

#: Repeating the identical call can plausibly succeed.
RETRYABLE: dict[str, bool] = {
    INVALID_REQUEST: False,
    BLOCKED_URL: False,
    AUTH_FAILED: False,
    FORBIDDEN: False,
    NOT_FOUND: False,
    RATE_LIMITED: True,
    TIMEOUT: True,
    SERVER_ERROR: True,
    NETWORK_ERROR: True,
    TOO_MANY_REDIRECTS: False,
    INVALID_RESPONSE: False,
    UNSUPPORTED_CONTENT: False,
    PARSE_FAILED: False,
    EMPTY_CONTENT: False,
    FIXTURE_MISSING: False,
}

ALL_CODES = tuple(RETRYABLE)

#: Default follow-up per (endpoint, code). The endpoint matters: a scholar rate
#: limit is a routing signal (web has no QPS cap), a fetch rate limit is not.
_HINTS: dict[tuple[str, str], dict[str, Any]] = {
    ("scholar", RATE_LIMITED): {
        "fallback": "search_web",
        "why": "scholar is capped at 2 QPS; web has no per-second cap",
    },
    ("scholar", TIMEOUT): {"fallback": "search_web"},
    ("scholar", SERVER_ERROR): {"fallback": "search_web"},
    ("web", RATE_LIMITED): {"fallback": "wait_or_reword"},
    ("fetch", FORBIDDEN): {
        "fallback": "use_snippet",
        "why": "the source refused the request; judge from the search snippet",
    },
    ("fetch", NOT_FOUND): {"fallback": "search_web"},
    ("fetch", TIMEOUT): {
        "fallback": "use_snippet",
        "why": "do not re-fetch; a second attempt costs the same time again",
    },
    ("fetch", UNSUPPORTED_CONTENT): {"fallback": "use_snippet"},
    ("fetch", PARSE_FAILED): {"fallback": "use_snippet"},
    ("fetch", EMPTY_CONTENT): {"fallback": "use_snippet"},
    ("fetch", BLOCKED_URL): {"fallback": "search_web", "why": "url is off-limits by policy"},
    ("fetch", TOO_MANY_REDIRECTS): {"fallback": "use_snippet"},
}


def default_hint(endpoint: str | None, code: str) -> dict[str, Any]:
    """The advised follow-up for this failure, or ``{}`` when there is none."""
    if endpoint is None:
        return {}
    return dict(_HINTS.get((endpoint, code), {}))


def is_retryable(code: str) -> bool:
    """Whether repeating the identical call is worth anything."""
    return RETRYABLE.get(code, False)


def fail_reply(
    code: str,
    message: str,
    *,
    endpoint: str | None = None,
    request: dict | None = None,
    hint: dict | None = None,
    retryable: bool | None = None,
    **extra: Any,
) -> ToolReply:
    """Build a failure envelope."""
    reply: ToolReply = {
        "ok": False,
        "data": None,
        "error": f"{code}: {message}",
        "error_code": code,
        "retryable": is_retryable(code) if retryable is None else retryable,
        "hint": default_hint(endpoint, code) if hint is None else dict(hint),
    }
    if request is not None:
        reply["request"] = request
    for key, value in extra.items():
        if value is not None:
            reply[key] = value  # type: ignore[literal-required]
    return reply


__all__ = [
    "ALL_CODES",
    "AUTH_FAILED",
    "BLOCKED_URL",
    "EMPTY_CONTENT",
    "FIXTURE_MISSING",
    "FORBIDDEN",
    "INVALID_REQUEST",
    "INVALID_RESPONSE",
    "NETWORK_ERROR",
    "NOT_FOUND",
    "PARSE_FAILED",
    "RATE_LIMITED",
    "RETRYABLE",
    "SERVER_ERROR",
    "TIMEOUT",
    "TOO_MANY_REDIRECTS",
    "UNSUPPORTED_CONTENT",
    "default_hint",
    "fail_reply",
    "is_retryable",
]
