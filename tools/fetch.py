"""Body-text extraction for one source url.

Public surface::

    await afetch_source(url, *, max_chars=8000)
    fetch_source(url, *, max_chars=8000)

PDF routing happens inside: the caller sees one function, and a PDF comes back
as its abstract with ``is_pdf`` set.

Two things this module is careful about.

**Where it is allowed to connect.** The text being audited is untrusted, so the
url can be steered. Loopback, private, link-local, multicast, unspecified and
reserved addresses are refused, userinfo in the url is refused, and every
redirect hop is checked again under the same policy.

**Resolving exactly once.** Validating a hostname and then handing that hostname
to the HTTP client lets the name resolve twice, and a record that changes
between the two resolutions defeats the check. So each hop is resolved here,
the address is validated, and the request is sent to that address with the
original ``Host`` header and TLS server name preserved. The connection goes to
the address that was checked.

*Known limits of that approach*: a host with several addresses is pinned to the
one we validated, so a hostile resolver cannot swap in a private address after
the check, but it can still choose which public address we get. Nothing here
constrains what a legitimately public address does — an open redirector or a
proxy running on a public host is outside this policy, as is any egress control
the surrounding environment does or does not provide.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from tools import errors, source_mode, usage
from tools.liner import load_fixture
from tools.schemas import PageText, ToolReply, empty_page_text, ok_reply

DEFAULT_MAX_CHARS = 8000
MAX_CHARS_MIN = 500
MAX_CHARS_MAX = 20000

#: Stop reading the body here. A document larger than this is truncated and
#: says so; the alternative is spending the whole time budget on one page.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3

HTML_TIMEOUT_S = 6.0
PDF_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 4.0

PDF_MAX_PAGES = 4

USER_AGENT = "redline-audit/1.0 (+source verification)"

_ALLOWED_SCHEMES = ("http", "https")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_ABSTRACT_RE = re.compile(r"\bAbstract\b", re.IGNORECASE)
_ABSTRACT_END_RE = re.compile(r"\n\s*(?:1\.?\s+)?(?:Introduction|Keywords|Background and aims)\b", re.IGNORECASE)

#: Test-only escape hatch: "ip:port" pairs the address policy lets through so a
#: stub server on loopback can exercise the extraction path. Never read from the
#: environment and never populated at runtime.
_TEST_ALLOWED_ADDRS: set[str] = set()


# --------------------------------------------------------------------------
# url and address policy
# --------------------------------------------------------------------------


class _Blocked(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _split(url: str) -> Any:
    return urlsplit(url.strip())


def _check_url_shape(url: str) -> Any:
    """Syntactic checks. Raises ``_Blocked`` with the reason."""
    if not isinstance(url, str) or not url.strip():
        raise _Blocked("url was empty or not a string")
    parts = _split(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise _Blocked(f"scheme {parts.scheme or '(none)'!r} is not http or https")
    if "@" in parts.netloc:
        raise _Blocked("url carries userinfo before the host; parsers disagree on which host wins")
    if not parts.hostname:
        raise _Blocked("url has no host")
    try:
        parts.port
    except ValueError:
        raise _Blocked("url has an invalid port") from None
    return parts


def _address_allowed(ip: ipaddress._BaseAddress) -> str | None:
    """``None`` when the address may be used, otherwise the reason it may not."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Order matters: the link-local range is also private, and the specific
    # reason is what tells a reader whether this was the metadata service.
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_reserved:
        return "reserved address"
    return None


async def _resolve(host: str, port: int) -> str:
    """One resolution per hop. Returns the address we will connect to."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        reason = _address_allowed(literal)
        if reason and f"{literal}:{port}" not in _TEST_ALLOWED_ADDRS:
            raise _Blocked(f"{host} is a {reason}")
        return str(literal)

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _Blocked(f"could not resolve {host}: {exc.__class__.__name__}") from None
    if not infos:
        raise _Blocked(f"could not resolve {host}")

    blocked_reason = None
    for info in infos:
        candidate = info[4][0]
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if f"{ip}:{port}" in _TEST_ALLOWED_ADDRS:
            return str(ip)
        reason = _address_allowed(ip)
        if reason is None:
            return str(ip)
        blocked_reason = reason
    raise _Blocked(f"{host} resolves to a {blocked_reason or 'disallowed address'}")


def _pinned_url(parts: Any, ip: str) -> str:
    """The same url with the validated address in place of the hostname."""
    host = f"[{ip}]" if ":" in ip else ip
    port = parts.port
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def _host_header(parts: Any) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    # The head is dropped on purpose: a page whose only text is its <title> has
    # no body, and returning the title as body text would look like content.
    body = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", html)
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    body = _TAG_RE.sub(" ", body)
    body = (
        body.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return _tidy(body)


def _tidy(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_RE.sub("\n\n", text).strip()


def _html_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return _tidy(_TAG_RE.sub(" ", match.group(1)))[:300] if match else ""


def _extract_html(html: str, url: str) -> tuple[str, str]:
    """Return (title, body). Falls back to tag stripping if extraction fails."""
    title = ""
    text = ""
    try:
        import trafilatura
    except ImportError:
        trafilatura = None  # type: ignore[assignment]
    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
        except Exception:
            extracted = None
        if extracted:
            text = _tidy(extracted)
        try:
            metadata = trafilatura.extract_metadata(html)
        except Exception:
            metadata = None
        if metadata is not None and getattr(metadata, "title", None):
            title = str(metadata.title).strip()[:300]
    if not text:
        text = _strip_html(html)
    if not title:
        title = _html_title(html)
    return title, text


def _extract_pdf(raw: bytes) -> tuple[str, str]:
    """Return (title, abstract-or-opening-text). Raises on an unreadable file."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    title = ""
    try:
        metadata = reader.metadata
        if metadata and metadata.title:
            title = str(metadata.title).strip()[:300]
    except Exception:
        title = ""

    chunks = []
    for page in reader.pages[:PDF_MAX_PAGES]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    text = _tidy("\n".join(chunks))

    match = _ABSTRACT_RE.search(text)
    if match:
        tail = text[match.start():]
        end = _ABSTRACT_END_RE.search(tail, 20)
        text = tail[: end.start()] if end else tail
    return title, _tidy(text)


def _page(
    url: str,
    title: str,
    text: str,
    *,
    is_pdf: bool,
    source_truncated: bool,
    max_chars: int,
) -> PageText:
    original = len(text)
    text_truncated = original > max_chars
    body = text[:max_chars] if text_truncated else text
    return empty_page_text(
        url=url,
        title=title,
        text=body,
        is_pdf=is_pdf,
        truncated=source_truncated or text_truncated,
        source_truncated=source_truncated,
        text_truncated=text_truncated,
        original_chars=original,
        returned_chars=len(body),
    )


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, ToolReply] = {}
_CACHE_MAX_ENTRIES = 256


def _cache_get(key: tuple) -> ToolReply | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    return json.loads(json.dumps(hit)) if hit is not None else None


def _cache_put(key: tuple, reply: ToolReply) -> None:
    # Successes and permanent failures only. A timeout or a 5xx is a fact about
    # this moment, and caching it would keep a source unreachable for the rest
    # of the run.
    if not reply.get("ok") and reply.get("retryable", False):
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = json.loads(json.dumps(reply))


def clear_cache() -> None:
    """Drop the fetch cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _mock_fetch(url: str, max_chars: int, request: dict) -> ToolReply:
    fixtures = load_fixture("sources")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()

    if host in {name.lower() for name in fixtures.get("fabricated_hosts", [])}:
        return errors.fail_reply(
            errors.NOT_FOUND,
            f"no document at {url}",
            endpoint="fetch",
            request=request,
            source_mode=source_mode.MOCK,
        )

    record = fixtures["sources"].get(url)
    if record is None:
        fallback = fixtures["fallback"]
        slug = (parts.path or "/").strip("/").replace("/", " ") or "index"
        record = {
            "title": fallback["title"].format(host=host or url, slug=slug),
            "text": fallback["text"].format(host=host or url),
            "is_pdf": (parts.path or "").lower().endswith(".pdf"),
        }

    source_mode.register_mock_urls([url])
    page = _page(
        url,
        record.get("title", ""),
        record.get("text", ""),
        is_pdf=bool(record.get("is_pdf")),
        source_truncated=False,
        max_chars=max_chars,
    )
    return ok_reply(page, request=request, source_mode=source_mode.MOCK)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_CLIENTS: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    loop_id = id(asyncio.get_running_loop())
    client = _CLIENTS.get(loop_id)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(PDF_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"user-agent": USER_AGENT, "accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"},
        )
        _CLIENTS[loop_id] = client
    return client


async def aclose() -> None:
    """Close the client bound to the running loop."""
    client = _CLIENTS.pop(id(asyncio.get_running_loop()), None)
    if client is not None and not client.is_closed:
        await client.aclose()


def _status_failure(status: int, request: dict) -> ToolReply:
    if status in (401, 402, 403, 451):
        code, message = errors.FORBIDDEN, f"the source refused the request ({status})"
    elif status in (404, 410):
        code, message = errors.NOT_FOUND, f"the source has no document there ({status})"
    elif status == 429:
        code, message = errors.RATE_LIMITED, f"the source rate limited us ({status})"
    elif status >= 500:
        code, message = errors.SERVER_ERROR, f"the source returned {status}"
    else:
        code, message = errors.INVALID_RESPONSE, f"unexpected status {status}"
    return errors.fail_reply(code, message, endpoint="fetch", request=request)


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_RESPONSE_BYTES:
            truncated = True
            break
    raw = b"".join(chunks)
    return raw[:MAX_RESPONSE_BYTES], truncated


async def _live_fetch(url: str, max_chars: int, request: dict) -> ToolReply:
    current = url
    seen = 0
    while True:
        try:
            parts = _check_url_shape(current)
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
            ip = await _resolve(parts.hostname, port)
        except _Blocked as blocked:
            return errors.fail_reply(
                errors.BLOCKED_URL, blocked.message, endpoint="fetch", request=request,
            )

        target = _pinned_url(parts, ip)
        headers = {"host": _host_header(parts)}
        extensions = {"sni_hostname": parts.hostname}
        timeout = httpx.Timeout(
            PDF_TIMEOUT_S if (parts.path or "").lower().endswith(".pdf") else HTML_TIMEOUT_S,
            connect=CONNECT_TIMEOUT_S,
        )
        try:
            async with _client().stream(
                "GET", target, headers=headers, extensions=extensions, timeout=timeout
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return errors.fail_reply(
                            errors.INVALID_RESPONSE, f"redirect without a location ({response.status_code})",
                            endpoint="fetch", request=request,
                        )
                    seen += 1
                    if seen > MAX_REDIRECTS:
                        return errors.fail_reply(
                            errors.TOO_MANY_REDIRECTS, f"more than {MAX_REDIRECTS} redirects",
                            endpoint="fetch", request=request,
                        )
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return _status_failure(response.status_code, request)

                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                raw, source_truncated = await _read_capped(response)
                encoding = response.charset_encoding or "utf-8"
                final_url = current
        except httpx.TimeoutException:
            return errors.fail_reply(
                errors.TIMEOUT, f"the source did not answer within {timeout.read:g}s",
                endpoint="fetch", request=request,
            )
        except httpx.HTTPError as exc:
            return errors.fail_reply(
                errors.NETWORK_ERROR, f"{exc.__class__.__name__}: {exc}",
                endpoint="fetch", request=request,
            )
        break

    # The magic bytes win over the header: sources mislabel PDFs as octet-stream.
    is_pdf = content_type == "application/pdf" or raw.startswith(b"%PDF-")

    if is_pdf:
        try:
            title, text = _extract_pdf(raw)
        except Exception as exc:
            return errors.fail_reply(
                errors.PARSE_FAILED, f"could not read the PDF ({exc.__class__.__name__})",
                endpoint="fetch", request=request,
            )
    elif content_type in ("text/html", "application/xhtml+xml", "text/plain", ""):
        html = raw.decode(encoding, errors="replace")
        title, text = _extract_html(html, final_url) if content_type != "text/plain" else ("", _tidy(html))
    else:
        return errors.fail_reply(
            errors.UNSUPPORTED_CONTENT, f"content type {content_type!r} is neither html nor pdf",
            endpoint="fetch", request=request,
        )

    if not text.strip():
        return errors.fail_reply(
            errors.EMPTY_CONTENT, "no body text could be extracted",
            endpoint="fetch", request=request,
        )

    page = _page(
        final_url, title, text, is_pdf=is_pdf, source_truncated=source_truncated, max_chars=max_chars
    )
    return ok_reply(page, request=request, source_mode=source_mode.LIVE)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


async def afetch_source(url: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> ToolReply:
    """Fetch one source and return its body text. Never raises."""
    try:
        requested = int(max_chars)
    except (TypeError, ValueError):
        requested = DEFAULT_MAX_CHARS
    resolved = max(MAX_CHARS_MIN, min(MAX_CHARS_MAX, requested))
    notes = [] if resolved == requested else [f"max_chars clamped from {requested} to {resolved}"]
    request = {
        "endpoint": "fetch",
        "url": url if isinstance(url, str) else repr(url),
        "max_chars": resolved,
        "normalized_args": {"notes": notes},
    }

    try:
        _check_url_shape(url)
    except _Blocked as blocked:
        usage.record("fetch", failure=True)
        code = errors.INVALID_REQUEST if "url was empty" in blocked.message else errors.BLOCKED_URL
        return errors.fail_reply(code, blocked.message, endpoint="fetch", request=request)

    key = (source_mode.current_mode(), url.strip(), resolved)
    cached = _cache_get(key)
    if cached is not None:
        usage.record("fetch", cache_hit=True, failure=not cached.get("ok", False))
        cached["cached"] = True
        return cached

    if source_mode.is_mock():
        reply = _mock_fetch(url.strip(), resolved, request)
    else:
        reply = await _live_fetch(url.strip(), resolved, request)

    usage.record("fetch", failure=not reply.get("ok", False))
    _cache_put(key, reply)
    return reply


def fetch_source(url: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> ToolReply:
    """Synchronous ``afetch_source``."""
    coro = afetch_source(url, max_chars=max_chars)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS_MAX",
    "MAX_CHARS_MIN",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "aclose",
    "afetch_source",
    "clear_cache",
    "fetch_source",
]
