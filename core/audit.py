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

# 감사 대상 클레임 중 반증 검색이 나가야 하는 최소 비율.
# stance 파라미터만으로는 채택이 들쭉날쭉했다(어떤 런은 확증 7 : 반증 3). 반증은 축3의
# 원재료라, 안 쏘고 끝난 런을 완주라고 부르면 시그니처 산출물이 없는 채로 감사 완료가 뜬다.
CHALLENGE_RATIO = 0.5

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
INPUT_KINDS = (
    "ai_answer",
    "research_report",
    "academic_paragraph",
    "news_or_blog",
    "opinion_or_creative",
    "unknown",
)
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

# 헤드라인 수치 "뒷받침 안 됨 N/M"의 분자. 호스트 지표와 모델 보고서가 같은 정의를 쓰게
# 하는 단일 출처다. `no_source`는 분자가 아니라 별도 수치다 — 검색이 못 찾은 것을
# 뒷받침 안 됨으로 집계하면 수치 자체가 과장이 된다.
UNSUPPORTED_VERDICTS = ("unsupported", "overstated")

EVIDENCE_TOOLS = ("search_web", "search_scholar", "fetch_source")

# `Audit.status`가 가질 수 있는 값. `error`는 **시작하지 못한 런**이다 — 시간이 모자라
# 일부만 한 `partial`과 섞으면 크래시가 부분 감사로 위장된다.
AUDIT_STATUSES = ("running", "complete", "partial", "non_auditable", "error")

# 검색의 방향. 축1이 "지지 증거가 있는가"를 묻는 순간 그 검색은 구조적으로 확증이 된다 —
# 방향을 툴에서 구분하지 못하면 반증 검색은 셀 수도 강제할 수도 없는 부탁으로 남는다.
STANCES = ("support", "challenge")

# 클레임 등록 전의 탐색에 쓰는 예약 귀속값. 이 검색은 클레임별 쌍 계산에서 빠진다 —
# 귀속을 요구하되 "고르기 전에 훑어본다"는 정상 경로를 막지 않기 위한 값이다.
EXPLORATORY_CLAIM_ID = "explore"

SNIPPET_MAX_CHARS = 500
HOST_SENTENCES_MAX = 80
HOST_SENTENCE_CHARS = 160
CANDIDATE_MAX = 8
# 정규화 기준 최소 앵커 길이 — 한두 글자짜리 조각은 아무 문장에나 들어맞는다.
# 한 문장에 주장이 둘일 때 쓰는 정상적인 조각("성인의 62%")은 통과해야 하므로 낮게 잡고,
# 숫자가 다른 수 안에 박히는 경우는 길이가 아니라 anchor_matches의 경계 검사가 막는다.
# 문장 전체를 옮겨 적은 경우는 길이와 무관하게 통과한다.
MIN_ANCHOR_CHARS = 3
CATALOG_MAX = 20

# 화면의 %가 무엇인지 — 반환값에 그대로 실어 모델·시청자가 오독하지 않게 한다.
# ★ 이 수는 축별 outcome이 정해진 폭만큼 움직이는 **단계 점수**다. 인용 증거가 1건이든
# 10건이든 같은 outcome이면 같은 폭으로 움직인다 — 그러니 "근거의 양"이라고 부르면 안 된다.
# 실제 근거의 양은 `evidence_count`·`fetched_source_count`로 따로 센다.
SCORE_MEANS = "호스트 규칙이 정한 단계 점수 — 축별 판정이 정해진 폭만큼 움직인다. 진실 확률도, 근거 개수도 아니다"


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
    stance: str              # 이 결과를 데려온 검색의 방향 (support | challenge)
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
# ISO 639-1 두 글자(+선택 지역 태그). 형식 검증은 호스트 몫이다 — strict 스키마는
# pattern·minLength 같은 키워드를 지원하지 않아 모델 스키마에 넣을 수 없다.
_LANG_RE = re.compile(r"^[A-Za-z]{2}(?:[-_][A-Za-z0-9]{2,8})?$")
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


def anchor_matches(needle: str, hay: str) -> bool:
    """정규화된 앵커가 문장 안에 **경계를 지켜** 들어 있는가.

    단순 부분열 검사는 `"100"`을 `"회원은 1004명이다"`에 붙인다 — 정규화가 문장부호를
    지우므로 숫자가 더 긴 수 안에 박혀도 통과한다. 좌표계는 이 제품이 서 있는 유일한
    축이라, 숫자 앵커가 다른 수의 일부인 경우는 매치로 세지 않는다.
    """
    if not needle or not hay:
        return False
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        left = hay[start - 1] if start > 0 else ""
        right = hay[end] if end < len(hay) else ""
        inside_number = (needle[0].isdigit() and left.isdigit()) or (
            needle[-1].isdigit() and right.isdigit()
        )
        if not inside_number:
            return True
        start = hay.find(needle, start + 1)
    return False


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
    # (3, "fail")은 비어 있다 — 축3은 판정 열을 만지지 못한다. 아래 decide_verdict 참조.
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
        # skip은 "안 했다"이지 "확인할 수 없었다"가 아니다. 미확정을 해소하지 않는다 —
        # 그러면 아무 일도 하지 않고 모든 축을 skip으로 선언한 런이 완주가 된다.
        # 축 순서를 지나가는 용도로만 쓰이고, 그 클레임은 pending으로 남는다.
        return current
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
        # ★ 축3은 판정을 바꾸지 않는다. 축3이 찾는 것은 "이 글이 언급하지 않은 반대·한정
        # 문헌"이고 그런 문헌은 **참이고 정확히 인용된 문장에도 거의 항상 존재한다**.
        # 그것을 지나친 단정으로 승격하면 시그니처 산출물이 판정 열을 오염시켜, 전부 참인
        # 글이 화면에서 대부분 문제 있는 글로 칠해진다. 맥락이 빠졌다는 것과 원문이 증거보다
        # 세다는 것은 다른 사건이다 — 앞의 것은 반박 패널이 말하고, 뒤의 것은 축2가 말한다.
        return current
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
        self.searches: list[dict] = []
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
        stance: str = "support",
        extra: dict | None = None,
    ) -> EvidenceRecord:
        """실제로 받아온 결과 1건을 원장에 넣고 id를 발급한다. 같은 URL은 같은 id로 접힌다.

        `stance`는 이 결과를 데려온 검색의 방향이다. 기본값이 `support`인 것은 의도다 —
        방향을 말하지 않은 옛 호출부가 조용히 반증 자료로 둔갑하면 안 된다.
        """
        stance = stance if stance in STANCES else "support"
        key = normalize_url(url)
        existing_id = self._evidence_by_url.get(key)
        if existing_id:
            rec = self._evidence_by_id[existing_id]
            # ★ stance만은 예외로 승격한다. 확증 검색이 먼저 데려온 문헌을 반증 검색이 다시
            # 데려왔다면 그것은 반증 검색이 정식으로 찾아낸 자료다 — 최초 등록 시점에
            # 고정하면 반박 카드가 "뒷받침 검색에서 나온 약한 증거"로 오라벨된다.
            if stance == "challenge":
                rec["stance"] = "challenge"
            # 나머지는 먼저 받은 값이 정본이다 — 비어 있던 자리만 채운다.
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
            "stance": stance,
            "retrieved_at": round(self._clock() - self._started_at, 3),
            "extra": {k: v for k, v in (extra or {}).items() if v is not None},
        }
        self.evidence.append(rec)
        self._evidence_by_url[key] = rec["id"]
        self._evidence_by_id[rec["id"]] = rec
        return rec

    # ── 검색 기록 (발사 시점) ───────────────────────────────────────────
    def check_search(self, claim_id: str, stance: str, query: str) -> dict | None:
        """발사 전 검사 — 통과하면 None, 막을 이유가 있으면 거부 dict.

        검색이 어느 클레임의 일인지 호스트가 모르면 "클레임마다 확증·반증 한 쌍"은
        집행할 수 없는 부탁으로 남는다. 그래서 귀속을 요구하되, 클레임 등록 **전**의
        탐색 경로는 막지 않는다 — `EXPLORATORY_CLAIM_ID`로 쏘고 쌍 계산에서 빠진다.
        """
        if not (query or "").strip():
            return _fail("검색 질의가 비어 있다.")
        if claim_id != EXPLORATORY_CLAIM_ID:
            claim = self.get_claim(claim_id)
            if claim is None:
                return _fail(
                    f"모르는 claim_id '{claim_id}'다. 먼저 record_claim으로 등록하고 그 id로 "
                    f"검색하라. 아직 클레임을 고르는 중이면 claim_id=\"{EXPLORATORY_CLAIM_ID}\"로 "
                    "쏠 수 있다(이 검색은 클레임 쌍 계산에서 빠진다).",
                    known_claim_ids=[c["id"] for c in self.claims],
                    exploratory_claim_id=EXPLORATORY_CLAIM_ID,
                )
            if not claim["auditable"]:
                return _fail(
                    f"{claim_id}은 의견·권고로 등록된 클레임이라 감사 대상이 아니다 — 검색하지 마라.",
                    known_claim_ids=[c["id"] for c in self.claims if c["auditable"]],
                )
        key = normalize_for_match(query)
        for entry in self.searches:
            if entry["claim_id"] != claim_id or normalize_for_match(entry["query"]) != key:
                continue
            if entry["stance"] != stance:
                return _fail(
                    f"{claim_id}에 이미 같은 질의를 {entry['stance']} 방향으로 쐈다 — 라벨만 바꾼 "
                    "같은 질의는 반증 검색이 아니다. 반박·한정 자료를 겨냥한 다른 질의를 써라 "
                    "(limitations · contrary · no effect · systematic review 류).",
                    existing_stance=entry["stance"],
                    existing_query=entry["query"],
                )
        return None

    def note_search(
        self, stance: str, query: str, *, claim_id: str = EXPLORATORY_CLAIM_ID, endpoint: str = "web"
    ) -> dict:
        """검색이 나가는 순간을 기록한다. 반환한 항목에 `mark_search_result`로 회수를 적는다.

        발사 기록은 관측용이다. 완주 게이트가 세는 것은 **회수**다 —
        같은 질의를 세 번 쏜 것도, 전부 실패한 검색도 감사를 한 것이 아니다.
        """
        entry = {
            "claim_id": claim_id,
            "endpoint": endpoint,
            "stance": stance if stance in STANCES else "support",
            "query": query,
            "dispatched_at": round(self._clock() - self._started_at, 3),
            "result_status": "pending",
            "ok": None,
            "result_count": 0,
        }
        self.searches.append(entry)
        return entry

    def mark_search_result(self, entry: dict | None, ok: bool, result_count: int) -> None:
        """그 검색이 무엇을 받아왔는지 기록한다. 결과 0건은 회수가 아니다."""
        if not isinstance(entry, dict):
            return
        entry["ok"] = bool(ok)
        entry["result_count"] = max(0, int(result_count or 0))
        entry["result_status"] = "ok" if ok and entry["result_count"] else ("empty" if ok else "failed")

    def search_ledger(self) -> list[dict]:
        """파이프라인을 사후 검증할 수 있게 하는 작은 원장 — 누가 어느 방향으로 무엇을 쐈는가."""
        return [
            {
                "claim_id": e["claim_id"],
                "endpoint": e["endpoint"],
                "stance": e["stance"],
                "query": e["query"],
                "dispatched_at": e["dispatched_at"],
                "result_status": e["result_status"],
            }
            for e in self.searches
        ]

    def paired_claims(self) -> list[str]:
        """확증·반증을 **서로 다른 질의로** 둘 다 쏜 감사 대상 클레임."""
        by_claim: dict[str, dict[str, set[str]]] = {}
        for e in self.searches:
            if e["claim_id"] == EXPLORATORY_CLAIM_ID:
                continue
            key = normalize_for_match(e["query"])
            if key:
                by_claim.setdefault(e["claim_id"], {s: set() for s in STANCES})[e["stance"]].add(key)
        paired = []
        for c in self.claims:
            if not c["auditable"]:
                continue
            rows = by_claim.get(c["id"])
            if rows and rows["support"] and rows["challenge"] and rows["support"] != rows["challenge"]:
                paired.append(c["id"])
        return paired

    def search_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in STANCES}
        for entry in self.searches:
            counts[entry["stance"]] = counts.get(entry["stance"], 0) + 1
        return counts

    def challenge_query_count(self) -> int:
        """발사된 반증 검색 수 — 관측치다. 게이트가 쓰는 수는 `effective_challenge_count()`다."""
        return self.search_counts()["challenge"]

    def effective_challenge_count(self) -> int:
        """게이트가 세는 반증 검색 수: **서로 다른 질의**로 **결과를 받아온** 것만."""
        seen = set()
        for entry in self.searches:
            if entry["stance"] != "challenge":
                continue
            if not entry.get("ok") or entry.get("result_count", 0) < 1:
                continue
            key = normalize_for_match(entry["query"])
            if key:
                seen.add(key)
        return len(seen)

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
        if input_kind not in INPUT_KINDS:
            return _fail(
                f"input_kind '{input_kind}'은 허용되지 않는다.", allowed_input_kinds=list(INPUT_KINDS)
            )
        if not _LANG_RE.match((lang or "").strip()):
            return _fail(
                f"lang '{lang}'은 ISO 639-1 형식이 아니다 — 'ko'·'en'처럼 두 글자로 적어라.",
                example=["ko", "en", "ja"],
            )
        if int(sentence_count) < 0:
            return _fail(f"sentence_count는 0 이상이어야 한다(받은 값 {sentence_count}).")
        if not (rationale or "").strip():
            return _fail("rationale이 비어 있다 — 이 분류의 근거를 한 문장으로 적어라.")

        decision = {"input_kind": input_kind, "lang": lang.strip(), "auditable": bool(auditable)}
        previous = self.classification
        if previous is not None:
            already = {k: previous[k] for k in decision}
            if already == decision:
                # 같은 분류를 다시 보낸 것은 무해하다 — 상태를 바꾸지 않고 그대로 확인해 준다.
                return self._classification_reply(
                    warning="이미 같은 분류가 기록돼 있다 — 상태는 바뀌지 않았다.", recorded=False
                )
            if self.claims:
                return _fail(
                    "클레임이 이미 등록된 뒤에는 분류를 바꿀 수 없다 — 등록된 클레임의 전제가 "
                    "무너진다. 개별 주장의 성격은 record_claim의 claim_type으로 말하라.",
                    recorded_classification=already,
                    claims_recorded=len(self.claims),
                )
            if already["auditable"] != decision["auditable"]:
                return _fail(
                    "감사 가능 여부를 뒤집을 수 없다 — 첫 결정이 화면의 첫 이벤트로 이미 나갔다.",
                    recorded_classification=already,
                )

        self.classification = {
            **decision,
            "sentence_count_model": int(sentence_count),
            "sentence_count_host": len(self.sentences),
            "rationale": rationale,
        }
        if not auditable:
            self.status = "non_auditable"
        warning = None
        if int(sentence_count) != len(self.sentences):
            warning = (
                f"네가 센 문장 수({sentence_count})와 호스트 분할({len(self.sentences)})이 다르다. "
                "호스트 좌표가 정본이니 host_sentences의 index를 써라."
            )
        if input_kind == "opinion_or_creative" and auditable:
            # 막지는 않는다 — 사설·칼럼에도 검증 가능한 통계가 섞인다. 다만 그 조합을
            # 골랐다는 사실은 알려준다.
            note = (
                "input_kind가 opinion_or_creative인데 감사 가능으로 분류했다 — 사실 주장이 "
                "실제로 있으면 그대로 진행하고, 가치명제뿐이면 auditable=false가 맞다."
            )
            warning = f"{warning} {note}" if warning else note
        return self._classification_reply(warning=warning)

    def _classification_reply(self, *, warning: str | None, recorded: bool = True) -> dict:
        claimable = self.claimable_indices()
        auditable = bool((self.classification or {}).get("auditable"))
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": recorded,
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
        if claim_type == "normative" and auditable:
            return _fail(
                "normative(가치·당위 주장)는 참·거짓을 물을 수 없으므로 auditable=false로 등록한다. "
                "참·거짓을 물을 수 있는 주장이면 claim_type을 사실 유형으로 고쳐라.",
                allowed_claim_types=[t for t in CLAIM_TYPES if t != "normative"],
            )
        duplicate = self._find_duplicate_claim(index, text, claim_type)
        if duplicate is not None:
            # 같은 문장·같은 텍스트·같은 유형은 같은 주장이다. 상한을 먹이지 않고 기존 id를
            # 돌려준다 — 지금까지 이것이 접혔던 것은 호스트 제약이 아니라 모델 행동이었다.
            return {
                "ok": True,
                "error": None,
                "data": {
                    "recorded": False,
                    "duplicate_of": duplicate["id"],
                    "claim_id": duplicate["id"],
                    "index": duplicate["index"],
                    "sentence": self.sentences[duplicate["index"]],
                    "sentence_kind": self.sentence_kinds[duplicate["index"]],
                    "auditable": duplicate["auditable"],
                    "claims_recorded": len(self.claims),
                    "claims_remaining": max(0, self.max_claims - len(self.claims)),
                    "expected_next_axis": self.expected_next_axis(duplicate),
                    "next_action": (
                        f"이미 {duplicate['id']}로 등록된 주장이다 — 상한은 소모되지 않았다. "
                        "새로 등록하지 말고 그 id로 판정을 이어가라."
                    ),
                    "warning": "같은 주장을 다시 등록하려 했다 — 클레임 예산은 서로 다른 주장에 써라.",
                },
            }
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
        if not needle or not anchor_matches(needle, hay):
            return _fail(
                f"text가 호스트 문장 {index}에 들어 있지 않다. 원문 문장을 그대로 복사하고 "
                "index_candidates의 좌표로 다시 호출하라. (숫자만 옮겨 적으면 다른 수의 "
                "일부에 붙을 수 있어 거부된다 — 문장을 통째로 복사하라.)",
                index_candidates=self.find_index_candidates(text),
                sentence=self.sentences[index][:HOST_SENTENCE_CHARS],
            )
        whole_sentence = needle == hay
        if not whole_sentence and len(needle) < MIN_ANCHOR_CHARS:
            return _fail(
                f"앵커가 너무 짧다({len(needle)}자) — 이렇게 짧은 조각은 엉뚱한 문장에도 들어맞는다. "
                "그 주장이 담긴 문장을 통째로 복사하라.",
                index_candidates=self.find_index_candidates(text),
                sentence=self.sentences[index][:HOST_SENTENCE_CHARS],
                min_anchor_chars=MIN_ANCHOR_CHARS,
            )
        also_matches = [
            i
            for i in sorted(self.claimable_indices())
            if i != index and anchor_matches(needle, normalize_for_match(self.sentences[i]))
        ]
        if also_matches and not whole_sentence:
            # 여러 문장에 들어맞는 조각은 어느 주장에 대한 것인지 호스트가 알 수 없다.
            return _fail(
                f"이 조각은 문장 {[index, *also_matches]} 여러 곳에 들어맞는다 — 어느 주장인지 "
                "가릴 수 없다. 그 주장이 담긴 문장을 통째로 복사하라.",
                index_candidates=self.find_index_candidates(text),
                ambiguous_indices=[index, *also_matches],
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
        warning = None
        if also_matches:
            # 같은 문장이 원문에 여러 번 나온다. 어느 복사본에 밑줄이 가도 글자는 같지만,
            # 반복 사실 자체는 모델이 알고 보고해야 한다.
            warning = (
                f"같은 문장이 {[index, *also_matches]}에 반복된다 — 이 클레임은 문장 {index}에 "
                "앵커됐다. 반복을 새 클레임으로 또 등록하지 말고 최종 보고에 반복 횟수를 적어라."
            )
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
                "warning": warning,
            },
        }

    def _find_duplicate_claim(self, index: int, text: str, claim_type: str) -> Claim | None:
        key = normalize_for_match(text)
        for c in self.claims:
            if (
                c["index"] == index
                and c["claim_type"] == claim_type
                and normalize_for_match(c["text"]) == key
            ):
                return c
        return None

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
                    f"축{missing[0]}을 먼저 판정하라. outcome=\"skip\"은 순서만 지나갈 뿐 "
                    "판정이 아니다 — 그 클레임은 미확정으로 남아 완주에 걸린다.",
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
        if outcome == "fail" and axis in (2, 3) and not known_ids:
            # 축2 fail은 "출처와 대조했다", 축3 fail은 "반대 자료를 찾았다"는 사건이다 —
            # 둘 다 본 것이 있어야 성립한다. 부재의 주장은 축1 fail 하나뿐이다.
            what = "출처와 대조한 근거" if axis == 2 else "찾아낸 반대·한정 자료"
            return _fail(
                f"축{axis} fail은 {what}의 evidence_ids가 최소 1개 필요하다. "
                "접근하지 못했으면 undecidable, 출처 자체를 못 찾았으면 축1 fail로 기록하라 — "
                "본 것 없이 내리는 부정 판정은 감사가 아니다."
                + ("" if self.evidence else " 아직 원장이 비어 있다 — 먼저 검색을 호출하라."),
                available_evidence=self.evidence_catalog(),
                evidence_total=len(self.evidence),
            )
        if not (evidence or "").strip():
            return _fail(
                "evidence(판정 근거 한 문장)가 비어 있다 — 이 문장은 시청자가 읽는 글이다. "
                "무엇을 보고 그렇게 판단했는지 한 문장으로 적어라."
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
            elif axis == 3:
                warning = (
                    f"축3은 판정을 바꾸지 않는다 — 제안한 '{verdict}'는 무시했다. 반대·한정 문헌은 "
                    "record_omission으로 반박 목록에 남는다. 원문이 증거보다 세다고 판단했다면 "
                    "그것은 축2의 일이다 — axis=2로 다시 호출해 정정하라."
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
                "stage_score_before": confidence_before,
                "stage_score_after": claim["confidence"],
                # 실제로 확보한 근거의 양은 점수가 아니라 이 두 수다.
                "evidence_count": len(self.cited_evidence_ids(claim)),
                "fetched_source_count": self.fetched_evidence_count(claim),
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
        claim = self.get_claim(claim_id)
        if claim is None:
            return _fail(
                f"모르는 claim_id '{claim_id}'다.",
                known_claim_ids=[c["id"] for c in self.claims],
            )
        if not claim["auditable"]:
            return _fail(
                f"{claim_id}은 의견·권고로 등록된 클레임이다 — 사실 판정 대상이 아니므로 "
                "반박 문헌도 붙지 않는다.",
                known_claim_ids=[c["id"] for c in self.claims if c["auditable"]],
            )
        if any(r["axis"] == 1 and r["outcome"] == "fail" for r in claim["axis_results"]):
            return _fail(
                f"{claim_id}은 축1 확인 실패로 종결된 클레임이다 — 출처를 확인하지 못한 주장에 "
                "'이 글이 언급하지 않은 반대 문헌'을 붙이는 것은 성립하지 않는다.",
                claim_terminal=True,
            )
        if not (summary or "").strip():
            return _fail(
                "summary가 비어 있다 — 이 문헌이 무엇을 반박하거나 한정하는지 한 문장으로 적어라. "
                "그 문장이 반박 패널에 그대로 표시된다."
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
        warning = None
        if rec["stance"] != "challenge":
            # 확증 검색에서 우연히 나온 반대 자료도 실재하는 자료다 — 막지 않는다.
            # 다만 반증 검색이 데려온 것과 같은 무게로 세지는 않는다.
            warning = (
                f"{eid}는 확증(support) 검색에서 나온 자료다 — 반박 자료로 쓸 수는 있지만 "
                "반증 검색이 찾아낸 것과 같지 않다. 이 클레임의 반증 검색을 따로 발사하라."
            )
        return {
            "ok": True,
            "error": None,
            "data": {
                "recorded": True,
                "omission": omission,
                "omission_count": len(self.omissions),
                "evidence_stance": rec["stance"],
                "stance_mismatch": rec["stance"] != "challenge",
                "warning": warning,
            },
        }

    # ── 파생 수치 ───────────────────────────────────────────────────────
    def audited_claims(self) -> list[Claim]:
        return [c for c in self.claims if c["auditable"] and c["axis_results"]]

    def audited_claim_count(self) -> int:
        return len(self.audited_claims())

    def unsupported_rate(self) -> tuple[int, int]:
        """(미지지 수, 감사한 클레임 수). 분자 정의는 `UNSUPPORTED_VERDICTS` 한 곳이고
        프롬프트의 최종 보고 헤드라인도 같은 정의를 쓴다 — 두 수치가 갈라지면 안 된다."""
        audited = self.audited_claims()
        bad = sum(1 for c in audited if c["verdict"] in UNSUPPORTED_VERDICTS)
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
        """(수행, 기대, 최소 요구) — 축1 확인 실패로 종결된 것만 빼고 전부 축3 대상이다.

        분모를 축1 **수행 이력**으로 잡으면 축1을 건너뛴 런에서 요구치가 0이 되어 게이트가
        사라진다 — 일을 한 런만 벌하는 게이트가 된다. 그래서 **등록 사실**로 잡는다.
        """
        targets = [c for c in self.claims if c["auditable"]]
        expected = [
            c
            for c in targets
            if not any(r["axis"] == 1 and r["outcome"] == "fail" for r in c["axis_results"])
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

    def challenge_progress(self) -> tuple[int, int]:
        """(게이트가 세는 반증 검색 수, 최소 요구치).

        세는 것은 **회수**다 — 서로 다른 질의로 결과를 받아온 반증 검색만. 요구치는
        **사후 정직성 라벨**이지 모델에게 주는 할당량이 아니다(봉투에 싣지 않는 이유).
        """
        targets = sum(1 for c in self.claims if c["auditable"])
        required = max(1, ceil(targets * CHALLENGE_RATIO)) if targets else 0
        return self.effective_challenge_count(), required

    def completion_report(self) -> dict:
        """완주 조건 판정 — `reason="complete"`의 유일한 근거.

        감사 대상(auditable=true) 클레임만 본다. 가치명제의 pending은 부분 감사가 아니다.
        """
        targets = [c for c in self.claims if c["auditable"]]
        pending = [c["id"] for c in targets if c["verdict"] == "pending"]
        axis3_done, axis3_expected, axis3_required = self.axis3_progress()
        challenge_queries, challenge_required = self.challenge_progress()
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
            if targets and axis3_expected and not self.omissions:
                # 시그니처 산출물이 0건인 런을 완주라고 부르면, 화면이 "반박까지 찾아봤지만
                # 없었다"고 단정하게 된다. 못 찾은 것과 안 찾은 것을 구분할 수 없다.
                missing.append("축3 누락 증거가 0건이다")
            if targets and challenge_queries < challenge_required:
                # 벌하는 것은 호출 거부가 아니라 반증의 부재다 — 모델이 우아하게 마무리한
                # 것과 감사가 완주된 것은 다른 사건이다.
                missing.append(
                    f"결과를 받아온 서로 다른 반증 검색이 {challenge_queries}회뿐이다"
                    f"(최소 {challenge_required}, 발사 {self.challenge_query_count()}회)"
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
            "challenge_queries": challenge_queries,
            "challenge_required": challenge_required,
            "challenge_dispatched": self.challenge_query_count(),
            "claims_with_support_challenge_pair": len(self.paired_claims()),
            "search_counts": self.search_counts(),
            "omission_count": len(self.omissions),
        }

    # ── 직렬화 ──────────────────────────────────────────────────────────
    def cited_evidence_ids(self, claim: Claim) -> set[str]:
        """이 클레임의 판정이 인용한 증거 id — 판정 점수와 달리 실제로 본 자료의 수다."""
        ids: set[str] = set()
        for r in claim["axis_results"]:
            ids.update(r["evidence_ids"])
        ids.update(o["evidence_id"] for o in self.omissions if o["claim_id"] == claim["id"])
        return ids

    def fetched_evidence_count(self, claim: Claim) -> int:
        """그중 본문을 실제로 가져와 대조한 자료의 수."""
        return sum(
            1
            for eid in self.cited_evidence_ids(claim)
            if self._evidence_by_id.get(eid, {}).get("tool") == "fetch_source"
        )

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
            "searches": self.search_ledger(),
            "omission_count": len(self.omissions),
        }
        if not stream:
            d["input_text"] = self.input_text
        return d
