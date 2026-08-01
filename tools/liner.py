"""Search client for the two Liner endpoints, plus the offline fixture path.

Public surface::

    await asearch_web(query, *, max_results=8, lang=None, date_range=None)
    await asearch_scholar(query, *, max_results=8, lang=None, date_range=None)
    search_web(...) / search_scholar(...)      # same keywords, synchronous

Nothing here raises and nothing here judges. A failure comes back as a
``ToolReply`` with a code, and the results are handed on in the order the
endpoint returned them, with no re-ranking and no summarising.

Three asymmetries drive the shape of this module:

* The wire is camelCase (``citationCount``, ``totalCount``); everything that
  leaves here is the snake_case in ``tools.schemas``.
* ``web`` has no per-second cap and ``scholar`` is capped at 2 QPS. So scholar
  gets a local rate limiter and 429 backoff, and web gets no fan-out limit at
  all — fanning out on web is the point.
* An empty result set is a finding, not an error, so it comes back ``ok`` with
  ``result_status="empty"``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from tools import errors, source_mode, usage
from tools.schemas import (
    RESULT_EMPTY,
    RESULT_OK,
    SearchResult,
    ToolReply,
    empty_search_result,
    ok_reply,
)

BASE_URL = "https://platform.liner.com/api/v1/"
WEB_PATH = "tools/search/web"
SCHOLAR_PATH = "tools/search/scholar"

#: Header name for the key. Not a bearer token.
API_KEY_HEADER = "x-api-key"

DATE_RANGES = ("past_day", "past_week", "past_month", "past_year")

MAX_RESULTS_MIN = 1
MAX_RESULTS_MAX = 20
DEFAULT_MAX_RESULTS = 8

REQUEST_TIMEOUT_S = 12.0
CONNECT_TIMEOUT_S = 5.0

#: scholar is capped at 2 QPS upstream. web is not capped and must not be.
SCHOLAR_QPS = 2.0
SCHOLAR_429_RETRIES = 2
SCHOLAR_BACKOFF_BASE_S = 0.6
#: A transient web failure is worth exactly one more try, not a loop.
WEB_RETRIES = 1

_FIXTURE_DIR = Path(__file__).with_name("fixtures")

_LANG_RE = re.compile(r"^[a-z]{2}$")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[0-9a-z가-힣ㄱ-ㆎ]+")

_CACHE_MAX_ENTRIES = 512


# --------------------------------------------------------------------------
# argument normalization
# --------------------------------------------------------------------------


def _normalize_query(query: Any) -> str:
    if not isinstance(query, str):
        return ""
    normalized = unicodedata.normalize("NFKC", query)
    return _WS_RE.sub(" ", normalized).strip()


def _normalize_lang(lang: Any) -> tuple[str | None, str | None]:
    """Return (iso_639_1_or_None, note_when_the_input_was_changed)."""
    if lang is None:
        return None, None
    if not isinstance(lang, str) or not lang.strip():
        return None, "lang was not a string; sent without a language filter"
    raw = lang.strip()
    primary = re.split(r"[-_]", unicodedata.normalize("NFKC", raw))[0].lower()
    if _LANG_RE.match(primary):
        if primary != raw:
            return primary, f"lang {raw!r} reduced to ISO 639-1 {primary!r}"
        return primary, None
    return None, f"lang {raw!r} is not ISO 639-1; sent without a language filter"


def _normalize_date_range(date_range: Any) -> tuple[str | None, str | None]:
    if date_range is None:
        return None, None
    if not isinstance(date_range, str):
        return None, "date_range was not a string; sent without a date filter"
    value = date_range.strip().lower()
    if value in DATE_RANGES:
        return value, None
    return None, f"date_range {date_range!r} is not one of {list(DATE_RANGES)}; sent without a date filter"


def _normalize_max_results(max_results: Any) -> tuple[int, str | None]:
    try:
        value = int(max_results)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS, f"max_results {max_results!r} is not an integer; used {DEFAULT_MAX_RESULTS}"
    clamped = max(MAX_RESULTS_MIN, min(MAX_RESULTS_MAX, value))
    if clamped != value:
        return clamped, f"max_results clamped from {value} to {clamped}"
    return clamped, None


def _build_request(
    endpoint: str,
    query: Any,
    max_results: Any,
    lang: Any,
    date_range: Any,
) -> dict:
    """Normalize the arguments and echo back exactly what will be sent."""
    normalized_query = _normalize_query(query)
    resolved_max, max_note = _normalize_max_results(max_results)
    resolved_lang, lang_note = _normalize_lang(lang)
    resolved_range, range_note = _normalize_date_range(date_range)
    notes = [note for note in (max_note, lang_note, range_note) if note]
    return {
        "endpoint": endpoint,
        "query": normalized_query,
        "lang": resolved_lang,
        "date_range": resolved_range,
        "max_results": resolved_max,
        "normalized_args": {"notes": notes, "query_in": query if isinstance(query, str) else repr(query)},
    }


def _payload(request: dict) -> dict:
    payload: dict[str, Any] = {"query": request["query"], "max_results": request["max_results"]}
    if request["lang"]:
        payload["lang"] = request["lang"]
    if request["date_range"]:
        payload["date_range"] = request["date_range"]
    return payload


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, ToolReply] = {}


def _cache_key(request: dict) -> tuple:
    # The mode is part of the key: fixture answers and live answers for the same
    # query are different answers and must never stand in for each other.
    return (
        source_mode.current_mode(),
        request["endpoint"],
        request["query"].casefold(),
        request["lang"],
        request["date_range"],
        request["max_results"],
    )


def _cache_get(key: tuple) -> ToolReply | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    return json.loads(json.dumps(hit)) if hit is not None else None


def _cache_put(key: tuple, reply: ToolReply) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = json.loads(json.dumps(reply))


def clear_cache() -> None:
    """Drop the search cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


def cache_size() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)


# --------------------------------------------------------------------------
# response normalization
# --------------------------------------------------------------------------


def _hostname_of(url: str) -> str:
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", url or "")
    if not match:
        return ""
    host = match.group(1).rsplit("@", 1)[-1]
    if host.startswith("["):
        return host.partition("]")[0].lstrip("[")
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [item for item in value if isinstance(item, str) and item.strip()]
    return items or None


def _normalize_result(raw: Any, endpoint: str) -> SearchResult | None:
    """camelCase wire record -> SearchResult. Records without a url are dropped."""
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    hostname = raw.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        hostname = _hostname_of(url)
    date = raw.get("date")
    if not isinstance(date, str) or not date.strip():
        date = None
    journal = raw.get("journal")
    if not isinstance(journal, str) or not journal.strip():
        journal = None
    return empty_search_result(
        title=raw.get("title") if isinstance(raw.get("title"), str) else "",
        url=url,
        hostname=hostname.strip(),
        description=raw.get("description") if isinstance(raw.get("description"), str) else "",
        date=date,
        source=endpoint,
        citation_count=_as_int(raw.get("citationCount", raw.get("citation_count"))),
        authors=_as_str_list(raw.get("authors")),
        journal=journal,
    )


def _normalize_payload(body: Any, endpoint: str) -> tuple[list[SearchResult], int | None] | None:
    if not isinstance(body, dict):
        return None
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        return None
    results = [item for item in (_normalize_result(raw, endpoint) for raw in raw_results) if item]
    total = _as_int(body.get("totalCount", body.get("total_count")))
    return results, total


def _success(
    results: list[SearchResult],
    request: dict,
    *,
    total: int | None,
    cached: bool,
    mode: str,
) -> ToolReply:
    return ok_reply(
        results,
        request=request,
        result_status=RESULT_OK if results else RESULT_EMPTY,
        total_count=total if total is not None else len(results),
        cached=cached,
        source_mode=mode,
    )


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

_FIXTURE_LOCK = threading.Lock()
_FIXTURES: dict[str, dict] = {}


def load_fixture(name: str) -> dict:
    """Read and memoize one fixture file."""
    with _FIXTURE_LOCK:
        cached = _FIXTURES.get(name)
        if cached is not None:
            return cached
    path = _FIXTURE_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    with _FIXTURE_LOCK:
        _FIXTURES[name] = data
    return data


def reload_fixtures() -> None:
    with _FIXTURE_LOCK:
        _FIXTURES.clear()


def _match_key(query: str) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", query)).casefold().strip()


def _rule_applies(rule: dict, endpoint: str, key: str) -> bool:
    rule_endpoint = rule.get("endpoint", "any")
    if rule_endpoint not in ("any", endpoint):
        return False
    tokens = rule.get("all_tokens") or []
    return bool(tokens) and all(str(token).casefold() in key for token in tokens)


def _tokens(query: str) -> list[str]:
    return _TOKEN_RE.findall(_match_key(query))


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" or "ㄱ" <= ch <= "ㆎ" for ch in text)


_STOPWORDS = frozenset(
    {"a", "an", "and", "the", "of", "for", "on", "in", "to", "is", "are", "with", "vs", "study", "review", "evidence"}
)


def _topic_of(query: str) -> str:
    tokens = [token for token in _tokens(query) if token not in _STOPWORDS]
    if not tokens:
        tokens = _tokens(query) or [query.strip() or "the topic"]
    return " ".join(tokens[:6])


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()


def _pick(pool: list, seed: bytes, offset: int):
    return pool[seed[offset % len(seed)] % len(pool)] if pool else None


def _fallback_results(endpoint: str, query: str, max_results: int) -> list[SearchResult]:
    """Deterministic, deliberately neutral results for a query no rule covers."""
    config = load_fixture("search")["fallback"]
    seed = _digest(endpoint, _match_key(query))
    lang = "ko" if _has_hangul(query) else "en"
    templates = config["templates"][lang]
    topic = _topic_of(query)
    count_min = int(config.get("count_min", 2))
    count_max = int(config.get("count_max", 3))
    span = max(1, count_max - count_min + 1)
    count = min(max_results, count_min + seed[0] % span)

    hostnames = config["hostnames"].get(endpoint) or config["hostnames"]["web"]
    titles = templates["titles"]
    results: list[SearchResult] = []
    for i in range(count):
        title = titles[(seed[1 + i] + i) % len(titles)].format(topic=topic)
        hostname = hostnames[(seed[5 + i] + i) % len(hostnames)]
        slug = re.sub(r"[^a-z0-9]+", "-", _match_key(topic)).strip("-") or "record"
        marker = _digest(endpoint, _match_key(query), str(i)).hex()[:8]
        results.append(
            empty_search_result(
                title=title,
                url=f"https://{hostname}/{slug}-{marker}",
                hostname=hostname,
                description=templates["description"].format(topic=topic),
                date=_pick(config["dates"], seed, 9 + i),
                source=endpoint,
                citation_count=_pick(config["citation_counts"], seed, 13 + i) if endpoint == "scholar" else None,
                authors=_pick(config["authors"], seed, 17 + i) if endpoint == "scholar" else None,
                journal=_pick(config["journals"], seed, 21 + i) if endpoint == "scholar" else None,
            )
        )
    return results


def _fixture_results(endpoint: str, request: dict) -> tuple[list[SearchResult], str]:
    """Fixture lookup. Returns (results, rule_id)."""
    fixtures = load_fixture("search")
    key = _match_key(request["query"])
    for rule in fixtures.get("empty_rules", []):
        if _rule_applies(rule, endpoint, key):
            return [], rule["id"]
    for rule in fixtures.get("rules", []):
        if _rule_applies(rule, endpoint, key):
            results = []
            for raw in rule.get("results", []):
                record = dict(raw)
                record.setdefault("hostname", _hostname_of(record.get("url", "")))
                results.append(
                    empty_search_result(
                        title=record.get("title", ""),
                        url=record.get("url", ""),
                        hostname=record.get("hostname", ""),
                        description=record.get("description", ""),
                        date=record.get("date"),
                        source=endpoint,
                        citation_count=record.get("citation_count"),
                        authors=record.get("authors"),
                        journal=record.get("journal"),
                    )
                )
            return results[: request["max_results"]], rule["id"]
    return _fallback_results(endpoint, request["query"], request["max_results"]), "fallback"


def _mock_search(endpoint: str, request: dict) -> ToolReply:
    results, rule_id = _fixture_results(endpoint, request)
    source_mode.register_mock_urls(result["url"] for result in results)
    request = dict(request)
    request["fixture_rule"] = rule_id
    return _success(results, request, total=len(results), cached=False, mode=source_mode.MOCK)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_CLIENTS: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    loop_id = id(asyncio.get_running_loop())
    client = _CLIENTS.get(loop_id)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )
        _CLIENTS[loop_id] = client
    return client


async def aclose() -> None:
    """Close the client bound to the running loop."""
    client = _CLIENTS.pop(id(asyncio.get_running_loop()), None)
    if client is not None and not client.is_closed:
        await client.aclose()


class _RateLimiter:
    """Spaces calls so they never exceed a fixed rate on this loop."""

    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / qps
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now += wait
            self._next_at = now + self._interval


_LIMITERS: dict[int, _RateLimiter] = {}


def _scholar_limiter() -> _RateLimiter:
    loop_id = id(asyncio.get_running_loop())
    limiter = _LIMITERS.get(loop_id)
    if limiter is None:
        limiter = _RateLimiter(SCHOLAR_QPS)
        _LIMITERS[loop_id] = limiter
    return limiter


def _status_failure(status: int, endpoint: str, request: dict, body_hint: str) -> ToolReply:
    if status in (401, 403):
        code = errors.AUTH_FAILED
        message = f"the search API rejected the key ({status})"
    elif status == 404:
        code = errors.NOT_FOUND
        message = f"endpoint not found ({status})"
    elif status == 429:
        code = errors.RATE_LIMITED
        message = f"rate limited by the search API ({status})"
    elif status >= 500:
        code = errors.SERVER_ERROR
        message = f"the search API returned {status}"
    else:
        code = errors.INVALID_RESPONSE
        message = f"unexpected status {status}"
    if body_hint:
        message = f"{message}: {body_hint}"
    return errors.fail_reply(code, message, endpoint=endpoint, request=request)


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, min(5.0, float(header)))
        except ValueError:
            pass
    return SCHOLAR_BACKOFF_BASE_S * (2**attempt)


async def _post(endpoint: str, path: str, request: dict, key: str) -> ToolReply:
    body_payload = _payload(request)
    headers = {API_KEY_HEADER: key, "content-type": "application/json"}
    limiter = _scholar_limiter() if endpoint == "scholar" else None
    attempts = SCHOLAR_429_RETRIES if endpoint == "scholar" else WEB_RETRIES
    last: ToolReply | None = None

    for attempt in range(attempts + 1):
        if limiter is not None:
            await limiter.acquire()
        try:
            response = await _client().post(path, json=body_payload, headers=headers)
        except httpx.TimeoutException as exc:
            last = errors.fail_reply(
                errors.TIMEOUT, f"no response within {REQUEST_TIMEOUT_S:g}s ({exc.__class__.__name__})",
                endpoint=endpoint, request=request,
            )
        except httpx.HTTPError as exc:
            last = errors.fail_reply(
                errors.NETWORK_ERROR, f"{exc.__class__.__name__}: {exc}",
                endpoint=endpoint, request=request,
            )
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    return errors.fail_reply(
                        errors.INVALID_RESPONSE, "response body was not JSON",
                        endpoint=endpoint, request=request,
                    )
                normalized = _normalize_payload(body, endpoint)
                if normalized is None:
                    return errors.fail_reply(
                        errors.INVALID_RESPONSE, "response JSON had no results array",
                        endpoint=endpoint, request=request,
                    )
                results, total = normalized
                return _success(results, request, total=total, cached=False, mode=source_mode.LIVE)

            snippet = (response.text or "")[:160].replace("\n", " ").strip()
            last = _status_failure(response.status_code, endpoint, request, snippet)
            if response.status_code == 429 and endpoint == "scholar" and attempt < attempts:
                await asyncio.sleep(_retry_after_seconds(response, attempt))
                continue
            if response.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(SCHOLAR_BACKOFF_BASE_S * (2**attempt))
                continue
            return last

        # network-level failure: one more try if we have budget
        if attempt >= attempts:
            break
        await asyncio.sleep(SCHOLAR_BACKOFF_BASE_S * (2**attempt))

    assert last is not None
    if last.get("error_code") == errors.RATE_LIMITED:
        last["error"] = f"{errors.RATE_LIMITED}: rate limited after {attempts} retries"
    return last


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


async def _asearch(endpoint: str, path: str, query, max_results, lang, date_range) -> ToolReply:
    request = _build_request(endpoint, query, max_results, lang, date_range)
    if not request["query"]:
        usage.record(endpoint, failure=True)
        return errors.fail_reply(
            errors.INVALID_REQUEST, "query was empty after normalization",
            endpoint=endpoint, request=request,
        )

    key = _cache_key(request)
    cached = _cache_get(key)
    if cached is not None:
        usage.record(endpoint, cache_hit=True)
        cached["cached"] = True
        return cached

    if source_mode.is_mock():
        reply = _mock_search(endpoint, request)
        usage.record(endpoint)
        _cache_put(key, reply)
        return reply

    api_key = source_mode.api_key()
    if not api_key:
        usage.record(endpoint, failure=True)
        return errors.fail_reply(
            errors.AUTH_FAILED, "LINER_API_KEY is not set", endpoint=endpoint, request=request,
        )

    reply = await _post(endpoint, path, request, api_key)
    usage.record(endpoint, failure=not reply.get("ok", False))
    if reply.get("ok"):
        _cache_put(key, reply)
    return reply


async def asearch_web(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    lang: str | None = None,
    date_range: str | None = None,
) -> ToolReply:
    """Search the general web. No fan-out limit — parallel calls are expected."""
    return await _asearch("web", WEB_PATH, query, max_results, lang, date_range)


async def asearch_scholar(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    lang: str | None = None,
    date_range: str | None = None,
) -> ToolReply:
    """Search scholarly records. Locally limited to 2 QPS, with 429 backoff."""
    return await _asearch("scholar", SCHOLAR_PATH, query, max_results, lang, date_range)


def _run_sync(coro) -> ToolReply:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def search_web(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    lang: str | None = None,
    date_range: str | None = None,
) -> ToolReply:
    """Synchronous ``asearch_web``."""
    return _run_sync(asearch_web(query, max_results=max_results, lang=lang, date_range=date_range))


def search_scholar(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    lang: str | None = None,
    date_range: str | None = None,
) -> ToolReply:
    """Synchronous ``asearch_scholar``."""
    return _run_sync(asearch_scholar(query, max_results=max_results, lang=lang, date_range=date_range))


def fixture_urls() -> frozenset[str]:
    """Every url the search fixtures can hand out from a rule."""
    fixtures = load_fixture("search")
    urls: set[str] = set()
    for rule in fixtures.get("rules", []):
        for record in rule.get("results", []):
            url = record.get("url")
            if url:
                urls.add(url)
    return frozenset(urls)


__all__ = [
    "API_KEY_HEADER",
    "BASE_URL",
    "DATE_RANGES",
    "DEFAULT_MAX_RESULTS",
    "MAX_RESULTS_MAX",
    "MAX_RESULTS_MIN",
    "SCHOLAR_QPS",
    "aclose",
    "asearch_scholar",
    "asearch_web",
    "cache_size",
    "clear_cache",
    "fixture_urls",
    "load_fixture",
    "reload_fixtures",
    "search_scholar",
    "search_web",
]
