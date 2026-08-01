"""감사 상태 — 문장 좌표계 · 클레임 원장 · 증거 원장 · 판정 계산.

이 모듈은 네트워크에 나가지 않는다. 순수 계산과 상태 보관만 한다.

세 가지가 여기 있다:

1. **문장 좌표계** — `split_sentences_with_kinds()`가 index → (문장, kind)를 만든다.
   하이라이트·커버리지·클레임 앵커가 전부 이 index 위에 선다. 호스트가 소유한다.
2. **증거 원장** — 검색·페치가 실제로 받아온 결과만 `register_evidence()`로 들어온다.
   판정과 누락 증거는 원장 id로만 출처를 가리킬 수 있다.
3. **판정 계산** — 신뢰도 갱신·verdict 파생·예산 계산은 전부 순수 함수다.
"""

from __future__ import annotations

import re
import time
import unicodedata
from math import ceil
from typing import Any, Callable, Iterable, Sequence, TypedDict
from urllib.parse import urlsplit, urlunsplit

# ── 호스트 소유 상수 ──────────────────────────────────────────────────────────
# 클레임 상한의 진실은 여기 한 곳이다. 프롬프트는 {{MAX_CLAIMS}} 자리표시자로
# 주입받으므로 숫자를 두 곳에 적지 않는다.
DEFAULT_MAX_CLAIMS = 12

# 기록 툴 소프트 상한 — 정상 런의 기록 호출은 클레임당 3축 + 분류 + 누락 몇 건이라
# 상한의 1/3에도 닿지 않는다. 폭주(같은 판정 무한 재호출)만 막는 백스톱이다.
MAX_RECORD_CALLS = 150

# 살아남은 클레임 중 축3(완전성)을 실제로 수행해야 하는 최소 비율.
# "최소 1회"였을 때 12건 중 1건만 하고도 완주로 기록되는 런이 나왔다 — 축3은 이 제품의
# 시그니처 산출물이라, 통째로 생략된 런을 완주라고 부르면 안 한 것을 한 것처럼 보이게 된다.
AXIS3_MIN_FRACTION = 0.5

# 문장 종류 — `sentences`와 같은 길이의 병렬 배열 `sentence_kinds`의 어휘.
SENTENCE_KINDS = (
    "prose",
    "list_item",
    "table_row",
    "quote",
    "code",
    "heading",
    "table_header",
    "code_fence",
    "divider",
)

# 구조 요소: 사실 주장이 아니라 이정표다. 클레임 앵커 불가 + 커버리지 분모 제외.
STRUCTURAL_KINDS = frozenset({"heading", "table_header", "code_fence", "divider"})

CLAIM_TYPES = ("statistical", "causal", "attribution", "definitional", "normative")
OUTCOMES = ("pass", "fail", "skip", "undecidable")
AXES = (1, 2, 3)
VERDICTS = (
    "pending",
    "unsupported",
    "overstated",
    "supported",
    "no_source",
    "undecidable",
)
# 모델이 제안할 수 있는 값 — "pending"은 호스트 초기값이라 제안 대상이 아니다.
SUGGESTABLE_VERDICTS = tuple(v for v in VERDICTS if v != "pending")

EVIDENCE_TOOLS = ("search_web", "search_scholar", "fetch_source")

SNIPPET_MAX_CHARS = 500
HOST_SENTENCES_MAX = 80
HOST_SENTENCE_CHARS = 160
CANDIDATE_MAX = 8
CATALOG_MAX = 20

# 화면의 %가 무엇인지 — 반환값에 그대로 실어 모델·시청자가 오독하지 않게 한다.
SCORE_MEANS = "확보한 지지 근거의 양(진실 확률 아님)"


# ── 자료구조 ─────────────────────────────────────────────────────────────────
class AxisResult(TypedDict):
    axis: int
    outcome: str
    evidence: str
    evidence_ids: list[str]
    source_urls: list[str]
    delta: float
    raw_delta: float
    suggested_verdict: str | None


class Claim(TypedDict):
    id: str
    index: int
    text: str
    claim_type: str
    auditable: bool
    cited_source: str | None
    base_confidence: float   # 호스트 상수 BASE_CONFIDENCE — 모델은 시작값을 정하지 않는다
    confidence: float
    verdict: str
    axis_results: list[AxisResult]


class Omission(TypedDict):
    claim_id: str
    evidence_id: str
    title: str
    url: str
    date: str | None
    citation_count: int | None
    summary: str


class EvidenceRecord(TypedDict):
    id: str
    tool: str
    query: str
    url: str
    title: str
    snippet: str
    retrieved_at: float
    extra: dict


# ── 문장 좌표계 ──────────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_BOLD_ONLY_RE = re.compile(r"^\s*\*\*(?P<inner>.+?)\*\*\s*[:：]?\s*$")
_DIVIDER_RE = re.compile(r"^\s{0,3}([-*_=])(?:\s*\1){2,}\s*$")
_TABLE_LINE_RE = re.compile(r"^\s{0,3}\|")
_TABLE_SEP_RE = re.compile(r"^\s{0,3}\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")
_QUOTE_RE = re.compile(r"^\s{0,3}>+\s?")
_BULLET_RE = re.compile(
    r"^\s*(?:[-*+•‣·]|\d{1,2}[.)]|\(\d{1,2}\)|[a-zA-Z][.)])\s+"
)
_TERMINAL_PUNCT = ".!?。！？…"
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])[\"'”’」』)\]]*\s+")


def _split_line_into_sentences(line: str) -> list[str]:
    """한 줄을 종결부호 뒤에서 문장으로 자른다. 소수점("7.2년")은 뒤에 공백이 없어 안 잘린다."""
    parts = _SENT_SPLIT_RE.split(line)
    return [p.strip() for p in parts if p.strip()]


def split_sentences_with_kinds(text: str) -> tuple[list[str], list[str]]:
    """문장 좌표계를 만든다 — (sentences, sentence_kinds) 병렬 배열.

    줄 단위로 마크다운 구조를 먼저 판정하고, 내용 줄만 종결부호에서 문장으로 쪼갠다.
    **구조 줄(제목·표 헤더·코드 펜스·구분선)은 쪼개지 않는다** — `## 1. 제목`을 온점에서
    자르면 번호만 남은 유령 줄이 좌표계에 생긴다.
    """
    sentences: list[str] = []
    kinds: list[str] = []
    in_fence = False
    prev_line_span: tuple[int, int] | None = None  # 직전 줄이 차지한 index 구간
    prev_line_kind: str | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            prev_line_span, prev_line_kind = None, None
            continue

        payload = line
        if in_fence:
            if _FENCE_RE.match(line):
                in_fence = False
                kind = "code_fence"
            else:
                kind = "code"
        elif _FENCE_RE.match(line):
            in_fence = True
            kind = "code_fence"
        elif _TABLE_LINE_RE.match(line):
            if _TABLE_SEP_RE.match(line):
                kind = "table_header"
                # 정렬 구분행 바로 위의 행은 헤더 행이다 — 소급해 다시 이름 붙인다.
                if prev_line_kind == "table_row" and prev_line_span:
                    for i in range(*prev_line_span):
                        kinds[i] = "table_header"
            else:
                kind = "table_row"
        elif _DIVIDER_RE.match(line):
            kind = "divider"
        elif _HEADING_RE.match(line):
            kind = "heading"
        elif _is_bold_heading(line):
            kind = "heading"
        elif _QUOTE_RE.match(line):
            kind = "quote"
            payload = _QUOTE_RE.sub("", line, count=1).strip()
        elif _BULLET_RE.match(line):
            kind = "list_item"
            payload = _BULLET_RE.sub("", line, count=1).strip()
        else:
            kind = "prose"

        start = len(sentences)
        if kind in STRUCTURAL_KINDS:
            sentences.append(line)
            kinds.append(kind)
        else:
            pieces = _split_line_into_sentences(payload) or ([payload] if payload else [])
            for piece in pieces:
                sentences.append(piece)
                kinds.append(kind)
        prev_line_span = (start, len(sentences))
        prev_line_kind = kind

    return sentences, kinds


def _is_bold_heading(line: str) -> bool:
    """굵은 글씨 단독 줄 — 종결부호로 끝나면 강조 산문이지 제목이 아니다."""
    m = _BOLD_ONLY_RE.match(line)
    if not m:
        return False
    inner = m.group("inner").strip()
    return bool(inner) and inner[-1] not in _TERMINAL_PUNCT


def split_sentences(text: str) -> list[str]:
    """문장 배열만 필요할 때. 좌표계 정본은 `split_sentences_with_kinds`다."""
    return split_sentences_with_kinds(text)[0]


def normalize_for_match(s: str) -> str:
    """공백·문장부호·기호·제어문자를 지우고 NFKC + casefold — 좌표 검증용 비교 키."""
    out = []
    for ch in unicodedata.normalize("NFKC", s or ""):
        if unicodedata.category(ch)[0] in ("Z", "P", "S", "C"):
            continue
        out.append(ch)
    return "".join(out).casefold()


def normalize_url(url: str) -> str:
    """같은 페이지를 같은 증거로 접기 위한 키 — 쿼리스트링은 의미가 있어 유지한다."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip()
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, host, path, parts.query, ""))


# ── 판정 계산 (순수 함수) ────────────────────────────────────────────────────
# 신뢰도 변화표. 이 숫자는 "주장이 참일 확률"이 아니라 **감사가 확보한 지지 근거의 양**이다.
# 0.0인 세 칸이 그 의미를 지킨다:
#   (1,"undecidable")·(2,"undecidable") — 페이월·403·타임아웃은 우리가 못 읽은 것이지
#     주장이 틀렸다는 증거가 아니다. 도구 접근 실패로 주장을 깎지 않는다.
#   (3,"pass") — 반증을 못 찾은 것은 지지 근거가 아니다. 없는 것을 점수로 주지 않는다.
_DELTA_TABLE: dict[tuple[int, str], float] = {
    (1, "pass"): +0.10,
    (1, "fail"): -0.40,
    (1, "undecidable"): 0.0,
    (1, "skip"): 0.0,
    (2, "pass"): +0.20,
    (2, "fail"): -0.45,
    (2, "undecidable"): 0.0,
    (2, "skip"): 0.0,
    (3, "pass"): 0.0,
    (3, "fail"): -0.15,
    (3, "undecidable"): 0.0,
    (3, "skip"): 0.0,
}

# 모델의 verdict 제안이 받아들여지는 자리. 여기 없는 제안은 무시되고 warning이 실린다.
# (1,"fail")은 의도적으로 비어 있다 — 축1 확인 실패는 항상 no_source다.
_ALLOWED_SUGGESTIONS: dict[tuple[int, str], frozenset[str]] = {
    (1, "undecidable"): frozenset({"undecidable"}),
    (2, "pass"): frozenset({"supported"}),
    (2, "fail"): frozenset({"unsupported", "overstated"}),
    (2, "undecidable"): frozenset({"undecidable"}),
    (3, "fail"): frozenset({"overstated", "unsupported"}),
    (3, "undecidable"): frozenset({"undecidable"}),
}


# 모든 클레임이 여기서 출발한다. 시작값을 모델이 정하면 화면의 %가 "확보한 근거의 양"이
# 아니라 "모델의 첫인상 + 근거"가 된다 — 증거를 더 적게 확보한 주장이 첫인상 덕에 더 높게
# 뜨는 일이 실제로 일어났다. 고정하면 표시값이 증거만의 함수가 되고, 지지 판정의 상한은
# 0.5 + 축1 0.10 + 축2 0.20 = 0.80이 되어 1.00(확실함)이 구조적으로 나올 수 없다.
BASE_CONFIDENCE = 0.5


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def axis_delta(axis: int, outcome: str) -> float:
    """클램프 전 표 값. 교체(F8) 시 리플레이의 재료다."""
    return _DELTA_TABLE.get((axis, outcome), 0.0)


def decide_verdict(axis: int, outcome: str, suggested: str | None, current: str) -> str:
    """축 결과 하나를 현재 verdict에 접는다.

    - **축1 `fail`은 무조건 `no_source`다.** 검색이 못 찾은 것과 존재하지 않는 것은 다르며,
      확인 실패를 `unsupported`로 승격하는 경로는 이 코드에 없다.
    - `skip`은 아직 아무것도 확인하지 않았다는 뜻이라 확정된 판정을 되돌리지 않는다.
    - 축3에서 반대 증거를 찾으면 지지되던 주장은 `overstated`로 내려간다 —
      이미 `unsupported`인 주장을 `overstated`로 **올리지는** 않는다.
    """
    if axis == 1 and outcome == "fail":
        return "no_source"

    allowed = _ALLOWED_SUGGESTIONS.get((axis, outcome), frozenset())
    if suggested and suggested in allowed:
        return suggested

    if outcome == "skip":
        # 축2를 건너뛴 채 끝나면 "확인하지 못했다"가 정직한 라벨이다.
        return "undecidable" if (current == "pending" and axis >= 2) else current
    if outcome == "undecidable":
        return "undecidable" if current in ("pending", "undecidable") else current
    if outcome == "pass":
        if axis == 2:
            return "supported"
        return current  # 축1 존재 확인·축3 반증 미발견은 그 자체로 판정이 아니다
    # outcome == "fail"
    if axis == 2:
        return "unsupported"
    if axis == 3:
        if current == "supported":
            return "overstated"
        return "undecidable" if current == "pending" else current
    return current


def derive_claim_verdict(axis_results: Sequence[AxisResult]) -> str:
    """축 결과 **집합**에서 verdict를 파생한다 — 호출 순서에 좌우되지 않는다.

    늦게 도착한 축1 `pass`가 이미 확정된 축2 `supported`를 뒤집지 못하게 하려면
    호출 순서가 아니라 축 오름차순으로 접어야 한다. 단 축1 `fail`(A-01)이 언제나 이긴다.
    """
    for r in axis_results:
        if r["axis"] == 1 and r["outcome"] == "fail":
            return "no_source"
    verdict = "pending"
    for r in sorted(axis_results, key=lambda x: x["axis"]):
        verdict = decide_verdict(r["axis"], r["outcome"], r.get("suggested_verdict"), verdict)
    return verdict


def replay_confidence(base: float, axis_results: Sequence[AxisResult]) -> float:
    """시작값부터 기록 순서대로 다시 적용해 신뢰도와 각 축의 실변화량을 재계산한다.

    단순 뺄셈으로 이전 델타를 되돌리면 0/1 경계에서 잘렸던 값이 복원되지 않는다.
    전체 리플레이만이 클램프 상호작용까지 정확하다. `AxisResult["delta"]`를 제자리 갱신한다.
    """
    conf = clamp01(base)
    for r in axis_results:
        stepped = clamp01(conf + r.get("raw_delta", 0.0))
        r["delta"] = round(stepped - conf, 6)
        conf = stepped
    return round(conf, 6)


def plan_claim_budget(remaining_tool_calls: int, remaining_claims: int) -> int:
    """클레임 하나에 쓸 수 있는 네트워크 호출 수. 예산이 0이면 0이다(넘겨짚지 않는다)."""
    if remaining_tool_calls <= 0 or remaining_claims <= 0:
        return 0
    return max(1, remaining_tool_calls // remaining_claims)


# ── Audit ────────────────────────────────────────────────────────────────────
def _fail(error: str, **data: Any) -> dict:
    return {"ok": False, "error": error, "data": data}


class Audit:
    """한 번의 감사 런이 만들어내는 모든 상태."""

    def __init__(
        self,
        input_text: str,
        *,
        max_claims: int = DEFAULT_MAX_CLAIMS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.input_text = input_text
        self.sentences, self.sentence_kinds = split_sentences_with_kinds(input_text)
        self.max_claims = max_claims
        self.claims: list[Claim] = []
        self.omissions: list[Omission] = []
        self.evidence: list[EvidenceRecord] = []
        self.status = "running"
        self.classification: dict | None = None
        self.source_mode = "unknown"
        self._clock = clock or time.monotonic
        self._started_at = self._clock()
        self._evidence_by_url: dict[str, str] = {}
        self._evidence_by_id: dict[str, EvidenceRecord] = {}

    # ── 좌표계 파생 ─────────────────────────────────────────────────────
    def claimable_indices(self) -> set[int]:
        """구조 요소를 뺀 감사 가능 문장 index."""
        return {i for i, k in enumerate(self.sentence_kinds) if k not in STRUCTURAL_KINDS}

    def host_sentences(self, *, limit: int = HOST_SENTENCES_MAX) -> list[dict]:
        rows = []
        for i, (s, k) in enumerate(zip(self.sentences, self.sentence_kinds)):
            if i >= limit:
                break
            rows.append({"index": i, "text": s[:HOST_SENTENCE_CHARS], "kind": k})
        return rows

    def find_index_candidates(self, text: str) -> list[dict]:
        """어긋난 좌표를 고칠 후보 — 감사 가능 문장에서만 고른다."""
        needle = normalize_for_match(text)
        claimable = self.claimable_indices()
        forward, reverse = [], []
        if needle:
            for i in sorted(claimable):
                hay = normalize_for_match(self.sentences[i])
                if not hay:
                    continue
                if needle in hay:
                    forward.append(i)
                elif hay in needle:
                    reverse.append(i)
        picked = (forward or reverse)[:CANDIDATE_MAX]
        return [
            {
                "index": i,
                "text": self.sentences[i][:HOST_SENTENCE_CHARS],
                "kind": self.sentence_kinds[i],
            }
            for i in picked
        ]

    # ── 증거 원장 (호스트 전용 — 모델 툴 스키마에 없다) ──────────────────
    def register_evidence(
        self,
        *,
        tool: str,
        query: str,
        url: str,
        title: str = "",
        snippet: str = "",
        extra: dict | None = None,
    ) -> EvidenceRecord:
        """실제로 받아온 결과 1건을 원장에 넣고 id를 발급한다. 같은 URL은 같은 id로 접힌다."""
        key = normalize_url(url)
        existing_id = self._evidence_by_url.get(key)
        if existing_id:
            rec = self._evidence_by_id[existing_id]
            # 먼저 받은 값이 정본이다 — 비어 있던 자리만 채운다.
            if not rec["title"] and title:
                rec["title"] = title
            if not rec["snippet"] and snippet:
                rec["snippet"] = (snippet or "")[:SNIPPET_MAX_CHARS]
            for k, v in (extra or {}).items():
                if v is not None and k not in rec["extra"]:
                    rec["extra"][k] = v
            return rec
        rec: EvidenceRecord = {
            "id": f"E{len(self.evidence) + 1}",
            "tool": tool,
            "query": query,
            "url": url,
            "title": title or "",
            "snippet": (snippet or "")[:SNIPPET_MAX_CHARS],
            "retrieved_at": round(self._clock() - self._started_at, 3),
            "extra": {k: v for k, v in (extra or {}).items() if v is not None},
        }
        self.evidence.append(rec)
        self._evidence_by_url[key] = rec["id"]
        self._evidence_by_id[rec["id"]] = rec
        return rec

    def evidence_catalog(self, *, limit: int = CATALOG_MAX) -> list[dict]:
        return [
            {"id": r["id"], "url": r["url"], "title": r["title"]}
            for r in self.evidence[:limit]
        ]

    def _resolve_evidence(self, ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """(원장에 있는 id 순서 유지·중복 제거, 모르는 id)"""
        known, unknown = [], []
        for raw in ids or []:
            eid = (raw or "").strip()
            if not eid:
                continue
            if eid in self._evidence_by_id:
                if eid not in known:
                    known.append(eid)
            elif eid not in unknown:
                unknown.append(eid)
        return known, unknown

    def source_urls_for(self, ids: Sequence[str]) -> list[str]:
        return [self._evidence_by_id[i]["url"] for i in ids if i in self._evidence_by_id]

    # ── 기록 툴 ─────────────────────────────────────────────────────────
    def record_classification(
        self,
        *,
        input_kind: str,
        lang: str,
        auditable: bool,
        sentence_count: int,
        rationale: str,
    ) -> dict:
        self.classification = {
            "input_kind": input_kind,
            "lang": lang,
            "auditable": bool(auditable),
            "sentence_count_model": int(sentence_count),
            "sentence_count_host": len(self.sentences),
            "rationale": rationale,
        }
        if not auditable:
            self.status = "non_auditable"
        claimable = self.claimable_indices()
        warning = None
        if int(sentence_count) != len(self.sentences):
            warning = (
                f"네가 센 문장 수({sentence_count})와 호스트 분할({len(self.sentences)})이 다르다. "
                "호스트 좌표가 정본이니 host_sentences의 index를 써라."
            )
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": True,
                "classification": self.classification,
                "host_sentences": self.host_sentences(),
                "host_sentence_count": len(self.sentences),
                "auditable_sentence_count": len(claimable),
                "host_sentences_truncated": len(self.sentences) > HOST_SENTENCES_MAX,
                "max_claims": self.max_claims,
                "next_action": (
                    "감사할 클레임을 골라 record_claim으로 등록하라."
                    if auditable
                    else "감사 대상이 아니다. 이유를 한 문장으로 최종 보고하고 끝내라."
                ),
                "warning": warning,
            },
        }

    def record_claim(
        self,
        *,
        index: int,
        text: str,
        claim_type: str,
        auditable: bool,
        cited_source: str | None = None,
        budget_per_claim: int | None = None,
    ) -> dict:
        if claim_type not in CLAIM_TYPES:
            return _fail(
                f"claim_type '{claim_type}'은 허용되지 않는다.",
                allowed_claim_types=list(CLAIM_TYPES),
            )
        if len(self.claims) >= self.max_claims:
            return _fail(
                f"클레임 상한 {self.max_claims}개를 이미 채웠다. 새 클레임 대신 등록된 클레임의 판정을 마무리하라.",
                claims_recorded=len(self.claims),
                max_claims=self.max_claims,
            )
        if not self.sentences:
            return _fail(
                "입력에서 문장을 하나도 찾지 못했다. 재시도하지 말고 감사 불가로 마무리하라.",
                index_candidates=[],
            )
        if not isinstance(index, int) or index < 0 or index >= len(self.sentences):
            return _fail(
                f"index {index}가 문장 범위(0~{len(self.sentences) - 1}) 밖이다.",
                index_candidates=self.find_index_candidates(text),
                host_sentence_count=len(self.sentences),
            )
        kind = self.sentence_kinds[index]
        if kind in STRUCTURAL_KINDS:
            return _fail(
                f"index {index}는 구조 요소({kind})다 — 제목·표 헤더·구분선·코드 펜스에는 "
                "클레임을 앵커할 수 없다. 그 아래 산문·목록 문장에서 골라라.",
                index_candidates=self.find_index_candidates(text),
                sentence_kind=kind,
            )
        needle, hay = normalize_for_match(text), normalize_for_match(self.sentences[index])
        if not needle or needle not in hay:
            return _fail(
                f"text가 호스트 문장 {index}에 들어 있지 않다. 원문 문장을 그대로 복사하고 "
                "index_candidates의 좌표로 다시 호출하라.",
                index_candidates=self.find_index_candidates(text),
                sentence=self.sentences[index][:HOST_SENTENCE_CHARS],
            )

        normalized_args: dict[str, Any] = {}
        claim: Claim = {
            "id": f"C{len(self.claims) + 1}",
            "index": index,
            "text": text,
            "claim_type": claim_type,
            "auditable": bool(auditable),
            "cited_source": cited_source or None,
            # 시작값은 호스트 상수다 — 모델도 호출자도 다른 값을 넣을 수 없다.
            "base_confidence": BASE_CONFIDENCE,
            "confidence": BASE_CONFIDENCE,
            "verdict": "pending",
            "axis_results": [],
        }
        self.claims.append(claim)
        at_index = [c["id"] for c in self.claims if c["index"] == index]
        note = None
        if len(at_index) > 1:
            note = f"문장 {index}에 클레임이 {len(at_index)}개다 — 한 문장 다중 주장은 정상이다."
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": True,
                "claim_id": claim["id"],
                "index": index,
                "sentence": self.sentences[index],
                "sentence_kind": kind,
                "auditable": claim["auditable"],
                "claims_at_index": at_index,
                "claims_recorded": len(self.claims),
                "claims_remaining": max(0, self.max_claims - len(self.claims)),
                "budget_per_claim": budget_per_claim,
                "normalized_args": normalized_args,
                "expected_next_axis": 1 if claim["auditable"] else None,
                "next_action": (
                    "축1(존재)부터 감사하라. 확증·반증 검색을 같은 턴에 발사하라."
                    if claim["auditable"]
                    else "의견·권고로 등록됐다. 감사하지 말고 다음 클레임으로 가라."
                ),
                "note": note,
                "warning": None,
            },
        }

    def get_claim(self, claim_id: str) -> Claim | None:
        for c in self.claims:
            if c["id"] == claim_id:
                return c
        return None

    def _axis_state(self, claim: Claim) -> dict[int, AxisResult]:
        return {r["axis"]: r for r in claim["axis_results"]}

    def expected_next_axis(self, claim: Claim) -> int | None:
        state = self._axis_state(claim)
        if 1 in state and state[1]["outcome"] == "fail":
            return None
        for a in AXES:
            if a not in state:
                return a
        return None

    def update_verdict(
        self,
        *,
        claim_id: str,
        axis: int,
        outcome: str,
        evidence: str,
        evidence_ids: Sequence[str] | None = None,
        verdict: str | None = None,
        tool_budget_left: int | None = None,
    ) -> dict:
        claim = self.get_claim(claim_id)
        if claim is None:
            return _fail(
                f"모르는 claim_id '{claim_id}'다.",
                known_claim_ids=[c["id"] for c in self.claims],
            )
        if axis not in AXES:
            return _fail(
                f"axis {axis}는 기록할 수 없다. 축4(오염)는 미구현이다.",
                allowed_axes=list(AXES),
            )
        if outcome not in OUTCOMES:
            return _fail(f"outcome '{outcome}'은 허용되지 않는다.", allowed_outcomes=list(OUTCOMES))
        if verdict is not None and verdict not in SUGGESTABLE_VERDICTS:
            return _fail(
                f"verdict '{verdict}'는 허용되지 않는다. 제안하지 않으려면 null을 보내라.",
                allowed_verdicts=list(SUGGESTABLE_VERDICTS),
            )

        state = self._axis_state(claim)
        replacing = axis in state
        # 같은 축 재호출은 정정이다 — 순서·종결 검사를 건너뛰어 회복 경로를 막지 않는다.
        if not replacing:
            if 1 in state and state[1]["outcome"] == "fail" and axis in (2, 3):
                return _fail(
                    f"{claim_id}은 축1 확인 실패로 종결된 클레임이다 — 축{axis}는 기록할 수 없다. "
                    "다음 클레임으로 넘어가라. 축1 판정을 정정하려면 axis=1로 다시 호출하라.",
                    claim_terminal=True,
                    verdict=claim["verdict"],
                )
            missing = [a for a in AXES if a < axis and a not in state]
            if missing:
                return _fail(
                    f"축 순서가 어긋났다 — {claim_id}에 축{missing[0]}이 아직 없다. "
                    "수행하지 않을 축은 outcome=\"skip\"으로 명시하면 통과한다(기록 툴은 예산 0).",
                    expected_next_axis=missing[0],
                    recorded_axes=sorted(state),
                )

        known_ids, unknown_ids = self._resolve_evidence(evidence_ids or [])
        if unknown_ids:
            return _fail(
                f"원장에 없는 evidence_id: {', '.join(unknown_ids)}. "
                "available_evidence에서 골라 다시 호출하라."
                + ("" if self.evidence else " 아직 원장이 비어 있다 — 먼저 검색을 호출하라."),
                available_evidence=self.evidence_catalog(),
                evidence_total=len(self.evidence),
            )
        if outcome == "pass" and not known_ids:
            return _fail(
                "긍정 판정(pass)은 evidence_ids 최소 1개가 필수다 — 확인에 쓴 증거 없이 "
                "\"확인했다\"는 성립하지 않는다. 증거가 없으면 fail 또는 undecidable이다."
                + ("" if self.evidence else " 아직 원장이 비어 있다 — 먼저 검색을 호출하라."),
                available_evidence=self.evidence_catalog(),
                evidence_total=len(self.evidence),
            )

        confidence_before = claim["confidence"]
        raw = axis_delta(axis, outcome)
        result: AxisResult = {
            "axis": axis,
            "outcome": outcome,
            "evidence": evidence,
            "evidence_ids": known_ids,
            "source_urls": self.source_urls_for(known_ids),
            "delta": 0.0,
            "raw_delta": raw,
            "suggested_verdict": verdict,
        }
        previous_outcome = None
        if replacing:
            previous_outcome = state[axis]["outcome"]
            pos = claim["axis_results"].index(state[axis])
            claim["axis_results"][pos] = result  # 제자리 교체 — axis_chain 순서 보존
        else:
            claim["axis_results"].append(result)

        # 델타는 시작값부터 전체 리플레이로 재계산한다 — 같은 축을 다시 불러도 펌핑이 없다.
        claim["confidence"] = replay_confidence(claim["base_confidence"], claim["axis_results"])
        claim["verdict"] = derive_claim_verdict(claim["axis_results"])

        warning = None
        if verdict and verdict != claim["verdict"]:
            if axis == 1 and outcome == "fail":
                warning = (
                    f"축1 확인 실패는 항상 no_source로 저장한다 — 제안한 '{verdict}'는 무시했다. "
                    "뒷받침 안 됨 판정이 필요하면 축2에서 출처 대조로 내려라."
                )
            else:
                warning = f"제안한 verdict '{verdict}'는 이 축·결과 조합에서 쓰이지 않아 무시했다."
        if replacing:
            prefix = f"{claim_id}의 축{axis} 기존 판정('{previous_outcome}')을 교체했다."
            warning = f"{prefix} {warning}" if warning else prefix

        next_axis = self.expected_next_axis(claim)
        terminal = next_axis is None
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": True,
                "claim_id": claim_id,
                "axis": axis,
                "outcome": outcome,
                "replaced": replacing,
                "previous_outcome": previous_outcome,
                "evidence_ids": known_ids,
                "source_urls": result["source_urls"],
                "confidence_before": confidence_before,
                "confidence_after": claim["confidence"],
                "delta": round(claim["confidence"] - confidence_before, 6),
                "evidence_score_before": confidence_before,
                "evidence_score_after": claim["confidence"],
                "score_means": SCORE_MEANS,
                "verdict": claim["verdict"],
                "axis_chain": [r["axis"] for r in claim["axis_results"]],
                "expected_next_axis": next_axis,
                "claim_terminal": terminal,
                "next_action": (
                    f"{claim_id}의 다음은 축{next_axis}다."
                    if next_axis
                    else f"{claim_id}은 종결이다 — 다음 클레임으로 가라."
                ),
                "tool_budget_left": tool_budget_left,
                "warning": warning,
            },
        }

    def record_omission(self, *, claim_id: str, evidence_id: str, summary: str) -> dict:
        if self.get_claim(claim_id) is None:
            return _fail(
                f"모르는 claim_id '{claim_id}'다.",
                known_claim_ids=[c["id"] for c in self.claims],
            )
        known, unknown = self._resolve_evidence([evidence_id])
        if unknown or not known:
            return _fail(
                f"원장에 없는 evidence_id '{evidence_id}'다 — 누락 증거는 실재하는 자료여야 한다.",
                available_evidence=self.evidence_catalog(),
                evidence_total=len(self.evidence),
            )
        eid = known[0]
        for o in self.omissions:
            if o["claim_id"] == claim_id and o["evidence_id"] == eid:
                return _fail(
                    f"({claim_id}, {eid})는 이미 등록된 누락 증거다 — 같은 자료를 다시 세지 않는다.",
                    existing=o,
                    omission_count=len(self.omissions),
                )
        rec = self._evidence_by_id[eid]
        extra = rec.get("extra") or {}
        omission: Omission = {
            "claim_id": claim_id,
            "evidence_id": eid,
            "title": rec["title"],
            "url": rec["url"],
            "date": extra.get("date"),
            "citation_count": extra.get("citation_count"),
            "summary": summary,
        }
        self.omissions.append(omission)
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": True,
                "omission": omission,
                "omission_count": len(self.omissions),
                "warning": None,
            },
        }

    # ── 파생 수치 ───────────────────────────────────────────────────────
    def audited_claims(self) -> list[Claim]:
        return [c for c in self.claims if c["auditable"] and c["axis_results"]]

    def audited_claim_count(self) -> int:
        return len(self.audited_claims())

    def unsupported_rate(self) -> tuple[int, int]:
        audited = self.audited_claims()
        bad = sum(1 for c in audited if c["verdict"] in ("unsupported", "overstated"))
        return bad, len(audited)

    def no_source_count(self) -> int:
        return sum(1 for c in self.claims if c["verdict"] == "no_source")

    def coverage(self) -> tuple[int, int]:
        """(감사한 고유 문장 수, 감사 가능한 문장 수). 분모에서 구조 요소는 빠진다."""
        claimable = self.claimable_indices()
        touched = {c["index"] for c in self.audited_claims()} & claimable
        return len(touched), len(claimable)

    def claims_by_index(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for c in self.claims:
            out.setdefault(str(c["index"]), []).append(c["id"])
        return out

    def axis3_progress(self) -> tuple[int, int, int]:
        """(수행, 기대, 최소 요구) — 축1을 통과하고 살아남은 클레임이 축3의 대상이다."""
        targets = [c for c in self.claims if c["auditable"]]
        expected = [
            c
            for c in targets
            if any(r["axis"] == 1 and r["outcome"] in ("pass", "undecidable") for r in c["axis_results"])
            and not any(r["axis"] == 1 and r["outcome"] == "fail" for r in c["axis_results"])
        ]
        with_omission = {o["claim_id"] for o in self.omissions}
        done = sum(
            1
            for c in expected
            if c["id"] in with_omission
            or any(r["axis"] == 3 and r["outcome"] != "skip" for r in c["axis_results"])
        )
        required = ceil(len(expected) * AXIS3_MIN_FRACTION) if expected else 0
        return done, len(expected), max(1, required) if expected else 0

    def completion_report(self) -> dict:
        """완주 조건 판정 — `reason="complete"`의 유일한 근거.

        감사 대상(auditable=true) 클레임만 본다. 가치명제의 pending은 부분 감사가 아니다.
        """
        targets = [c for c in self.claims if c["auditable"]]
        pending = [c["id"] for c in targets if c["verdict"] == "pending"]
        axis3_done, axis3_expected, axis3_required = self.axis3_progress()
        missing: list[str] = []
        if self.status != "non_auditable":
            if not targets:
                missing.append("감사 대상 클레임이 하나도 등록되지 않았다")
            if pending:
                missing.append(f"미확정 클레임 {', '.join(pending)}")
            if axis3_expected and axis3_done < axis3_required:
                missing.append(
                    f"축3(완전성)을 {axis3_done}/{axis3_expected} 클레임에만 실행했다"
                    f"(최소 {axis3_required})"
                )
        return {
            "complete": not missing,
            "missing_actions": missing,
            "claims_total": len(self.claims),
            "auditable_claims": len(targets),
            "pending_claims": pending,
            "axis3_executed": axis3_done > 0,
            "axis3_done": axis3_done,
            "axis3_expected": axis3_expected,
            "axis3_required": axis3_required,
            "omission_count": len(self.omissions),
        }

    # ── 직렬화 ──────────────────────────────────────────────────────────
    def _cited_evidence_ids(self) -> set[str]:
        ids = {o["evidence_id"] for o in self.omissions}
        for c in self.claims:
            for r in c["axis_results"]:
                ids.update(r["evidence_ids"])
        return ids

    def to_dict(self, *, stream: bool = False) -> dict:
        """기본형은 정본 전체. `stream=True`는 이벤트용 슬림 스냅샷.

        매 기록마다 방출되는 스냅샷에서 원장 전체(검색 수십 건)와 입력 원문을 빼는 것이
        이벤트 크기를 줄이는 유일한 방법이다 — UI가 읽는 키는 그대로 둔다.
        """
        unsupported, audited = self.unsupported_rate()
        covered, coverable = self.coverage()
        evidence = self.evidence
        if stream:
            cited = self._cited_evidence_ids()
            evidence = [r for r in self.evidence if r["id"] in cited]
        d: dict[str, Any] = {
            "sentences": list(self.sentences),
            "sentence_kinds": list(self.sentence_kinds),
            "claims": [dict(c) for c in self.claims],
            "omissions": [dict(o) for o in self.omissions],
            "evidence": [dict(r) for r in evidence],
            "evidence_total": len(self.evidence),
            "evidence_cited": len(self._cited_evidence_ids()),
            "status": self.status,
            "classification": self.classification,
            "source_mode": self.source_mode,
            "coverage": [covered, coverable],
            "claimable_sentence_count": coverable,
            "unsupported_rate": [unsupported, audited],
            "no_source_count": self.no_source_count(),
            "audited_claim_count": self.audited_claim_count(),
            "claims_by_index": self.claims_by_index(),
            "omission_count": len(self.omissions),
        }
        if not stream:
            d["input_text"] = self.input_text
        return d
