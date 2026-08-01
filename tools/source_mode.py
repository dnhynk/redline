"""Live/mock decision and mock-leak detection.

Two jobs:

1. Decide whether this process talks to the network or to fixtures. The rule is
   ``LINER_MOCK=1`` or a missing ``LINER_API_KEY`` means mock.
2. Make it impossible for fixture data to be shown as if it were real. Every
   url the mock path hands out is registered here, so a caller can ask whether
   a url it is about to display came from a fixture — ``detect_leak()`` answers
   that even when the process is in live mode.

The environment is read on each call, never cached at import time, so a caller
that changes the environment mid-process gets the mode it just asked for.
"""

from __future__ import annotations

import os
import threading
from typing import Iterable

LIVE = "live"
MOCK = "mock"
UNKNOWN = "unknown"

MODES = (LIVE, MOCK, UNKNOWN)

_TRUE_VALUES = {"1", "true", "yes", "on"}

_LOCK = threading.Lock()
_MOCK_URLS: set[str] = set()


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def resolve_mode(*, api_key: str | None = None, mock_flag: str | None = None) -> str:
    """Decide the mode from explicit values, falling back to the environment."""
    if mock_flag is None:
        mock_flag = os.environ.get("LINER_MOCK")
    if api_key is None:
        api_key = os.environ.get("LINER_API_KEY")
    if _flag(mock_flag):
        return MOCK
    if not (api_key or "").strip():
        return MOCK
    return LIVE


def current_mode() -> str:
    """The mode this process is in right now."""
    return resolve_mode()


def is_mock() -> bool:
    return current_mode() == MOCK


def mode_reason() -> str:
    """Why the current mode was chosen — for the run record, not for control flow."""
    if _flag(os.environ.get("LINER_MOCK")):
        return "LINER_MOCK is set"
    if not (os.environ.get("LINER_API_KEY") or "").strip():
        return "LINER_API_KEY is absent"
    return "LINER_API_KEY is present and LINER_MOCK is unset"


def api_key() -> str | None:
    """The configured key, or ``None``."""
    key = (os.environ.get("LINER_API_KEY") or "").strip()
    return key or None


# --- leak detection --------------------------------------------------------


def register_mock_urls(urls: Iterable[str]) -> None:
    """Remember urls that came out of a fixture."""
    with _LOCK:
        for url in urls:
            if url:
                _MOCK_URLS.add(url)


def known_mock_urls() -> frozenset[str]:
    """Every url handed out by the mock path so far."""
    with _LOCK:
        return frozenset(_MOCK_URLS)


def is_mock_url(url: str) -> bool:
    """Whether this exact url was handed out by a fixture."""
    with _LOCK:
        return url in _MOCK_URLS


def detect_leak(urls: Iterable[str], *, mode: str | None = None) -> list[str]:
    """Fixture urls that are about to be presented as live data.

    Returns an empty list in mock mode — there the fixture origin is already
    declared. In live mode any hit is a leak and the caller must not label it
    as measured data.
    """
    if mode is None:
        mode = current_mode()
    if mode != LIVE:
        return []
    with _LOCK:
        return [url for url in urls if url in _MOCK_URLS]


def forget_mock_urls() -> None:
    """Drop the registry. For tests and for restarting a run cleanly."""
    with _LOCK:
        _MOCK_URLS.clear()


def describe() -> dict:
    """The run record entry: mode, why, and how much fixture data is in play."""
    mode = current_mode()
    return {
        "mode": mode,
        "reason": mode_reason(),
        "mock_urls_registered": len(known_mock_urls()),
    }


__all__ = [
    "LIVE",
    "MOCK",
    "MODES",
    "UNKNOWN",
    "api_key",
    "current_mode",
    "describe",
    "detect_leak",
    "forget_mock_urls",
    "is_mock",
    "is_mock_url",
    "known_mock_urls",
    "mode_reason",
    "register_mock_urls",
    "resolve_mode",
]
