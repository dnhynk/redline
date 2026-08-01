"""REDLINE IO layer.

Everything that leaves this process on the network lives here: the two search
endpoints and source-text extraction. The package depends on the standard
library, httpx and the two parsers, and on nothing else in the project — the
import direction is one way, and it points out of here.

The layer does not judge. It does not rank, summarise, score or evaluate what it
retrieves; it normalises the shape and hands the content on unchanged.

Public API::

    asearch_web / asearch_scholar / search_web / search_scholar   -> ToolReply
    afetch_source / fetch_source                                  -> ToolReply

Every one of them returns a ``ToolReply`` and none of them raises: a tool
failure must not end the audit loop. On failure the envelope carries
``error_code``, ``retryable`` and ``hint`` alongside the message.

``source_mode`` records whether a run read the network or the fixtures, and
which urls came from fixtures, so fixture data can never be shown as measured.
``usage`` holds the per-endpoint counters.
"""

from tools import errors, fetch, liner, source_mode, usage
from tools.errors import fail_reply
from tools.fetch import afetch_source, fetch_source
from tools.liner import asearch_scholar, asearch_web, search_scholar, search_web
from tools.schemas import PageText, SearchResult, ToolReply, ok_reply

__all__ = [
    "PageText",
    "SearchResult",
    "ToolReply",
    "afetch_source",
    "asearch_scholar",
    "asearch_web",
    "errors",
    "fail_reply",
    "fetch",
    "fetch_source",
    "liner",
    "ok_reply",
    "search_scholar",
    "search_web",
    "source_mode",
    "usage",
]

__version__ = "1.0.0"
