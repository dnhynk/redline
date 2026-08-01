"""Normalized return types for the IO layer.

Every IO function returns a ``ToolReply`` and never raises: the agent loop must
survive a failing tool. The wire format of the upstream search API is camelCase;
everything that leaves this package is snake_case.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

SearchSource = Literal["web", "scholar"]

#: Endpoint labels used by the usage counters and by ``request["endpoint"]``.
ENDPOINTS = ("web", "scholar", "fetch")

#: ``result_status`` values carried on a successful search reply.
RESULT_OK = "ok"
RESULT_EMPTY = "empty"


class SearchResult(TypedDict):
    """One normalized search hit.

    All keys are always present. ``citation_count``/``authors``/``journal`` are
    scholar-only in practice and are ``None`` on web results. ``date`` is mostly
    ``YYYY-MM-DD`` but a bare year (``"2004"``) also occurs upstream, so it stays
    a string and is never reformatted.
    """

    title: str
    url: str
    hostname: str
    description: str
    date: str | None
    source: str
    citation_count: int | None
    authors: list[str] | None
    journal: str | None


class PageText(TypedDict):
    """Extracted body text of one source.

    The truncation accounting exists so a fidelity judgement is never made on a
    body that was silently cut: ``truncated`` is the union of the two causes,
    and when ``source_truncated`` is set ``original_chars`` is only a lower bound
    because the rest of the document was never downloaded.
    """

    url: str
    title: str
    text: str
    is_pdf: bool
    truncated: bool
    source_truncated: bool
    text_truncated: bool
    original_chars: int
    returned_chars: int


class ToolReply(TypedDict, total=False):
    """Envelope returned by every public IO function.

    ``ok``/``data``/``error`` are always present. ``error`` is ``None`` on
    success and ``"<error_code>: <message>"`` on failure. The remaining keys are
    structured detail the caller may forward to the model.
    """

    ok: bool
    data: Any
    error: str | None
    error_code: str
    retryable: bool
    hint: dict
    request: dict
    result_status: str
    total_count: int
    cached: bool
    source_mode: str


def empty_search_result(**overrides: Any) -> SearchResult:
    """A ``SearchResult`` with every key present, for normalizers to fill in."""
    result: SearchResult = {
        "title": "",
        "url": "",
        "hostname": "",
        "description": "",
        "date": None,
        "source": "web",
        "citation_count": None,
        "authors": None,
        "journal": None,
    }
    result.update(overrides)  # type: ignore[typeddict-item]
    return result


def empty_page_text(**overrides: Any) -> PageText:
    """A ``PageText`` with every key present, for extractors to fill in."""
    page: PageText = {
        "url": "",
        "title": "",
        "text": "",
        "is_pdf": False,
        "truncated": False,
        "source_truncated": False,
        "text_truncated": False,
        "original_chars": 0,
        "returned_chars": 0,
    }
    page.update(overrides)  # type: ignore[typeddict-item]
    return page


def ok_reply(data: Any, **extra: Any) -> ToolReply:
    """Build a success envelope."""
    reply: ToolReply = {"ok": True, "data": data, "error": None}
    for key, value in extra.items():
        if value is not None:
            reply[key] = value  # type: ignore[literal-required]
    return reply


SEARCH_RESULT_KEYS = frozenset(SearchResult.__annotations__)
PAGE_TEXT_KEYS = frozenset(PageText.__annotations__)

__all__ = [
    "ENDPOINTS",
    "PAGE_TEXT_KEYS",
    "RESULT_EMPTY",
    "RESULT_OK",
    "SEARCH_RESULT_KEYS",
    "PageText",
    "SearchResult",
    "SearchSource",
    "ToolReply",
    "empty_page_text",
    "empty_search_result",
    "ok_reply",
]
