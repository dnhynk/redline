"""모델에 노출되는 툴 7종 + 예산 봉투.

원칙: 툴은 "모델이 못 하는 일"만 한다 = **네트워크 IO 3종 + 호스트 상태 기록 4종**.
분류·추출·대조·판정은 전부 모델이 한다. 툴은 모델이 내린 판단을 받아 적고 예산을 돌려준다.

모든 반환은 같은 봉투다: `{ok, data, error, budget}`. 기록 툴의 판정 dict는 `data` 안에
들어간다 — 봉투가 다르면 모델이 기록 실패를 알아채지 못한다.
"""

from __future__ import annotations

import importlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from agents import RunContextWrapper, function_tool

from core.audit import (
    DEFAULT_MAX_CLAIMS,
    MAX_RECORD_CALLS,
    Audit,
    plan_claim_budget,
)

# ── IO 층 연결 — tools/가 없으면 로컬 스텁으로 진행한다 ──────────────────────
try:  # pragma: no cover - 병합 상태에 따라 갈린다
    from tools import afetch_source as _io_fetch_source
    from tools import asearch_scholar as _io_search_scholar
    from tools import asearch_web as _io_search_web

    TOOLS_STUBBED = False
except Exception:  # ImportError 포함 — io-layer 미병합 상태
    TOOLS_STUBBED = True

    def _h(s: str) -> int:
        """결정적 해시 — 같은 질의는 언제나 같은 스텁 결과."""
        n = 0
        for ch in s:
            n = (n * 131 + ord(ch)) % 1_000_003
        return n

    async def _io_search_web(
        query: str,
        *,
        max_results: int = 8,
        lang: str | None = None,
        date_range: str | None = None,
    ) -> dict:
        return _stub_search(query, "web", max_results, lang, date_range)

    async def _io_search_scholar(
        query: str,
        *,
        max_results: int = 8,
        lang: str | None = None,
        date_range: str | None = None,
    ) -> dict:
        return _stub_search(query, "scholar", max_results, lang, date_range)

    async def _io_fetch_source(url: str, *, max_chars: int = 8000) -> dict:
        body = (
            f"[개발 스텁 본문] {url} 에서 가져온 것이 아니라 io 층 없이 코어를 돌리기 위한 "
            "결정적 자리채움 텍스트다. 실데이터가 아니다."
        )
        return {
            "ok": True,
            "error": None,
            "source_mode": "mock",
            "data": {
                "url": url,
                "title": f"[stub] {url}",
                "text": body[:max_chars],
                "is_pdf": False,
                "truncated": False,
                "source_truncated": False,
                "text_truncated": False,
                "original_chars": len(body),
                "returned_chars": min(len(body), max_chars),
            },
        }

    def _stub_search(
        query: str, source: str, max_results: int, lang: str | None, date_range: str | None
    ) -> dict:
        seed = _h(f"{source}:{query}")
        n = max(1, min(int(max_results or 3), 3))
        results = []
        for i in range(n):
            key = (seed + i * 7919) % 1_000_003
            results.append(
                {
                    "title": f"[stub] {query[:60]} — 결과 {i + 1}",
                    "url": f"https://stub.invalid/{source}/{key}",
                    "hostname": "stub.invalid",
                    "description": (
                        f"io 층 없이 코어를 돌리기 위한 개발 스텁 결과다({source}). "
                        f"질의: {query[:80]}"
                    ),
                    "date": None,
                    "source": source,
                    "citation_count": (key % 500) if source == "scholar" else None,
                    "authors": None,
                    "journal": None,
                }
            )
        return {
            "ok": True,
            "error": None,
            "data": results,
            "source_mode": "mock",
            "result_status": "ok" if results else "empty",
            "total_count": len(results),
            "cached": False,
            "request": {"query": query, "lang": lang, "date_range": date_range},
        }


def resolve_source_mode() -> str:
    """live / mock / unknown — 넘겨짚지 않는다. 못 얻으면 unknown으로 남긴다."""
    if TOOLS_STUBBED:
        # 로컬 스텁은 정의상 실데이터가 아니다. mock을 실데이터로 위장하지 않는다.
        return "mock"
    for name in ("tools.source_mode", "tools.liner", "tools"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        for attr in ("get_source_mode", "current_mode"):
            fn = getattr(mod, attr, None)
            if callable(fn):
                try:
                    value = fn()
                except Exception:
                    continue
                if value in ("live", "mock"):
                    return value
    return "unknown"


def get_endpoint_calls() -> dict | None:
    """엔드포인트별 호출 카운터. 카운터가 없으면 None — 봉투에서 키 자체가 빠진다."""
    if TOOLS_STUBBED:
        return None
    try:
        mod = importlib.import_module("tools")
    except Exception:
        return None
    fn = getattr(mod, "get_endpoint_calls", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def reset_endpoint_calls() -> None:
    if TOOLS_STUBBED:
        return
    try:
        mod = importlib.import_module("tools")
    except Exception:
        return
    fn = getattr(mod, "reset_endpoint_calls", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


# ── 상수 ─────────────────────────────────────────────────────────────────────
# Liner 공표 단가 (USD / 1,000 요청, 성공 호출만 과금). scholar가 web보다 3.3배 싸다 —
# 팬아웃을 web으로 하는 이유는 가격이 아니라 처리량(scholar 2 QPS)이다.
SEARCH_PRICE_PER_1K_USD = {"web": 1.0, "scholar": 0.3, "fetch": 0.0}

# 타임박스의 이 비율을 넘으면 새 fetch를 시작하지 않는다 — fetch는 검색보다 몇 배 느리다.
FETCH_CUTOFF_FRACTION = 0.6

MAX_RESULTS_RANGE = (1, 20)
MAX_CHARS_RANGE = (500, 20000)

# 실패 봉투의 구조화 정보 — 이 키들을 버리면 모델이 실패에 대응할 근거를 잃는다.
_IO_PASSTHROUGH = (
    "error_code",
    "retryable",
    "hint",
    "request",
    # provenance — 이것을 버리면 모델은 픽스처 검색을 실검색으로 알고 최종 보고에
    # 사실 검증처럼 쓴다. 화면만 mock 배너를 달고 보고서 문장은 모른다.
    "source_mode",
    "cached",
    "result_status",
    "total_count",
)

DateRange = Literal["past_day", "past_week", "past_month", "past_year"]
Stance = Literal["support", "challenge"]


# ── 실행 문맥 ────────────────────────────────────────────────────────────────
@dataclass
class AuditContext:
    """한 런의 예산·회계·이벤트 방출 통로. 툴은 여기를 통해서만 상태를 만진다."""

    audit: Audit
    timebox_s: float = 90.0
    max_tool_calls: int = 30
    max_claims: int = DEFAULT_MAX_CLAIMS
    max_record_calls: int = MAX_RECORD_CALLS
    clock: Callable[[], float] = time.monotonic
    on_audit_event: Callable[[dict], None] | None = None

    tool_calls_used: int = 0
    tool_calls_refused: int = 0
    fetch_calls_used: int = 0
    record_calls_used: int = 0
    record_calls_refused: int = 0
    axis_reached: int = 0
    non_auditable: bool = False
    started_at: float = 0.0
    max_concurrent_tools: int = 0
    audit_events_suppressed: int = 0
    _last_snapshot_key: str | None = None
    _spans: list[list[float | None]] = field(default_factory=list)
    _open_calls: int = 0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()

    # ── 시간 ────────────────────────────────────────────────────────────
    def elapsed_s(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def remaining_s(self) -> float:
        return max(0.0, self.timebox_s - self.elapsed_s())

    def time_fraction(self) -> float:
        if self.timebox_s <= 0:
            return 1.0
        return min(1.0, self.elapsed_s() / self.timebox_s)

    @property
    def fetch_cutoff_s(self) -> float:
        return round(self.timebox_s * FETCH_CUTOFF_FRACTION, 3)

    def fetch_cutoff_passed(self) -> bool:
        return self.elapsed_s() >= self.fetch_cutoff_s

    def fetch_allowed(self) -> bool:
        return not self.fetch_cutoff_passed() and self.tool_calls_used < self.max_tool_calls

    # ── 툴 대기 회계 (F14) ──────────────────────────────────────────────
    # 동시 발사된 호출을 각자 더하면 벽시계 5초가 30초로 계상된다. 진행 중 구간의
    # **합집합**을 재고, 직렬 합계는 따로 남긴다 — 둘의 비가 곧 병렬 효율이다.
    def open_tool_call(self) -> list[float | None]:
        span: list[float | None] = [self.clock(), None]
        self._spans.append(span)
        self._open_calls += 1
        self.max_concurrent_tools = max(self.max_concurrent_tools, self._open_calls)
        return span

    def close_tool_call(self, span: list[float | None]) -> None:
        if span[1] is None:
            span[1] = self.clock()
            self._open_calls = max(0, self._open_calls - 1)

    def close_open_spans(self) -> None:
        """취소·종결 시 열린 구간을 닫는다 — 안 닫으면 대기 시간이 영원히 자란다."""
        now = self.clock()
        for span in self._spans:
            if span[1] is None:
                span[1] = now
        self._open_calls = 0

    def tool_wait_s(self) -> float:
        now = self.clock()
        spans = sorted(
            (float(s), float(e if e is not None else now)) for s, e in self._spans
        )
        total, cur_start, cur_end = 0.0, None, None
        for start, end in spans:
            if cur_start is None:
                cur_start, cur_end = start, end
            elif start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                total += cur_end - cur_start
                cur_start, cur_end = start, end
        if cur_start is not None:
            total += cur_end - cur_start
        return round(total, 3)

    def tool_time_serial_s(self) -> float:
        now = self.clock()
        return round(
            sum(float(e if e is not None else now) - float(s) for s, e in self._spans), 3
        )

    def parallel_speedup(self) -> float:
        union = self.tool_wait_s()
        if union <= 0:
            return 1.0
        return round(self.tool_time_serial_s() / union, 3)

    # ── 예산 차감 ───────────────────────────────────────────────────────
    def charge_call(self, *, is_fetch: bool = False) -> bool:
        """네트워크 호출 1건. 상한 초과는 **그 호출만** 거부하고 런은 계속된다."""
        if self.tool_calls_used >= self.max_tool_calls:
            self.tool_calls_refused += 1
            return False
        self.tool_calls_used += 1
        if is_fetch:
            self.fetch_calls_used += 1
        return True

    def charge_record(self) -> bool:
        """기록 호출은 예산을 쓰지 않는다. 폭주 방지 소프트 상한만 있다."""
        if self.record_calls_used >= self.max_record_calls:
            self.record_calls_refused += 1
            return False
        self.record_calls_used += 1
        return True

    def note_source_mode(self, mode: str) -> None:
        """IO가 보고한 provenance를 런 상태에 반영한다. mock은 live로 되돌아가지 않는다."""
        if mode == "mock" or self.audit.source_mode not in ("live", "mock"):
            self.audit.source_mode = mode

    def emit_audit(self) -> None:
        """상태가 실제로 바뀌었을 때만 방출한다.

        기록 툴은 예산을 쓰지 않으므로 상한(150회)까지 재호출이 가능하고, 그 하나하나가
        전 문장·전 클레임 스냅샷이 되어 릴레이와 화면에 쌓인다. 같은 판정을 다시 기록한
        호출은 상태를 바꾸지 않는다 — 없는 상태 변화를 그리지 않는다는 규칙의 연장이다.
        """
        if self.on_audit_event is None:
            return
        snapshot = self.audit.to_dict(stream=True)
        key = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
        if key == self._last_snapshot_key:
            self.audit_events_suppressed += 1
            return
        self._last_snapshot_key = key
        self.on_audit_event(snapshot)

    # ── 봉투 ────────────────────────────────────────────────────────────
    def budget_snapshot(self) -> dict:
        remaining_calls = max(0, self.max_tool_calls - self.tool_calls_used)
        claims_remaining = max(0, self.max_claims - len(self.audit.claims))
        snap: dict[str, Any] = {
            "elapsed_s": round(self.elapsed_s(), 2),
            "timebox_s": self.timebox_s,
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_max": self.max_tool_calls,
            "tool_calls_refused": self.tool_calls_refused,
            "fetch_calls_used": self.fetch_calls_used,
            "record_calls_used": self.record_calls_used,
            "record_calls_max": self.max_record_calls,
            "record_calls_refused": self.record_calls_refused,
            "claims_recorded": len(self.audit.claims),
            "max_claims": self.max_claims,
            "axis_reached": self.axis_reached,
            "remaining_tool_calls": remaining_calls,
            "remaining_s": round(self.remaining_s(), 2),
            "time_fraction": round(self.time_fraction(), 3),
            "claims_remaining": claims_remaining,
            "fetch_allowed": self.fetch_allowed(),
            "fetch_cutoff_s": self.fetch_cutoff_s,
            # 이번 런의 검색·페치가 실데이터인가 픽스처인가. 보고서 문장이 이것을 알아야 한다.
            "source_mode": self.audit.source_mode,
            # 지금까지 나간 반증 쿼리 수 — **관측치이지 할당량이 아니다.**
            # 필요치(더 쏴야 하는 수)를 여기 실었더니 모델이 정확히 그 수에서 멈췄고
            # 누락 증거가 8.00 → 4.33으로 무너졌다. 하한을 알려주면 하한이 상한이 된다.
            "challenge_queries": self.audit.challenge_query_count(),
        }
        endpoint_calls = get_endpoint_calls()
        if endpoint_calls is not None:
            snap["endpoint_calls"] = endpoint_calls
            snap["search_price_per_1k_usd"] = dict(SEARCH_PRICE_PER_1K_USD)
            snap["estimated_search_cost_usd"] = round(
                estimate_search_cost(endpoint_calls), 6
            )
        return snap

    def claim_budget(self) -> int:
        """클레임 하나에 남는 네트워크 호출 수 — 등록된 클레임이 늘수록 줄어드는 신호다."""
        remaining_calls = max(0, self.max_tool_calls - self.tool_calls_used)
        return plan_claim_budget(remaining_calls, len(self.audit.claims) or 1)


def estimate_search_cost(endpoint_calls: dict) -> float:
    """성공 호출만 과금 — 캐시 적중과 실패는 빼고 단가를 곱한다."""
    total = 0.0
    for endpoint, price in SEARCH_PRICE_PER_1K_USD.items():
        row = endpoint_calls.get(endpoint) or {}
        billable = (
            int(row.get("calls", 0))
            - int(row.get("cache_hits", 0))
            - int(row.get("failures", 0))
        )
        total += max(0, billable) * price / 1000.0
    return total


# ── 봉투 조립 ────────────────────────────────────────────────────────────────
def _reply(
    ctx: AuditContext,
    *,
    ok: bool,
    data: Any = None,
    error: str | None = None,
    passthrough: dict | None = None,
) -> dict:
    envelope: dict[str, Any] = {"ok": ok, "data": data, "error": error}
    for key in _IO_PASSTHROUGH:
        if passthrough and key in passthrough:
            envelope[key] = passthrough[key]
    # IO가 방금 무엇으로 답했는지가 런의 진실이다 — mock이 한 번이라도 섞이면 mock이 이긴다.
    reported = (passthrough or {}).get("source_mode")
    if reported in ("live", "mock"):
        ctx.note_source_mode(reported)
    envelope.setdefault("source_mode", ctx.audit.source_mode)
    envelope["budget"] = ctx.budget_snapshot()
    return envelope


def _record_reply(ctx: AuditContext, result: dict) -> dict:
    """기록 툴의 판정 dict를 IO와 같은 봉투에 담는다 (A-08)."""
    return _reply(
        ctx,
        ok=bool(result.get("ok")),
        data=result.get("data"),
        error=result.get("error"),
    )


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return lo if value < lo else (hi if value > hi else value)


def _normalize_io_args(**pairs: tuple[int, int, int]) -> tuple[dict, dict]:
    """(정규화된 값, 잘랐다는 기록). 조용한 clamp를 남기지 않는다 (A-07)."""
    used, normalized = {}, {}
    for name, (given, lo, hi) in pairs.items():
        fixed = _clamp_int(int(given), lo, hi)
        used[name] = fixed
        if fixed != given:
            normalized[name] = {"given": given, "used": fixed}
    return used, normalized


def _register_search_evidence(
    audit: Audit, tool: str, query: str, results: Sequence[dict], stance: str = "support"
) -> list[dict]:
    """검색 결과 각 건을 원장에 넣고 evidence_id를 주입해 돌려준다."""
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        item = dict(r)
        url = item.get("url")
        if url:
            rec = audit.register_evidence(
                tool=tool,
                query=query,
                url=url,
                title=item.get("title") or "",
                snippet=item.get("description") or "",
                stance=stance,
                extra={
                    k: item.get(k)
                    for k in ("date", "citation_count", "authors", "journal", "hostname", "source")
                },
            )
            item["evidence_id"] = rec["id"]
            item["stance"] = rec["stance"]
        out.append(item)
    return out


def _register_fetch_evidence(audit: Audit, url: str, page: dict) -> dict:
    item = dict(page)
    target = item.get("url") or url
    rec = audit.register_evidence(
        tool="fetch_source",
        query=url,
        url=target,
        title=item.get("title") or "",
        snippet=item.get("text") or "",
        extra={
            "is_pdf": item.get("is_pdf"),
            "truncated": item.get("truncated"),
            "original_chars": item.get("original_chars"),
        },
    )
    item["evidence_id"] = rec["id"]
    return item


async def _io_call(
    ctx: AuditContext, coro_factory: Callable[[], Any], *, is_fetch: bool = False
) -> dict:
    span = ctx.open_tool_call()
    try:
        return await coro_factory()
    except Exception as exc:  # IO는 예외를 던지지 않기로 돼 있지만 루프가 죽으면 안 된다
        return {"ok": False, "data": None, "error": f"tool_error: {exc}", "retryable": False}
    finally:
        ctx.close_tool_call(span)


# ── 네트워크 3종 (예산 대상) ─────────────────────────────────────────────────
@function_tool
async def search_web(
    wrapper: RunContextWrapper[AuditContext],
    query: str,
    claim_id: str,
    stance: Stance,
    max_results: int = 8,
    lang: str | None = None,
    date_range: DateRange | None = None,
) -> dict:
    """웹을 검색한다. 넓은 탐색·존재 확인·반증 후보 훑기의 기본 엔드포인트다.

    단가 $1/1k 요청으로 scholar($0.3)보다 비싸지만 QPS 제한이 없어 여러 클레임의
    확증·반증 검색을 한 번에 동시 발사할 수 있다 — 아껴야 하는 것은 돈이 아니라 시간이다.
    결과 각 건에는 호스트가 발급한 evidence_id가 붙어 온다.

    Args:
        claim_id: 이 검색이 어느 클레임의 일인가 ("C1"…). 아직 클레임을 고르는 중이면 "explore"
        stance: 이 검색의 방향. support=주장을 뒷받침할 자료, challenge=반박·한정할 자료.
            쿼리 자체가 달라야 한다 — 라벨만 바꾼 같은 쿼리는 반증 검색이 아니다
        max_results: 최대 결과 수 (1~20, 호스트 클램프)
        lang: ISO 639-1 (en, ko). 생략(null) 시 자동
        date_range: 기간 필터. 생략(null) 시 전 기간
    """
    ctx = wrapper.context
    # 귀속·중복 검사는 예산을 깎기 전에 한다 — 거부된 검색이 남은 호출 수를 갉아먹지 않는다.
    refusal = ctx.audit.check_search(claim_id, stance, query)
    if refusal is not None:
        ctx.tool_calls_refused += 1
        return _reply(ctx, ok=False, error=refusal["error"], data=refusal["data"])
    if not ctx.charge_call():
        return _reply(
            ctx,
            ok=False,
            error=f"네트워크 호출 상한 {ctx.max_tool_calls}회를 넘겼다 — 이 호출은 거부됐다. "
            "이미 모은 증거로 판정을 마무리하라(기록 툴은 계속 쓸 수 있다).",
        )
    used, normalized = _normalize_io_args(max_results=(max_results, *MAX_RESULTS_RANGE))
    entry = ctx.audit.note_search(stance, query, claim_id=claim_id, endpoint="web")
    reply = await _io_call(
        ctx,
        lambda: _io_search_web(
            query, max_results=used["max_results"], lang=lang, date_range=date_range
        ),
    )
    return _search_reply(ctx, "search_web", query, reply, normalized, stance, entry)


@function_tool
async def search_scholar(
    wrapper: RunContextWrapper[AuditContext],
    query: str,
    claim_id: str,
    stance: Stance,
    max_results: int = 8,
    lang: str | None = None,
    date_range: DateRange | None = None,
) -> dict:
    """학술 문헌을 검색한다. 논문 제목·저자·인용 수가 필요할 때 쓴다.

    단가 $0.3/1k 요청으로 전 상품 최저가지만 **2 QPS 제한**이 있어 동시 발사에서
    대기가 타임박스를 먹는다 — 팬아웃은 search_web으로, scholar는 소수 정예 질의로.
    쿼리는 영어로 쓰는 것이 회수율이 높다.

    Args:
        query: 검색 질의 (영어 권장)
        claim_id: 이 검색이 어느 클레임의 일인가 ("C1"…). 아직 클레임을 고르는 중이면 "explore"
        stance: 이 검색의 방향. support=주장을 뒷받침할 자료, challenge=반박·한정할 자료
            (limitations · contrary · no effect · systematic review 류가 들어간다)
        max_results: 최대 결과 수 (1~20, 호스트 클램프)
        lang: ISO 639-1 (en, ko). 생략(null) 시 자동
        date_range: 기간 필터. 생략(null) 시 전 기간
    """
    ctx = wrapper.context
    # 귀속·중복 검사는 예산을 깎기 전에 한다 — 거부된 검색이 남은 호출 수를 갉아먹지 않는다.
    refusal = ctx.audit.check_search(claim_id, stance, query)
    if refusal is not None:
        ctx.tool_calls_refused += 1
        return _reply(ctx, ok=False, error=refusal["error"], data=refusal["data"])
    if not ctx.charge_call():
        return _reply(
            ctx,
            ok=False,
            error=f"네트워크 호출 상한 {ctx.max_tool_calls}회를 넘겼다 — 이 호출은 거부됐다. "
            "이미 모은 증거로 판정을 마무리하라(기록 툴은 계속 쓸 수 있다).",
        )
    used, normalized = _normalize_io_args(max_results=(max_results, *MAX_RESULTS_RANGE))
    entry = ctx.audit.note_search(stance, query, claim_id=claim_id, endpoint="scholar")
    reply = await _io_call(
        ctx,
        lambda: _io_search_scholar(
            query, max_results=used["max_results"], lang=lang, date_range=date_range
        ),
    )
    return _search_reply(ctx, "search_scholar", query, reply, normalized, stance, entry)


def _search_reply(
    ctx: AuditContext,
    tool: str,
    query: str,
    reply: dict,
    normalized: dict,
    stance: str,
    entry: dict | None = None,
) -> dict:
    passthrough = dict(reply)
    request = dict(passthrough.get("request") or {})
    if normalized:
        request["normalized_args"] = normalized
    if request:
        passthrough["request"] = request
    if not reply.get("ok"):
        ctx.audit.mark_search_result(entry, False, 0)
        return _reply(ctx, ok=False, error=reply.get("error"), passthrough=passthrough)
    results = reply.get("data") or []
    if not isinstance(results, list):
        ctx.audit.mark_search_result(entry, False, 0)
        return _reply(
            ctx, ok=False, error=f"tool_error: 예상하지 못한 반환 형식({type(results).__name__})"
        )
    enriched = _register_search_evidence(ctx.audit, tool, query, results, stance)
    ctx.audit.mark_search_result(entry, True, len(enriched))
    return _reply(
        ctx,
        ok=True,
        data={"results": enriched, "result_count": len(enriched), "stance": stance},
        passthrough=passthrough,
    )


@function_tool
async def fetch_source(
    wrapper: RunContextWrapper[AuditContext],
    url: str,
    max_chars: int = 8000,
) -> dict:
    """URL의 본문을 가져온다. 축2 대조가 스니펫으로 서지 않을 때만 쓰는 최후 수단이다.

    API 과금은 없지만 지연이 가장 크다 — 비용이 돈이 아니라 시간인 경우다.
    반환의 truncated/original_chars/returned_chars로 본문이 잘렸는지 반드시 확인하라.
    결과에는 호스트가 발급한 evidence_id가 붙어 온다.

    Args:
        max_chars: 가져올 최대 글자 수 (500~20000, 호스트 클램프)
    """
    ctx = wrapper.context
    # 컷오프는 봉투에 싣기만 하는 부탁이 아니라 호스트가 집행하는 규칙이다. 예산을 깎기
    # 전에 막아야 거부된 호출이 남은 호출 수를 갉아먹지 않는다.
    if ctx.fetch_cutoff_passed():
        ctx.tool_calls_refused += 1
        return _reply(
            ctx,
            ok=False,
            error=f"타임박스 {int(FETCH_CUTOFF_FRACTION * 100)}%({ctx.fetch_cutoff_s}s)를 지나 "
            "새 fetch는 거부됐다 — 남은 시간은 판정과 축3에 쓴다. 이미 받은 스니펫으로 판정하라.",
        )
    if not ctx.charge_call(is_fetch=True):
        return _reply(
            ctx,
            ok=False,
            error=f"네트워크 호출 상한 {ctx.max_tool_calls}회를 넘겼다 — 이 호출은 거부됐다. "
            "이미 모은 증거로 판정을 마무리하라(기록 툴은 계속 쓸 수 있다).",
        )
    used, normalized = _normalize_io_args(max_chars=(max_chars, *MAX_CHARS_RANGE))
    reply = await _io_call(
        ctx, lambda: _io_fetch_source(url, max_chars=used["max_chars"]), is_fetch=True
    )
    passthrough = dict(reply)
    if normalized:
        request = dict(passthrough.get("request") or {})
        request["normalized_args"] = normalized
        passthrough["request"] = request
    if not reply.get("ok"):
        return _reply(ctx, ok=False, error=reply.get("error"), passthrough=passthrough)
    page = reply.get("data")
    if not isinstance(page, dict):
        return _reply(
            ctx, ok=False, error=f"tool_error: 예상하지 못한 반환 형식({type(page).__name__})"
        )
    return _reply(
        ctx, ok=True, data=_register_fetch_evidence(ctx.audit, url, page), passthrough=passthrough
    )


# ── 기록 4종 (예산 0) ────────────────────────────────────────────────────────
_RECORD_CAP_ERROR = (
    "기록 호출 소프트 상한에 닿았다 — 같은 판정을 반복 호출하고 있다. "
    "새 기록 대신 최종 보고를 써라."
)


@function_tool
async def record_classification(
    wrapper: RunContextWrapper[AuditContext],
    input_kind: Literal[
        "ai_answer",
        "research_report",
        "academic_paragraph",
        "news_or_blog",
        "opinion_or_creative",
        "unknown",
    ],
    lang: str,
    auditable: bool,
    sentence_count: int,
    rationale: str,
) -> dict:
    """입력이 어떤 종류의 일인지 먼저 결정해 기록한다. **첫 툴 호출은 반드시 이것이다.**

    반환에 host_sentences(index → 문장 + kind)가 들어 있다 — 그것이 하이라이트 좌표계의
    정본이다. 네가 센 문장 수가 아니라 호스트 좌표를 써라.

    Args:
        lang: ISO 639-1 (ko, en …)
        auditable: 사실 주장이 들어 있어 감사가 가능한가
        sentence_count: 모델이 센 문장 수 — 호스트 분할과 다르면 호스트가 정본
        rationale: 이 분류의 근거 한 문장
    """
    ctx = wrapper.context
    if not ctx.charge_record():
        return _reply(ctx, ok=False, error=_RECORD_CAP_ERROR)
    result = ctx.audit.record_classification(
        input_kind=input_kind,
        lang=lang,
        auditable=auditable,
        sentence_count=sentence_count,
        rationale=rationale,
    )
    if not auditable:
        ctx.non_auditable = True
    if result.get("ok"):
        ctx.emit_audit()
    return _record_reply(ctx, result)


@function_tool
async def record_claim(
    wrapper: RunContextWrapper[AuditContext],
    index: int,
    text: str,
    claim_type: Literal["statistical", "causal", "attribution", "definitional", "normative"],
    auditable: bool,
    cited_source: str | None = None,
) -> dict:
    """감사할 클레임 하나를 호스트 문장 좌표 위에 등록한다.

    호스트가 text를 sentences[index]와 대조한다. 어긋나면 등록이 거부되고
    data.index_candidates에 좌표 후보가 온다 — 거부는 실패가 아니라 정정 신호다.
    모든 클레임은 같은 신뢰도에서 시작한다 — 시작값을 정하는 파라미터는 없다.

    Args:
        index: 호스트 문장 좌표 (0부터, 하이라이트 앵커). host_sentences에서 고른다
        text: 그 문장의 원문(또는 주장 부분)을 그대로 복사
        cited_source: 텍스트가 지목한 출처. 없으면 null
    """
    ctx = wrapper.context
    if not ctx.charge_record():
        return _reply(ctx, ok=False, error=_RECORD_CAP_ERROR)
    result = ctx.audit.record_claim(
        index=index,
        text=text,
        claim_type=claim_type,
        auditable=auditable,
        cited_source=cited_source,
        budget_per_claim=ctx.claim_budget(),
    )
    if result.get("ok"):
        ctx.emit_audit()
    return _record_reply(ctx, result)


@function_tool
async def update_verdict(
    wrapper: RunContextWrapper[AuditContext],
    claim_id: str,
    axis: Literal[1, 2, 3],
    outcome: Literal["pass", "fail", "skip", "undecidable"],
    evidence: str,
    evidence_ids: list[str],
    verdict: Literal["unsupported", "overstated", "supported", "no_source", "undecidable"]
    | None = None,
) -> dict:
    """축 판정 하나를 기록한다. 반환의 delta(하강폭)와 confidence_after가 화면에 실시간으로 뜬다.

    축1 확인 실패는 항상 no_source로 저장된다 — 검색이 못 찾은 것과 존재하지 않는 것은 다르다.
    같은 (클레임, 축)을 다시 부르면 이전 판정이 교체되고 신뢰도는 시작값부터 재계산된다.

    Args:
        axis: 1=존재 2=충실도 3=완전성
        evidence: 이 판정의 근거 한 문장 (판단 서술 — 출처 표기가 아니다)
        evidence_ids: 인용하는 증거 원장 id ("E1"…). pass는 최소 1개 필수, fail·undecidable·skip은 빈 배열 허용
        verdict: null이면 호스트가 규칙으로 파생
    """
    ctx = wrapper.context
    if not ctx.charge_record():
        return _reply(ctx, ok=False, error=_RECORD_CAP_ERROR)
    result = ctx.audit.update_verdict(
        claim_id=claim_id,
        axis=axis,
        outcome=outcome,
        evidence=evidence,
        evidence_ids=evidence_ids,
        verdict=verdict,
        tool_budget_left=max(0, ctx.max_tool_calls - ctx.tool_calls_used),
    )
    if result.get("ok"):
        ctx.axis_reached = max(ctx.axis_reached, int(axis))
        ctx.emit_audit()
    return _record_reply(ctx, result)


@function_tool
async def record_omission(
    wrapper: RunContextWrapper[AuditContext],
    claim_id: str,
    evidence_id: str,
    summary: str,
) -> dict:
    """축3 산출물 — 이 글이 언급하지 않은 반대·한정 문헌 하나를 기록한다.

    제목·URL·날짜·인용 수는 호스트가 원장에서 채운다. 너는 어느 자료가 무엇을
    반박하는지만 말한다.

    Args:
        evidence_id: 그 반대·한정 문헌의 evidence_id — 검색·페치 결과에 실려 온 id만 유효
        summary: 이 문헌이 무엇을 반박하거나 한정하는가 한 문장
    """
    ctx = wrapper.context
    if not ctx.charge_record():
        return _reply(ctx, ok=False, error=_RECORD_CAP_ERROR)
    result = ctx.audit.record_omission(
        claim_id=claim_id, evidence_id=evidence_id, summary=summary
    )
    if result.get("ok"):
        ctx.emit_audit()
    return _record_reply(ctx, result)


NETWORK_TOOLS = (search_web, search_scholar, fetch_source)
RECORD_TOOLS = (record_classification, record_claim, update_verdict, record_omission)
ALL_TOOLS = [*NETWORK_TOOLS, *RECORD_TOOLS]
FIRST_TOOL_NAME = "record_classification"
