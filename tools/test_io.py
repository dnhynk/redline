"""Tests for the IO layer.

No test in this file calls a real endpoint. The offline path runs against the
fixtures; the network path runs against an in-process transport or a stub
server bound to loopback.

Loopback is refused by the address policy, which is the point of the policy, so
the stub-server tests register that one address in ``fetch._TEST_ALLOWED_ADDRS``
for the duration of the test. Nothing else is exempted, and a redirect that
leaves that address is still refused.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tools import errors, fetch, liner, source_mode, usage
from tools.schemas import PAGE_TEXT_KEYS, SEARCH_RESULT_KEYS

FIXTURE_DIR = Path(__file__).with_name("fixtures")


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts in offline mode with empty caches and counters."""
    monkeypatch.setenv("LINER_MOCK", "1")
    monkeypatch.setenv("LINER_API_KEY", "")
    liner.clear_cache()
    fetch.clear_cache()
    liner.reload_fixtures()
    usage.reset()
    source_mode.forget_mock_urls()
    yield
    liner.clear_cache()
    fetch.clear_cache()
    usage.reset()
    source_mode.forget_mock_urls()
    fetch._TEST_ALLOWED_ADDRS.clear()


@pytest.fixture
def live(monkeypatch):
    """Switch the process to the network path with a key present."""
    monkeypatch.setenv("LINER_MOCK", "0")
    monkeypatch.setenv("LINER_API_KEY", "test-key-not-a-real-key")
    return None


@pytest.fixture(scope="session")
def scenario() -> dict:
    return json.loads((FIXTURE_DIR / "scenario.json").read_text(encoding="utf-8"))


def mock_liner(monkeypatch, handler):
    """Point the search client at an in-process transport."""
    client = httpx.AsyncClient(base_url=liner.BASE_URL, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(liner, "_client", lambda: client)
    return client


LIVE_SHAPED_BODY = {
    "requestId": "0339742c-c049-4214-ac0b-670989112ec9",
    "totalCount": 2,
    "results": [
        {
            "title": "Coffee consumption and health: umbrella review",
            "url": "https://www.bmj.com/content/bmj/360/bmj.k194.full.pdf",
            "hostname": "bmj.com",
            "faviconUrl": "https://www.google.com/s2/favicons?domain=https://www.bmj.com",
            "description": "Umbrella review of meta-analyses.",
            "date": "2018-01-12",
            "citationCount": 88,
            "authors": None,
            "journal": "BMJ",
        },
        {
            "title": "A record whose date is a bare year",
            "url": "https://example.net/a",
            "hostname": "example.net",
            "description": "",
            "date": "2004",
            "citationCount": None,
            "authors": ["A. Author"],
            "journal": None,
        },
    ],
}


# --------------------------------------------------------------------------
# package boundary
# --------------------------------------------------------------------------


def test_package_imports_without_core():
    import subprocess
    import sys

    code = (
        "import sys, tools; "
        "assert 'core' not in sys.modules; "
        "assert not any(m.startswith('agents') for m in sys.modules); "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_public_api_is_exported():
    import tools

    for name in ("asearch_web", "asearch_scholar", "search_web", "search_scholar",
                 "afetch_source", "fetch_source", "source_mode", "usage", "errors"):
        assert hasattr(tools, name), name


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_every_code_declares_retryability():
    for code in errors.ALL_CODES:
        assert isinstance(errors.RETRYABLE[code], bool)


def test_failure_string_carries_the_code():
    reply = errors.fail_reply(errors.TIMEOUT, "took too long", endpoint="fetch")
    assert reply["ok"] is False
    assert reply["data"] is None
    assert reply["error"] == "timeout: took too long"
    assert reply["error_code"] == errors.TIMEOUT
    assert reply["retryable"] is True


def test_scholar_rate_limit_hint_points_at_web():
    hint = errors.default_hint("scholar", errors.RATE_LIMITED)
    assert hint["fallback"] == "search_web"


def test_fetch_forbidden_is_not_retryable_and_suggests_the_snippet():
    reply = errors.fail_reply(errors.FORBIDDEN, "403", endpoint="fetch")
    assert reply["retryable"] is False
    assert reply["hint"]["fallback"] == "use_snippet"


# --------------------------------------------------------------------------
# usage counters
# --------------------------------------------------------------------------


def test_counters_separate_hits_and_failures():
    usage.record("web")
    usage.record("web", cache_hit=True)
    usage.record("web", failure=True)
    usage.record("nowhere")
    snapshot = usage.snapshot()
    assert snapshot["web"] == {"calls": 3, "cache_hits": 1, "failures": 1}
    assert usage.billable_calls("web") == 1
    assert set(snapshot) == {"web", "scholar", "fetch"}


def test_searching_counts_against_its_endpoint():
    liner.search_web("coffee consumption all-cause mortality")
    liner.search_scholar("coffee consumption all-cause mortality")
    snapshot = usage.snapshot()
    assert snapshot["web"]["calls"] == 1
    assert snapshot["scholar"]["calls"] == 1


# --------------------------------------------------------------------------
# source mode
# --------------------------------------------------------------------------


def test_mode_is_mock_without_a_key():
    assert source_mode.resolve_mode(api_key="", mock_flag="0") == source_mode.MOCK
    assert source_mode.resolve_mode(api_key="   ", mock_flag=None) == source_mode.MOCK


def test_mode_is_mock_when_the_flag_is_set_even_with_a_key():
    assert source_mode.resolve_mode(api_key="abc", mock_flag="1") == source_mode.MOCK
    assert source_mode.resolve_mode(api_key="abc", mock_flag="true") == source_mode.MOCK


def test_mode_is_live_only_with_a_key_and_no_flag():
    assert source_mode.resolve_mode(api_key="abc", mock_flag="0") == source_mode.LIVE


def test_fixture_urls_are_registered_and_detectable_as_a_leak(monkeypatch):
    reply = liner.search_scholar("coffee consumption all-cause mortality")
    urls = [item["url"] for item in reply["data"]]
    assert urls
    assert source_mode.is_mock_url(urls[0])
    # In offline mode the origin is already declared, so nothing is a leak.
    assert source_mode.detect_leak(urls) == []
    # Presenting the same urls in a live run is exactly what must be caught.
    monkeypatch.setenv("LINER_MOCK", "0")
    monkeypatch.setenv("LINER_API_KEY", "abc")
    assert source_mode.detect_leak(urls) == urls


def test_describe_reports_the_reason():
    described = source_mode.describe()
    assert described["mode"] == source_mode.MOCK
    assert "LINER_MOCK" in described["reason"]


# --------------------------------------------------------------------------
# search: argument normalization
# --------------------------------------------------------------------------


def test_empty_query_is_refused_without_a_call():
    reply = liner.search_web("   ")
    assert reply["ok"] is False
    assert reply["error_code"] == errors.INVALID_REQUEST
    assert reply["retryable"] is False


def test_request_echoes_what_was_actually_sent():
    reply = liner.search_web("  coffee   consumption all-cause mortality ", lang="EN-us", date_range="past_year")
    request = reply["request"]
    assert request["query"] == "coffee consumption all-cause mortality"
    assert request["lang"] == "en"
    assert request["date_range"] == "past_year"
    assert any("ISO 639-1" in note for note in request["normalized_args"]["notes"])


def test_unusable_lang_and_date_range_are_dropped_not_fatal():
    reply = liner.search_web("coffee consumption all-cause mortality", lang="klingon", date_range="past_decade")
    assert reply["ok"] is True
    assert reply["request"]["lang"] is None
    assert reply["request"]["date_range"] is None
    assert len(reply["request"]["normalized_args"]["notes"]) == 2


def test_max_results_is_clamped_and_reported():
    reply = liner.search_scholar("coffee consumption all-cause mortality", max_results=99)
    assert reply["request"]["max_results"] == liner.MAX_RESULTS_MAX
    assert any("clamped" in note for note in reply["request"]["normalized_args"]["notes"])
    assert len(reply["data"]) <= liner.MAX_RESULTS_MAX


def test_scholar_accepts_lang():
    reply = liner.search_scholar("coffee consumption all-cause mortality", lang="ko")
    assert reply["request"]["lang"] == "ko"


# --------------------------------------------------------------------------
# search: offline results
# --------------------------------------------------------------------------


def test_results_have_every_contract_key():
    reply = liner.search_scholar("coffee consumption all-cause mortality")
    for item in reply["data"]:
        assert set(item) == SEARCH_RESULT_KEYS
        assert item["source"] == "scholar"
        assert isinstance(item["url"], str) and item["url"]
        assert isinstance(item["hostname"], str) and item["hostname"]


def test_web_results_carry_no_scholar_metadata():
    reply = liner.search_web("coffee drinking mortality press release")
    assert reply["data"]
    for item in reply["data"]:
        assert item["source"] == "web"
        assert item["citation_count"] is None
        assert item["journal"] is None


def test_offline_results_are_deterministic():
    first = liner.search_scholar("coffee cancer prevention evidence")
    liner.clear_cache()
    second = liner.search_scholar("coffee cancer prevention evidence")
    assert first["data"] == second["data"]


def test_null_metadata_cases_are_covered():
    reply = liner.search_scholar("coffee consumption all-cause mortality")
    data = reply["data"]
    assert any(item["citation_count"] is None for item in data)
    assert any(item["authors"] is None for item in data)
    assert any(item["journal"] is None for item in data)
    web = liner.search_web("coffee drinking mortality press release")
    assert any(item["date"] is None for item in web["data"])


def test_a_korean_query_matches_a_rule():
    reply = liner.search_web("커피 섭취 사망률 코호트 연구")
    assert reply["request"]["fixture_rule"] == "coffee-mortality-ko"
    assert reply["data"]
    assert any(re.search(r"[가-힣]", item["title"]) for item in reply["data"])


def test_second_identical_search_is_a_cache_hit():
    liner.search_web("coffee consumption all-cause mortality")
    second = liner.search_web("coffee consumption all-cause mortality")
    assert second["cached"] is True
    assert usage.snapshot()["web"] == {"calls": 2, "cache_hits": 1, "failures": 0}


def test_cache_key_normalizes_the_query():
    liner.search_web("Coffee Consumption All-Cause Mortality")
    second = liner.search_web("  coffee consumption all-cause  mortality")
    assert second["cached"] is True


# --------------------------------------------------------------------------
# search: zero results are a deliberate signal, not a coverage gap
# --------------------------------------------------------------------------


FABRICATED_QUERIES = [
    "Harvard Global Hydration Study 2025",
    "International Sleep Foundation Miracle Coffee",
    "대한수면의학회 기적의 커피 보고서",
    "Stanford Metabolic Longevity Trial 2024",
]

COVERED_QUERIES = [
    "coffee cancer prevention evidence",
    "커피 임신 카페인 안전",
    "caffeine intake and mortality risk",
    "coffee consumption all-cause mortality",
    "vitamin D supplementation respiratory tract infection",
    "green tea blood pressure meta-analysis",
    "eight glasses of water a day evidence",
    "an entirely unrelated question about thermal paste",
]


@pytest.mark.parametrize("query", FABRICATED_QUERIES)
def test_only_fabricated_sources_come_back_empty(query):
    for search in (liner.search_web, liner.search_scholar):
        liner.clear_cache()
        reply = search(query)
        assert reply["ok"] is True, reply
        assert reply["data"] == [], query
        assert reply["result_status"] == "empty"
        assert reply["request"]["fixture_rule"].startswith("fabricated-")


@pytest.mark.parametrize("query", COVERED_QUERIES)
def test_every_other_query_gets_results(query):
    for search in (liner.search_web, liner.search_scholar):
        liner.clear_cache()
        reply = search(query)
        assert reply["ok"] is True, reply
        assert len(reply["data"]) >= 2, (query, reply["request"]["fixture_rule"])
        assert reply["result_status"] == "ok"


def test_the_generated_fallback_stays_neutral():
    reply = liner.search_scholar("an entirely unrelated question about thermal paste")
    assert reply["request"]["fixture_rule"] == "fallback"
    for item in reply["data"]:
        assert set(item) == SEARCH_RESULT_KEYS
        assert "thermal paste" in item["description"]
        # Neutral means it takes no side; these words are what a side sounds like.
        assert not re.search(r"\b(proves?|proven|confirms?|refutes?|disproves?)\b", item["description"], re.I)


def test_the_fallback_answers_in_the_language_of_the_query():
    reply = liner.search_web("커피 임신 카페인 안전")
    assert reply["request"]["fixture_rule"] == "fallback"
    assert all(re.search(r"[가-힣]", item["title"]) for item in reply["data"])


def test_the_fallback_never_rescues_a_fabricated_source():
    for query in FABRICATED_QUERIES:
        liner.clear_cache()
        assert liner.search_web(query)["data"] == []


# --------------------------------------------------------------------------
# search: the network path
# --------------------------------------------------------------------------


def test_camelcase_wire_is_normalized_to_the_contract(live, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=LIVE_SHAPED_BODY)

    mock_liner(monkeypatch, handler)
    reply = liner.search_scholar("coffee mortality", max_results=2, lang="en", date_range="past_year")

    assert reply["ok"] is True
    assert reply["source_mode"] == "live"
    assert reply["total_count"] == 2
    first, second = reply["data"]
    assert set(first) == SEARCH_RESULT_KEYS
    assert first["citation_count"] == 88
    assert first["journal"] == "BMJ"
    assert first["authors"] is None
    assert second["date"] == "2004", "a bare year must survive as-is"
    assert second["citation_count"] is None
    assert seen["headers"][liner.API_KEY_HEADER] == "test-key-not-a-real-key"
    assert seen["body"] == {"query": "coffee mortality", "max_results": 2, "lang": "en", "date_range": "past_year"}


def test_a_live_response_without_results_is_an_invalid_response(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(200, json={"requestId": "x"}))
    reply = liner.search_web("coffee mortality")
    assert reply["ok"] is False
    assert reply["error_code"] == errors.INVALID_RESPONSE
    assert reply["retryable"] is False


def test_a_non_json_body_is_an_invalid_response(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(200, text="<html>nope</html>"))
    reply = liner.search_web("coffee mortality")
    assert reply["ok"] is False
    assert reply["error_code"] == errors.INVALID_RESPONSE


def test_a_rejected_key_is_not_retryable(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(401, text="bad key"))
    reply = liner.search_web("coffee mortality")
    assert reply["error_code"] == errors.AUTH_FAILED
    assert reply["retryable"] is False
    assert usage.snapshot()["web"]["failures"] == 1


def test_scholar_retries_a_rate_limit_then_hands_over_to_web(live, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")

    mock_liner(monkeypatch, handler)
    reply = liner.search_scholar("coffee mortality")
    assert reply["error_code"] == errors.RATE_LIMITED
    assert reply["retryable"] is True
    assert reply["hint"]["fallback"] == "search_web"
    assert len(calls) == liner.SCHOLAR_429_RETRIES + 1


def test_web_is_not_slowed_down_by_the_scholar_limiter(live, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"results": [], "totalCount": 0})

    mock_liner(monkeypatch, handler)

    async def fan_out():
        return await asyncio.gather(*(liner.asearch_web(f"query number {i}") for i in range(6)))

    replies = run(fan_out())
    assert all(reply["ok"] for reply in replies)
    assert len(calls) == 6


def test_an_empty_live_result_set_is_a_success_not_a_failure(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(200, json={"results": [], "totalCount": 0}))
    reply = liner.search_web("something with no hits")
    assert reply["ok"] is True
    assert reply["data"] == []
    assert reply["result_status"] == "empty"


def test_a_server_error_is_retryable(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(503, text="down"))
    reply = liner.search_web("coffee mortality")
    assert reply["error_code"] == errors.SERVER_ERROR
    assert reply["retryable"] is True


def test_a_failed_search_is_not_cached(live, monkeypatch):
    mock_liner(monkeypatch, lambda request: httpx.Response(503, text="down"))
    liner.search_web("coffee mortality")
    assert liner.cache_size() == 0


def test_a_missing_key_in_live_mode_fails_loudly(monkeypatch):
    monkeypatch.setenv("LINER_MOCK", "0")
    monkeypatch.setenv("LINER_API_KEY", "")
    # No key means offline mode, so the fixtures answer rather than the network.
    reply = liner.search_web("coffee consumption all-cause mortality")
    assert reply["source_mode"] == source_mode.MOCK


# --------------------------------------------------------------------------
# fetch: url and address policy
# --------------------------------------------------------------------------


BLOCKED_URLS = [
    "http://127.0.0.1/admin",
    "http://localhost:8000/",
    "https://[::1]/",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_addresses_off_the_public_internet_are_refused(live, url):
    reply = fetch.fetch_source(url)
    assert reply["ok"] is False, url
    assert reply["error_code"] == errors.BLOCKED_URL, url
    assert reply["retryable"] is False


def test_userinfo_in_the_url_is_refused(live):
    reply = fetch.fetch_source("https://user:pass@internal.example.com/secret")
    assert reply["error_code"] == errors.BLOCKED_URL
    assert "userinfo" in reply["error"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/x"])
def test_non_http_schemes_are_refused(live, url):
    reply = fetch.fetch_source(url)
    assert reply["error_code"] == errors.BLOCKED_URL


def test_an_empty_url_is_an_invalid_request(live):
    reply = fetch.fetch_source("   ")
    assert reply["error_code"] == errors.INVALID_REQUEST


def test_a_name_that_resolves_into_private_space_is_refused(live, monkeypatch):
    async def resolves_to_loopback(host, port, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    loop_holder = {}

    async def attempt():
        loop = asyncio.get_running_loop()
        loop_holder["loop"] = loop
        monkeypatch.setattr(loop, "getaddrinfo", resolves_to_loopback, raising=False)
        return await fetch.afetch_source("https://rebind.example.com/page")

    reply = run(attempt())
    assert reply["error_code"] == errors.BLOCKED_URL
    assert "loopback" in reply["error"]


# --------------------------------------------------------------------------
# fetch: the stub server
# --------------------------------------------------------------------------


ARTICLE_HTML = """<!doctype html><html><head><title>Vitamin D and respiratory infection</title></head>
<body><nav>skip me</nav><article><h1>Vitamin D and respiratory infection</h1>
<p>The protective effect was confined to participants with baseline circulating
25-hydroxyvitamin D below 25 nmol per litre. Participants who were replete at
baseline showed no significant benefit, and the authors decline to recommend
supplementation for the general population on the strength of these data.</p>
<p>Twenty-five randomised controlled trials contributed individual participant
data to this analysis, covering eleven thousand three hundred and twenty one
participants aged nought to ninety five years.</p></article></body></html>"""


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output readable
        pass

    def _send(self, status, body: bytes, content_type="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/article":
            self._send(200, ARTICLE_HTML.encode("utf-8"))
        elif path == "/big":
            filler = ("<p>" + "measured sentence about the source. " * 20 + "</p>").encode("utf-8")
            head = b"<!doctype html><html><head><title>Big</title></head><body>"
            body = head + filler * ((fetch.MAX_RESPONSE_BYTES // len(filler)) + 40) + b"</body></html>"
            self._send(200, body)
        elif path == "/forbidden":
            self._send(403, b"no")
        elif path == "/missing":
            self._send(404, b"gone")
        elif path == "/ratelimited":
            self._send(429, b"slow down")
        elif path == "/binary":
            self._send(200, b"\x89PNG\r\n\x1a\n", content_type="image/png")
        elif path == "/empty":
            self._send(200, b"<!doctype html><html><head><title>t</title></head><body></body></html>")
        elif path == "/badpdf":
            self._send(200, b"%PDF-1.4 this is not really a pdf", content_type="application/pdf")
        elif path == "/slow":
            time.sleep(1.5)
            self._send(200, ARTICLE_HTML.encode("utf-8"))
        elif path == "/redirect-ok":
            self._send(302, b"", extra={"location": "/article"})
        elif path == "/redirect-private":
            self._send(302, b"", extra={"location": "http://169.254.169.254/latest/meta-data/"})
        elif path == "/redirect-loop":
            self._send(302, b"", extra={"location": "/redirect-loop"})
        else:
            self._send(404, b"nothing here")


@pytest.fixture
def stub(live):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fetch._TEST_ALLOWED_ADDRS.add(f"127.0.0.1:{port}")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        fetch._TEST_ALLOWED_ADDRS.discard(f"127.0.0.1:{port}")


def test_html_extraction_returns_the_body_and_the_accounting(stub):
    reply = fetch.fetch_source(f"{stub}/article")
    assert reply["ok"] is True, reply
    page = reply["data"]
    assert set(page) == PAGE_TEXT_KEYS
    assert "25-hydroxyvitamin D" in page["text"]
    assert "skip me" not in page["text"]
    assert page["is_pdf"] is False
    assert page["truncated"] is False
    assert page["source_truncated"] is False
    assert page["text_truncated"] is False
    assert page["returned_chars"] == len(page["text"]) == page["original_chars"]


def test_max_chars_truncation_is_declared(stub):
    reply = fetch.fetch_source(f"{stub}/article", max_chars=500)
    page = reply["data"]
    assert page["returned_chars"] == 500
    assert page["original_chars"] > 500
    assert page["text_truncated"] is True
    assert page["source_truncated"] is False
    assert page["truncated"] is True


def test_max_chars_is_clamped_and_reported(stub):
    reply = fetch.fetch_source(f"{stub}/article", max_chars=1)
    assert reply["request"]["max_chars"] == fetch.MAX_CHARS_MIN
    assert any("clamped" in note for note in reply["request"]["normalized_args"]["notes"])


def test_a_response_cut_at_the_byte_cap_says_so(stub):
    reply = fetch.fetch_source(f"{stub}/big", max_chars=fetch.MAX_CHARS_MAX)
    assert reply["ok"] is True, reply
    page = reply["data"]
    assert page["source_truncated"] is True
    assert page["truncated"] is True


def test_a_refusal_is_typed_and_points_at_the_snippet(stub):
    reply = fetch.fetch_source(f"{stub}/forbidden")
    assert reply["error_code"] == errors.FORBIDDEN
    assert reply["retryable"] is False
    assert reply["hint"]["fallback"] == "use_snippet"


def test_a_missing_document_is_typed(stub):
    reply = fetch.fetch_source(f"{stub}/missing")
    assert reply["error_code"] == errors.NOT_FOUND
    assert reply["retryable"] is False


def test_a_source_rate_limit_is_retryable(stub):
    reply = fetch.fetch_source(f"{stub}/ratelimited")
    assert reply["error_code"] == errors.RATE_LIMITED
    assert reply["retryable"] is True


def test_a_non_document_content_type_is_typed(stub):
    reply = fetch.fetch_source(f"{stub}/binary")
    assert reply["error_code"] == errors.UNSUPPORTED_CONTENT


def test_a_page_with_no_body_text_is_typed(stub):
    reply = fetch.fetch_source(f"{stub}/empty")
    assert reply["error_code"] == errors.EMPTY_CONTENT


def test_an_unreadable_pdf_is_typed(stub):
    reply = fetch.fetch_source(f"{stub}/badpdf")
    assert reply["error_code"] in (errors.PARSE_FAILED, errors.EMPTY_CONTENT)
    assert reply["retryable"] is False


def test_a_slow_source_times_out_and_says_to_use_the_snippet(stub, monkeypatch):
    monkeypatch.setattr(fetch, "HTML_TIMEOUT_S", 0.4)
    reply = fetch.fetch_source(f"{stub}/slow")
    assert reply["error_code"] == errors.TIMEOUT
    assert reply["retryable"] is True
    assert reply["hint"]["fallback"] == "use_snippet"


def test_a_redirect_within_policy_is_followed(stub):
    reply = fetch.fetch_source(f"{stub}/redirect-ok")
    assert reply["ok"] is True
    assert reply["data"]["url"].endswith("/article")


def test_a_redirect_into_private_space_is_refused(stub):
    reply = fetch.fetch_source(f"{stub}/redirect-private")
    assert reply["ok"] is False
    assert reply["error_code"] == errors.BLOCKED_URL
    assert "link-local" in reply["error"]


def test_a_redirect_loop_ends(stub):
    reply = fetch.fetch_source(f"{stub}/redirect-loop")
    assert reply["error_code"] == errors.TOO_MANY_REDIRECTS


def test_a_success_is_cached_and_a_transient_failure_is_not(stub):
    fetch.fetch_source(f"{stub}/article")
    again = fetch.fetch_source(f"{stub}/article")
    assert again["cached"] is True
    fetch.clear_cache()
    fetch.fetch_source(f"{stub}/ratelimited")
    once_more = fetch.fetch_source(f"{stub}/ratelimited")
    assert once_more.get("cached") is not True


# --------------------------------------------------------------------------
# fetch: offline
# --------------------------------------------------------------------------


def test_a_registered_source_returns_its_body():
    reply = fetch.fetch_source("https://www.bmj.com/content/356/bmj.i6583")
    assert reply["ok"] is True
    page = reply["data"]
    assert set(page) == PAGE_TEXT_KEYS
    assert "25 nmol/L" in page["text"]
    assert reply["source_mode"] == source_mode.MOCK


def test_a_pdf_fixture_is_flagged_as_one():
    reply = fetch.fetch_source("https://www.bmj.com/content/bmj/359/bmj.j5024.full.pdf", max_chars=20000)
    assert reply["data"]["is_pdf"] is True
    assert "Abstract" in reply["data"]["text"]


def test_an_unregistered_url_gets_generic_text_rather_than_a_failure():
    reply = fetch.fetch_source("https://academic.oup.com/eurheartj/article/46/8/749/7928425")
    assert reply["ok"] is True, reply
    assert len(reply["data"]["text"]) > 200
    assert "academic.oup.com" in reply["data"]["title"]


def test_a_fabricated_host_still_fails():
    for url in (
        "https://harvard-global-hydration.org/2025-study",
        "http://sleepfoundation-miracle.org/miracle-coffee.pdf",
        "https://krsleep-miracle.or.kr/report",
    ):
        reply = fetch.fetch_source(url)
        assert reply["ok"] is False, url
        assert reply["error_code"] == errors.NOT_FOUND


def test_offline_truncation_accounting_is_honest():
    reply = fetch.fetch_source("https://www.bmj.com/content/356/bmj.i6583", max_chars=600)
    page = reply["data"]
    assert page["returned_chars"] == 600
    assert page["text_truncated"] is True
    assert page["source_truncated"] is False
    assert page["original_chars"] > 600


def test_fetching_counts_against_the_fetch_endpoint():
    fetch.fetch_source("https://www.bmj.com/content/356/bmj.i6583")
    fetch.fetch_source("https://harvard-global-hydration.org/2025-study")
    snapshot = usage.snapshot()
    assert snapshot["fetch"]["calls"] == 2
    assert snapshot["fetch"]["failures"] == 1


# --------------------------------------------------------------------------
# the offline scenario has to survive end to end
# --------------------------------------------------------------------------


def test_the_scenario_paragraph_matches_its_sentences(scenario):
    assert " ".join(scenario["sentences"]) == scenario["input_text"]
    assert len(scenario["claims"]) == scenario["expected_path"]["claims"] == 5


@pytest.mark.parametrize("row", json.loads((FIXTURE_DIR / "scenario.json").read_text(encoding="utf-8"))["query_expectations"])
def test_every_documented_query_still_behaves_that_way(row):
    search = liner.search_web if row["endpoint"] == "web" else liner.search_scholar
    reply = search(row["query"])
    assert reply["ok"] is True, row
    if row["expect"] == "empty":
        assert reply["data"] == [], row
    else:
        assert reply["data"], row


def test_the_two_fabricated_sources_end_at_the_existence_axis(scenario):
    for claim in scenario["claims"]:
        if claim["axis1"]["outcome"] != "fail":
            continue
        for query in claim["axis1"]["queries"]:
            liner.clear_cache()
            assert liner.search_web(query)["data"] == [], claim["id"]
            assert liner.search_scholar(query)["data"] == [], claim["id"]


def test_the_surviving_claims_find_their_sources(scenario):
    survivors = [claim for claim in scenario["claims"] if claim["axis1"]["outcome"] == "pass"]
    assert len(survivors) == 3
    for claim in survivors:
        for query in claim["axis1"]["queries"]:
            liner.clear_cache()
            reply = liner.search_scholar(query)
            assert reply["data"], claim["id"]
            assert claim["axis1"]["supporting_fixture"].endswith(reply["request"]["fixture_rule"])


def test_the_fidelity_axis_can_read_a_source_that_contradicts_its_claim(scenario):
    claim = next(item for item in scenario["claims"] if item["axis2"].get("verdict") == "unsupported")
    reply = fetch.fetch_source(claim["axis2"]["fetch"], max_chars=20000)
    assert reply["ok"] is True
    text = reply["data"]["text"]
    # The source is real and on topic, and it says something narrower than the claim.
    assert "below 25 nmol/L" in text
    assert "do not support" in text


def test_the_fidelity_axis_can_also_confirm(scenario):
    supported = [item for item in scenario["claims"] if item["axis2"].get("verdict") == "supported"]
    assert len(supported) == 2
    for claim in supported:
        reply = fetch.fetch_source(claim["axis2"]["fetch"], max_chars=20000)
        assert reply["ok"] is True, claim["id"]
        assert len(reply["data"]["text"]) > 300


def test_the_completeness_axis_finds_its_two_counter_documents(scenario):
    omission_urls = {row["url"] for row in scenario["omissions"]}
    assert len(omission_urls) == scenario["expected_path"]["omissions"] == 2

    found = set()
    for query in ("coffee pregnancy risk", "green tea blood pressure meta-analysis"):
        liner.clear_cache()
        reply = liner.search_scholar(query)
        found.update(item["url"] for item in reply["data"])
    assert omission_urls <= found


def test_the_whole_offline_scenario_runs_without_the_network(scenario):
    """Walk the documented route once and check the shape of what comes back."""
    axis1_failures = 0
    axis2_unsupported = 0
    omissions = 0

    for claim in scenario["claims"]:
        if claim["axis1"]["outcome"] == "fail":
            hits = sum(len(liner.search_web(query)["data"]) for query in claim["axis1"]["queries"])
            assert hits == 0
            axis1_failures += 1
            continue

        assert liner.search_scholar(claim["axis1"]["queries"][0])["data"]
        body = fetch.fetch_source(claim["axis2"]["fetch"], max_chars=20000)
        assert body["ok"] is True
        if claim["axis2"].get("verdict") == "unsupported":
            axis2_unsupported += 1
        if claim["axis3"].get("omission"):
            omissions += 1

    assert axis1_failures == scenario["expected_path"]["axis1_fail"] == 2
    assert axis2_unsupported == scenario["expected_path"]["axis2_unsupported"] == 1
    assert omissions == scenario["expected_path"]["omissions"] == 2
    assert usage.snapshot()["fetch"]["calls"] == 3
