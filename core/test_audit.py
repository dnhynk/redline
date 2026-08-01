"""core/audit.py 단위 테스트 — 좌표계 · 증거 원장 · 판정 계산."""

from __future__ import annotations

import pytest

from core.audit import (
    BASE_CONFIDENCE,
    DEFAULT_MAX_CLAIMS,
    EXPLORATORY_CLAIM_ID,
    SENTENCE_KINDS,
    STRUCTURAL_KINDS,
    Audit,
    axis_delta,
    clamp01,
    decide_verdict,
    derive_claim_verdict,
    normalize_for_match,
    normalize_url,
    plan_claim_budget,
    split_sentences,
    split_sentences_with_kinds,
)

CHATBOT = """## 노트북이 뜨거워지는 이유

노트북이 뜨거워지는 것은 대부분 정상입니다. CPU는 전력을 열로 방출합니다.

### 1. 발열의 원인

- 먼지가 쌓이면 냉각 성능이 40% 떨어집니다.
- 인텔 CPU는 보통 100도 부근에서 스로틀링됩니다.

이 과정에서 자연스럽게 고열이 발생합니다.

**고장이 아닙니다.**

| 항목 | 온도 |
|---|---|
| 아이들 | 45도 |

---
"""


# ── 문장 좌표계 ──────────────────────────────────────────────────────────────
def test_plain_prose_splits_on_terminal_punctuation():
    sentences, kinds = split_sentences_with_kinds(
        "커피는 각성 효과가 있다. 성인의 62%가 매일 마신다! 정말인가?"
    )
    assert sentences == ["커피는 각성 효과가 있다.", "성인의 62%가 매일 마신다!", "정말인가?"]
    assert kinds == ["prose"] * 3


def test_decimal_point_does_not_split_a_sentence():
    sentences, _ = split_sentences_with_kinds("수명이 정확히 7.2년 길다고 한다.")
    assert sentences == ["수명이 정확히 7.2년 길다고 한다."]


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "人工智能可以诊断疾病。研究表明准确率为95%。这是重大突破。",
            ["人工智能可以诊断疾病。", "研究表明准确率为95%。", "这是重大突破。"],
        ),
        (
            "これは事実です。研究によると80%減少しました。",
            ["これは事実です。", "研究によると80%減少しました。"],
        ),
        ("本当ですか？いいえ！そうです。", ["本当ですか？", "いいえ！", "そうです。"]),
    ],
)
def test_fullwidth_terminals_split_without_a_following_space(text, expected):
    """중국어·일본어는 종결부호 뒤에 공백을 두지 않는다.

    공백을 요구하면 한 단락이 문장 하나로 접히고, 좌표계 위에 선 하이라이트·커버리지·
    클레임 앵커가 전부 그 하나로 뭉갠다 — 어떤 언어가 올지 모르는 이상 막아야 한다.
    """
    assert split_sentences_with_kinds(text)[0] == expected


def test_fullwidth_decimal_point_is_not_a_terminal():
    """전각 종결부호는 소수점으로 쓰이지 않는다 — 반각 규칙을 그대로 물려받지 않는다."""
    assert split_sentences_with_kinds("价格是7.2元。这很便宜。")[0] == [
        "价格是7.2元。",
        "这很便宜。",
    ]


def test_closing_bracket_stays_with_the_sentence_it_closes():
    """구분자가 닫는 부호를 삼키면 좌표계에서 글자가 사라진다 — 화면은 이 배열을 원문으로 그린다."""
    assert split_sentences_with_kinds("「これは事実です。」次の文です。")[0] == [
        "「これは事実です。」",
        "次の文です。",
    ]
    assert split_sentences_with_kinds('He said "yes." Then left.')[0] == [
        'He said "yes."',
        "Then left.",
    ]


def test_markdown_kinds_are_classified():
    sentences, kinds = split_sentences_with_kinds(CHATBOT)
    pairs = dict(zip(sentences, kinds))
    assert pairs["## 노트북이 뜨거워지는 이유"] == "heading"
    assert pairs["### 1. 발열의 원인"] == "heading"
    assert pairs["먼지가 쌓이면 냉각 성능이 40% 떨어집니다."] == "list_item"
    assert pairs["이 과정에서 자연스럽게 고열이 발생합니다."] == "prose"
    assert pairs["| 아이들 | 45도 |"] == "table_row"
    assert pairs["|---|---|"] == "table_header"
    assert pairs["| 항목 | 온도 |"] == "table_header"
    assert pairs["---"] == "divider"
    assert set(kinds) <= set(SENTENCE_KINDS)


def test_heading_is_not_split_into_ghost_fragments():
    """`### 1. 발열의 원인`을 온점에서 자르면 번호만 남은 유령 줄이 좌표계에 생긴다."""
    sentences, kinds = split_sentences_with_kinds("### 1. 발열의 원인\n산문 문장이다.")
    assert sentences[0] == "### 1. 발열의 원인"
    assert kinds[0] == "heading"
    assert "### 1." not in sentences


def test_bold_line_is_heading_only_without_terminal_punctuation():
    _, kinds_a = split_sentences_with_kinds("**발열의 원인**")
    _, kinds_b = split_sentences_with_kinds("**고장이 아닙니다.**")
    assert kinds_a == ["heading"]
    assert kinds_b == ["prose"]


def test_list_and_quote_markers_are_stripped():
    sentences, kinds = split_sentences_with_kinds("- 첫 항목이다.\n> 인용문이다.\n1. 번호 항목이다.")
    assert sentences == ["첫 항목이다.", "인용문이다.", "번호 항목이다."]
    assert kinds == ["list_item", "quote", "list_item"]


def test_code_fence_and_body_kinds():
    sentences, kinds = split_sentences_with_kinds("```python\nx = 1  # 상수다.\n```")
    assert kinds == ["code_fence", "code", "code_fence"]
    assert sentences[1] == "x = 1  # 상수다."


def test_structural_kinds_are_excluded_from_claimable_indices():
    audit = Audit(CHATBOT)
    claimable = audit.claimable_indices()
    for i in claimable:
        assert audit.sentence_kinds[i] not in STRUCTURAL_KINDS
    assert len(claimable) < len(audit.sentences)
    # 목록 항목·표 본문 행은 구조가 아니다 — 챗봇 답변의 사실 주장이 거기 산다.
    kinds = {audit.sentence_kinds[i] for i in claimable}
    assert "list_item" in kinds and "table_row" in kinds


def test_split_sentences_is_the_kinds_free_view():
    assert split_sentences(CHATBOT) == split_sentences_with_kinds(CHATBOT)[0]


def test_normalize_for_match_absorbs_punctuation_and_case():
    assert normalize_for_match("A “B”, c!") == normalize_for_match("ab c")


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://Example.com/a/", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/a#frag", "https://example.com/a"),
    ],
)
def test_normalize_url_folds_variants(a, b):
    assert normalize_url(a).split("://", 1)[1] == normalize_url(b).split("://", 1)[1]


# ── 좌표 검증 ────────────────────────────────────────────────────────────────
def _audit_two_sentences() -> Audit:
    return Audit("커피는 각성 효과가 있다. 성인의 62%가 매일 마신다.")


def _challenge(audit: Audit, query: str, *, ok: bool = True, results: int = 1) -> dict:
    """반증 검색 한 발 — 발사하고 회수까지 기록한다."""
    entry = audit.note_search("challenge", query)
    audit.mark_search_result(entry, ok, results)
    return entry


def _claim(audit: Audit, index: int, text: str, **kw):
    args = dict(index=index, text=text, claim_type="statistical", auditable=True)
    args.update(kw)
    return audit.record_claim(**args)


def test_out_of_range_index_is_rejected_with_candidates():
    audit = _audit_two_sentences()
    out = _claim(audit, 99, "성인의 62%가 매일 마신다")
    assert out["ok"] is False
    assert "범위" in out["error"]
    assert [c["index"] for c in out["data"]["index_candidates"]] == [1]
    assert audit.claims == []


def test_mismatched_coordinate_is_rejected_and_correctable():
    audit = _audit_two_sentences()
    bad = _claim(audit, 0, "성인의 62%가 매일 마신다")
    assert bad["ok"] is False
    candidate = bad["data"]["index_candidates"][0]["index"]
    good = _claim(audit, candidate, "성인의 62%가 매일 마신다")
    assert good["ok"] is True
    assert good["data"]["claim_id"] == "C1"
    assert good["data"]["sentence_kind"] == "prose"


def test_structural_sentence_cannot_anchor_a_claim():
    audit = Audit(CHATBOT)
    heading_index = audit.sentence_kinds.index("heading")
    out = _claim(audit, heading_index, audit.sentences[heading_index])
    assert out["ok"] is False
    assert "구조 요소" in out["error"]
    assert all(
        audit.sentence_kinds[c["index"]] not in STRUCTURAL_KINDS
        for c in out["data"]["index_candidates"]
    )


def test_two_claims_on_one_sentence_are_allowed():
    audit = _audit_two_sentences()
    _claim(audit, 1, "성인의 62%")
    second = _claim(audit, 1, "매일 마신다")
    assert second["ok"] is True
    assert second["data"]["claims_at_index"] == ["C1", "C2"]
    assert audit.claims_by_index() == {"1": ["C1", "C2"]}


def test_invalid_claim_type_is_rejected_not_silently_defaulted():
    audit = _audit_two_sentences()
    out = _claim(audit, 0, "커피는 각성 효과가 있다", claim_type="vibes")
    assert out["ok"] is False and "claim_type" in out["error"]


def test_model_cannot_set_starting_confidence():
    """시작값이 모델 손에 있으면 화면의 %가 '근거의 양'이 아니라 '첫인상 + 근거'가 된다."""
    import inspect

    from core.model_tools import ALL_TOOLS

    schema = [t for t in ALL_TOOLS if t.name == "record_claim"][0].params_json_schema
    assert "prior" not in schema["properties"] and "prior" not in schema["required"]
    assert "confidence" not in schema["properties"]
    # 우회 경로도 막는다 — 호스트 시그니처에도 시작값 인자가 없다.
    params = inspect.signature(Audit.record_claim).parameters
    assert "prior" not in params and "confidence" not in params

    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다")
    _claim(audit, 1, "성인의 62%가 매일 마신다")
    assert [c["confidence"] for c in audit.claims] == [BASE_CONFIDENCE, BASE_CONFIDENCE]
    assert [c["base_confidence"] for c in audit.claims] == [BASE_CONFIDENCE] * 2


def test_supported_claim_never_displays_certainty():
    """지지 판정의 상한은 0.80이다 — 1.00이 뜨면 '참일 확률이 아니다'라는 화면 문구가 거짓이 된다."""
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
    out = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="출처가 주장을 지지한다", evidence_ids=["E1"]
    )
    assert out["data"]["verdict"] == "supported"
    assert audit.claims[0]["confidence"] == pytest.approx(0.80)
    assert audit.claims[0]["confidence"] < 1.0


def test_claim_cap_rejects_but_does_not_break_the_run():
    audit = Audit("가. 나. 다.", max_claims=2)
    assert _claim(audit, 0, "가")["ok"] is True
    assert _claim(audit, 1, "나")["ok"] is True
    out = _claim(audit, 2, "다")
    assert out["ok"] is False and out["data"]["max_claims"] == 2
    assert len(audit.claims) == 2


def test_empty_input_tells_the_model_not_to_retry():
    audit = Audit("")
    out = _claim(audit, 0, "무엇이든")
    assert out["ok"] is False and "재시도하지 말고" in out["error"]


# ── 증거 원장 ────────────────────────────────────────────────────────────────
def _with_evidence() -> Audit:
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다")
    audit.register_evidence(
        tool="search_web",
        query="커피 각성",
        url="https://who.int/caffeine",
        title="WHO 카페인 보고서",
        snippet="카페인은 각성을 유발한다",
        extra={"date": "2019-01-01", "citation_count": None},
    )
    return audit


def test_evidence_ids_are_host_issued_and_urls_fold():
    audit = _audit_two_sentences()
    first = audit.register_evidence(tool="search_web", query="q", url="https://a.test/x")
    again = audit.register_evidence(tool="search_scholar", query="다른 질의", url="https://A.test/x/")
    other = audit.register_evidence(tool="search_web", query="q", url="https://b.test/y")
    assert first["id"] == "E1" and again["id"] == "E1" and other["id"] == "E2"
    assert len(audit.evidence) == 2


def test_evidence_ledger_records_stance():
    """어느 방향 검색에서 나온 자료인지 원장이 기억해야 화면이 그것을 말할 수 있다."""
    audit = _audit_two_sentences()
    challenged = audit.register_evidence(
        tool="search_scholar",
        query="green tea cancer prevention limitations",
        url="https://a.test/meta",
        stance="challenge",
    )
    # 기본값은 support — 방향을 말하지 않은 호출이 조용히 반증 자료로 둔갑하면 안 된다.
    defaulted = audit.register_evidence(tool="search_web", query="q", url="https://b.test")
    assert challenged["stance"] == "challenge"
    assert defaulted["stance"] == "support"
    assert [r["stance"] for r in audit.to_dict()["evidence"]] == ["challenge", "support"]


def test_stance_travels_to_the_stream_snapshot():
    audit = _with_evidence()
    audit.register_evidence(
        tool="search_web", query="반박 질의", url="https://c.test", stance="challenge"
    )
    audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1", "E2"]
    )
    stream = audit.to_dict(stream=True)
    assert {r["id"]: r["stance"] for r in stream["evidence"]} == {"E1": "support", "E2": "challenge"}


def test_evidence_reuse_fills_empty_fields_only():
    audit = _audit_two_sentences()
    audit.register_evidence(tool="search_web", query="q", url="https://a.test", title="첫 제목")
    audit.register_evidence(tool="search_web", query="q", url="https://a.test", title="나중 제목")
    assert audit.evidence[0]["title"] == "첫 제목"


def test_unknown_evidence_id_is_rejected_with_catalog():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="확인", evidence_ids=["E9"]
    )
    assert out["ok"] is False
    assert out["data"]["available_evidence"][0]["id"] == "E1"
    assert audit.claims[0]["axis_results"] == []


def test_fabricated_url_has_no_place_in_the_schema():
    """update_verdict·record_omission은 URL을 받지 않는다 — 호스트가 원장에서 채운다."""
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="확인", evidence_ids=["E1"]
    )
    assert out["data"]["source_urls"] == ["https://who.int/caffeine"]
    assert audit.claims[0]["axis_results"][0]["source_urls"] == ["https://who.int/caffeine"]


def test_pass_without_citation_is_rejected():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="확인했다", evidence_ids=[]
    )
    assert out["ok"] is False and "pass" in out["error"]


@pytest.mark.parametrize("outcome", ["fail", "undecidable", "skip"])
def test_absence_claims_may_cite_nothing(outcome):
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome=outcome, evidence="못 찾았다", evidence_ids=[]
    )
    assert out["ok"] is True


def test_empty_ledger_tells_the_model_to_search_first():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다")
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="확인", evidence_ids=[]
    )
    assert "먼저 검색" in out["error"]


def test_unknown_claim_id_is_rejected():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C99", axis=1, outcome="fail", evidence="x", evidence_ids=[]
    )
    assert out["ok"] is False and out["data"]["known_claim_ids"] == ["C1"]


# ── 누락 증거 (축3) ──────────────────────────────────────────────────────────
def test_omission_bibliography_comes_from_the_ledger():
    audit = _with_evidence()
    out = audit.record_omission(claim_id="C1", evidence_id="E1", summary="반대 결과를 보고한다")
    assert out["ok"] is True
    om = out["data"]["omission"]
    assert om["url"] == "https://who.int/caffeine"
    assert om["title"] == "WHO 카페인 보고서"
    assert om["date"] == "2019-01-01"


def test_duplicate_omission_cannot_inflate_the_signature_number():
    audit = _with_evidence()
    audit.record_omission(claim_id="C1", evidence_id="E1", summary="첫 등록")
    dup = audit.record_omission(claim_id="C1", evidence_id="E1", summary="같은 자료 재등록")
    assert dup["ok"] is False and dup["data"]["existing"]["summary"] == "첫 등록"
    assert len(audit.omissions) == 1


def test_omission_rejects_unknown_ids():
    audit = _with_evidence()
    assert audit.record_omission(claim_id="C1", evidence_id="E9", summary="x")["ok"] is False
    assert audit.record_omission(claim_id="C9", evidence_id="E1", summary="x")["ok"] is False


# ── 판정 계산 ────────────────────────────────────────────────────────────────
def test_axis1_fail_is_always_no_source():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1",
        axis=1,
        outcome="fail",
        evidence="지목된 보고서를 찾지 못함",
        evidence_ids=[],
        verdict="unsupported",
    )
    assert out["data"]["verdict"] == "no_source"
    assert "no_source" in out["data"]["warning"]
    assert decide_verdict(1, "fail", "supported", "supported") == "no_source"


def test_axis2_fail_can_be_overstated_when_suggested():
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
    out = audit.update_verdict(
        claim_id="C1",
        axis=2,
        outcome="fail",
        evidence="원문은 상관관계만 말한다",
        evidence_ids=["E1"],
        verdict="overstated",
    )
    assert out["data"]["verdict"] == "overstated"
    assert out["data"]["delta"] == pytest.approx(-0.45)


def test_same_axis_recall_replaces_and_does_not_pump():
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    first = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="e", evidence_ids=["E1"]
    )
    second = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="e", evidence_ids=["E1"]
    )
    third = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="e", evidence_ids=["E1"]
    )
    assert first["data"]["confidence_after"] == third["data"]["confidence_after"]
    assert second["data"]["delta"] == 0.0 and third["data"]["delta"] == 0.0
    assert third["data"]["replaced"] is True
    assert third["data"]["previous_outcome"] == "pass"
    assert len(audit.claims[0]["axis_results"]) == 2


def test_axis_recall_replay_handles_clamp():
    """클램프된 델타는 단순 뺄셈으로 복원되지 않는다 — 시작값부터 전체 리플레이해야 맞다."""
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="undecidable", evidence="접근 불가", evidence_ids=[])
    audit.update_verdict(claim_id="C1", axis=2, outcome="fail", evidence="불일치", evidence_ids=["E1"])
    audit.update_verdict(claim_id="C1", axis=3, outcome="fail", evidence="반대 자료", evidence_ids=["E1"])
    # 0.5 → 0.05 → 0.0(하한에서 잘림). 축3의 표 값은 -0.15인데 실변화는 -0.05다.
    assert audit.claims[0]["confidence"] == 0.0
    assert audit.claims[0]["axis_results"][2]["delta"] == pytest.approx(-0.05)

    audit.update_verdict(claim_id="C1", axis=2, outcome="pass", evidence="정정", evidence_ids=["E1"])
    assert audit.claims[0]["confidence"] == pytest.approx(0.55)
    assert [r["delta"] for r in audit.claims[0]["axis_results"]] == pytest.approx([0.0, 0.20, -0.15])


def test_access_failure_and_missing_counter_evidence_do_not_move_the_score():
    """점수는 '참일 확률'이 아니라 '확보한 지지 근거의 양'이다."""
    assert axis_delta(1, "undecidable") == 0.0
    assert axis_delta(2, "undecidable") == 0.0
    assert axis_delta(3, "pass") == 0.0


def test_verdict_derivation_is_order_independent():
    results = [
        {"axis": 2, "outcome": "pass", "suggested_verdict": None},
        {"axis": 1, "outcome": "pass", "suggested_verdict": None},
    ]
    assert derive_claim_verdict(results) == "supported"
    assert derive_claim_verdict(list(reversed(results))) == "supported"


def test_late_axis1_does_not_overturn_axis2_except_fail():
    supported = [
        {"axis": 2, "outcome": "pass", "suggested_verdict": None},
        {"axis": 1, "outcome": "undecidable", "suggested_verdict": None},
    ]
    assert derive_claim_verdict(supported) == "supported"
    with_fail = supported + [{"axis": 1, "outcome": "fail", "suggested_verdict": None}]
    assert derive_claim_verdict(with_fail) == "no_source"


def test_axis3_never_touches_the_verdict_column():
    """축3이 찾는 반대·한정 문헌은 참인 문장에도 거의 항상 존재한다 —
    그것으로 판정을 강등하면 시그니처 산출물이 판정 열을 오염시킨다."""
    for current in ("supported", "unsupported", "overstated", "undecidable", "no_source"):
        assert decide_verdict(3, "fail", None, current) == current
        assert decide_verdict(3, "fail", "overstated", current) == current


def test_axis3_success_does_not_contaminate_the_verdict_column():
    """전부 참이고 정확히 인용된 문단에서 축3이 한정 문헌을 찾아도 미지지가 되면 안 된다."""
    audit = Audit(" ".join(f"참인 진술 {i}번이 여기 있다." for i in range(4)))
    audit.register_evidence(tool="search_web", query="q", url="https://a.test", title="자료")
    _challenge(audit, "반박 자료 검색")
    _challenge(audit, "다른 각도의 한정 문헌 검색")
    for i in range(4):
        _claim(audit, i, f"참인 진술 {i}번이 여기 있다")
        cid = f"C{i + 1}"
        audit.update_verdict(claim_id=cid, axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
        audit.update_verdict(claim_id=cid, axis=2, outcome="pass", evidence="대조", evidence_ids=["E1"])
    # 4건 중 3건에서 축3이 한정 문헌을 찾았다.
    for cid in ("C1", "C2", "C3"):
        out = audit.update_verdict(
            claim_id=cid,
            axis=3,
            outcome="fail",
            evidence="언급되지 않은 한정 문헌을 찾았다",
            evidence_ids=["E1"],
            verdict="overstated",
        )
        assert out["data"]["verdict"] == "supported"
        assert "축3은 판정을 바꾸지 않는다" in out["data"]["warning"]
        audit.record_omission(claim_id=cid, evidence_id="E1", summary="이 자료가 주장을 한정한다")

    assert [c["verdict"] for c in audit.claims] == ["supported"] * 4
    assert audit.unsupported_rate() == (0, 4)  # 화면 수치와 보고서 첫 줄이 같은 0/4를 말한다
    assert len(audit.omissions) == 3  # 반박은 판정이 아니라 목록으로 말한다
    assert audit.completion_report()["complete"] is True


def test_headline_numerator_has_one_definition():
    from core.audit import UNSUPPORTED_VERDICTS

    assert UNSUPPORTED_VERDICTS == ("unsupported", "overstated")
    assert "no_source" not in UNSUPPORTED_VERDICTS


def test_short_numeric_anchor_cannot_attach_to_a_longer_number():
    """정규화가 문장부호를 지우므로 100 ⊂ 1004 가 성립한다 — 경계를 검사해야 한다."""
    audit = Audit("성장률은 100% 늘었다. 회원은 1004명이다.")
    bad = _claim(audit, 1, "100")
    assert bad["ok"] is False
    assert audit.claims == []
    good = _claim(audit, 0, "100% 늘었다")
    assert good["ok"] is True


def test_anchor_that_fits_several_sentences_is_rejected():
    audit = Audit("첫 문장은 매출이 늘었다고 말한다. 둘째 문장도 매출이 늘었다고 말한다.")
    out = _claim(audit, 0, "매출이 늘었다")
    assert out["ok"] is False
    assert out["data"]["ambiguous_indices"] == [0, 1]
    assert audit.claims == []


def test_repeated_whole_sentence_anchors_where_the_model_pointed():
    audit = Audit("같은 문장이 반복된다. 같은 문장이 반복된다. 다른 문장이다.")
    out = _claim(audit, 1, "같은 문장이 반복된다.")
    assert out["ok"] is True
    assert audit.claims[0]["index"] == 1
    assert "반복된다" in out["data"]["warning"]


def test_one_character_anchor_is_rejected():
    audit = _audit_two_sentences()
    out = _claim(audit, 0, "는")
    assert out["ok"] is False
    assert out["data"]["min_anchor_chars"] == 3


def test_challenge_search_promotes_the_stance_of_a_known_document():
    """확증 검색이 먼저 데려온 문헌을 반증 검색이 다시 데려오면 그것은 반증 자료다."""
    audit = _audit_two_sentences()
    first = audit.register_evidence(
        tool="search_web", query="coffee benefits", url="https://ex.test/a", stance="support"
    )
    again = audit.register_evidence(
        tool="search_scholar", query="coffee harms limitations", url="https://ex.test/a", stance="challenge"
    )
    assert first["id"] == again["id"]
    assert again["stance"] == "challenge"
    # 반대 방향으로는 강등되지 않는다.
    audit.register_evidence(tool="search_web", query="coffee benefits", url="https://ex.test/a")
    assert audit.evidence[0]["stance"] == "challenge"


# ── 검색 귀속과 쌍 (T-02) ────────────────────────────────────────────────────
def test_search_must_name_a_registered_claim_or_declare_exploration():
    audit = _with_evidence()
    assert audit.check_search("C9", "support", "질의") is not None  # 모르는 클레임
    assert audit.check_search("C1", "support", "질의") is None
    # 클레임 등록 전 탐색 경로는 막지 않는다.
    assert audit.check_search(EXPLORATORY_CLAIM_ID, "support", "질의") is None
    assert audit.check_search("C1", "support", "   ") is not None  # 빈 질의


def test_relabelled_same_query_is_refused():
    """라벨만 바꾼 같은 질의는 반증 검색이 아니다 — 프롬프트 규칙을 호스트 규칙으로 만든다."""
    audit = _with_evidence()
    audit.note_search("support", "커피 각성 효과 근거", claim_id="C1", endpoint="web")
    refusal = audit.check_search("C1", "challenge", "커피  각성 효과 근거!")
    assert refusal is not None
    assert refusal["data"]["existing_stance"] == "support"
    # 다른 질의는 통과하고, 다른 클레임이면 같은 질의도 통과한다.
    assert audit.check_search("C1", "challenge", "커피 각성 효과 한계 반박") is None


def test_non_auditable_claim_cannot_be_searched():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다", claim_type="normative", auditable=False)
    assert audit.check_search("C1", "support", "질의") is not None


def test_pair_metric_counts_claims_with_two_distinct_queries():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다")
    _claim(audit, 1, "성인의 62%가 매일 마신다")
    audit.note_search("support", "커피 각성 근거", claim_id="C1", endpoint="web")
    audit.note_search("challenge", "커피 각성 한계 반박", claim_id="C1", endpoint="web")
    audit.note_search("support", "섭취율 통계", claim_id="C2", endpoint="web")
    # 탐색용 반증은 어느 클레임의 쌍으로도 세지 않는다.
    audit.note_search("challenge", "아무 반박", claim_id=EXPLORATORY_CLAIM_ID, endpoint="web")
    assert audit.paired_claims() == ["C1"]
    assert audit.completion_report()["claims_with_support_challenge_pair"] == 1


def test_search_ledger_is_exposed_for_after_the_fact_audit():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다")
    entry = audit.note_search("challenge", "반박 질의", claim_id="C1", endpoint="scholar")
    audit.mark_search_result(entry, True, 2)
    row = audit.to_dict()["searches"][0]
    assert set(row) == {
        "claim_id",
        "endpoint",
        "stance",
        "query",
        "dispatched_at",
        "result_status",
    }
    assert (row["claim_id"], row["endpoint"], row["stance"], row["result_status"]) == (
        "C1",
        "scholar",
        "challenge",
        "ok",
    )
    audit.mark_search_result(entry, True, 0)
    assert audit.to_dict()["searches"][0]["result_status"] == "empty"
    audit.mark_search_result(entry, False, 0)
    assert audit.to_dict()["searches"][0]["result_status"] == "failed"


# ── 부정 판정의 근거 (F-04) ──────────────────────────────────────────────────
@pytest.mark.parametrize("axis", [2, 3])
def test_negative_findings_need_evidence(axis):
    """축2 fail은 대조했다는 사건, 축3 fail은 찾아냈다는 사건이다 — 본 것이 있어야 한다."""
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
    if axis == 3:
        audit.update_verdict(claim_id="C1", axis=2, outcome="pass", evidence="대조", evidence_ids=["E1"])
    out = audit.update_verdict(
        claim_id="C1", axis=axis, outcome="fail", evidence="근거 없이 부정", evidence_ids=[]
    )
    assert out["ok"] is False
    assert "undecidable" in out["error"] and "축1" in out["error"]
    # 인용하면 통과한다.
    assert audit.update_verdict(
        claim_id="C1", axis=axis, outcome="fail", evidence="대조 결과", evidence_ids=["E1"]
    )["ok"] is True


def test_axis1_fail_may_still_cite_nothing():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=1, outcome="fail", evidence="지목된 출처를 못 찾음", evidence_ids=[]
    )
    assert out["ok"] is True  # 부재의 주장은 축1에서만 성립한다


def test_empty_evidence_sentence_is_rejected():
    audit = _with_evidence()
    out = audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="  ", evidence_ids=["E1"])
    assert out["ok"] is False and "evidence" in out["error"]


def test_omission_requires_an_auditable_surviving_claim_and_a_summary():
    audit = _with_evidence()
    assert audit.record_omission(claim_id="C1", evidence_id="E1", summary="  ")["ok"] is False

    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    blocked = audit.record_omission(claim_id="C1", evidence_id="E1", summary="반박 자료다")
    assert blocked["ok"] is False and blocked["data"]["claim_terminal"] is True

    opinion = Audit("정부는 규제를 강화해야 한다.")
    opinion.register_evidence(tool="search_web", query="q", url="https://a.test")
    opinion.record_claim(
        index=0, text="정부는 규제를 강화해야 한다", claim_type="normative", auditable=False
    )
    assert opinion.record_omission(claim_id="C1", evidence_id="E1", summary="반박")["ok"] is False


def test_omission_from_a_support_search_is_allowed_but_flagged():
    audit = _with_evidence()  # E1은 support 검색에서 왔다
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
    out = audit.record_omission(claim_id="C1", evidence_id="E1", summary="이 자료가 주장을 한정한다")
    assert out["ok"] is True  # 실재하는 자료를 막지는 않는다
    assert out["data"]["stance_mismatch"] is True
    assert "확증(support) 검색" in out["data"]["warning"]


# ── 점수의 뜻 (F-05) ─────────────────────────────────────────────────────────
def test_score_is_described_as_a_stage_score_not_an_amount_of_evidence():
    from core.audit import SCORE_MEANS

    assert "단계 점수" in SCORE_MEANS
    assert "근거의 양" not in SCORE_MEANS

    audit = _with_evidence()
    audit.register_evidence(tool="fetch_source", query="u", url="https://a.test/doc")
    one = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"]
    )
    many = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="대조", evidence_ids=["E1", "E2"]
    )
    # 같은 판정이면 근거가 몇 건이든 같은 폭으로 움직인다. 실제 양은 따로 센다.
    assert one["data"]["evidence_count"] == 1
    assert many["data"]["evidence_count"] == 2
    assert many["data"]["fetched_source_count"] == 1
    assert many["data"]["score_means"] == SCORE_MEANS


# ── 분류·클레임 불변식 (F-06) ────────────────────────────────────────────────
def _classify(audit: Audit, **kw):
    args = dict(
        input_kind="ai_answer", lang="ko", auditable=True, sentence_count=2, rationale="사실 주장이 있다"
    )
    args.update(kw)
    return audit.record_classification(**args)


def test_classification_is_idempotent_and_cannot_flip_after_claims():
    audit = _audit_two_sentences()
    assert _classify(audit)["ok"] is True
    same = _classify(audit, rationale="같은 결정, 다른 문장")
    assert same["ok"] is True and same["data"]["recorded"] is False

    flipped = _classify(audit, auditable=False)
    assert flipped["ok"] is False and "뒤집을 수 없다" in flipped["error"]
    assert audit.classification["auditable"] is True

    _claim(audit, 0, "커피는 각성 효과가 있다")
    after = _classify(audit, input_kind="news_or_blog")
    assert after["ok"] is False and "클레임이 이미 등록된" in after["error"]


def test_opinion_input_with_auditable_true_is_allowed_with_a_note():
    audit = _audit_two_sentences()
    out = _classify(audit, input_kind="opinion_or_creative")
    assert out["ok"] is True  # 사설에도 검증 가능한 통계가 섞인다
    assert "opinion_or_creative" in out["data"]["warning"]


def test_normative_claims_cannot_be_registered_as_auditable():
    audit = _audit_two_sentences()
    out = _claim(audit, 0, "커피는 각성 효과가 있다", claim_type="normative", auditable=True)
    assert out["ok"] is False and "normative" in out["error"]


def test_duplicate_claim_folds_without_spending_the_cap():
    audit = Audit("가나다라마 바사아자차. 다른 문장이다.", max_claims=2)
    first = _claim(audit, 0, "가나다라마 바사아자차")
    again = _claim(audit, 0, "가나다라마  바사아자차!")
    assert first["data"]["claim_id"] == "C1"
    assert again["ok"] is True and again["data"]["duplicate_of"] == "C1"
    assert again["data"]["recorded"] is False
    assert len(audit.claims) == 1
    assert again["data"]["claims_remaining"] == 1  # 상한을 먹지 않았다


# ── 값 형식 (F-07) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"lang": "korean"}, "ISO 639-1"),
        ({"lang": ""}, "ISO 639-1"),
        ({"sentence_count": -3}, "0 이상"),
        ({"rationale": "   "}, "rationale"),
        ({"input_kind": "essay"}, "input_kind"),
    ],
)
def test_classification_values_are_validated(kwargs, fragment):
    audit = _audit_two_sentences()
    out = _classify(audit, **kwargs)
    assert out["ok"] is False and fragment in out["error"]
    assert audit.classification is None



def test_prompt_score_language_matches_the_computation():
    from core.agent import PROMPT_PATH

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "단계 점수" in prompt
    assert "evidence_count" in prompt


def test_axis_order_violation_is_rejected_with_recovery_path():
    audit = _with_evidence()
    out = audit.update_verdict(
        claim_id="C1", axis=3, outcome="fail", evidence="반증", evidence_ids=[]
    )
    assert out["ok"] is False
    assert out["data"]["expected_next_axis"] == 1
    assert "skip" in out["error"]


def test_axis1_fail_terminates_the_claim_but_can_be_corrected():
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    blocked = audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="대조", evidence_ids=["E1"]
    )
    assert blocked["ok"] is False and blocked["data"]["claim_terminal"] is True
    fixed = audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="다시 찾음", evidence_ids=["E1"]
    )
    assert fixed["ok"] is True
    assert audit.update_verdict(
        claim_id="C1", axis=2, outcome="pass", evidence="대조", evidence_ids=["E1"]
    )["ok"] is True


def test_axis4_is_not_recordable():
    audit = _with_evidence()
    out = audit.update_verdict(claim_id="C1", axis=4, outcome="pass", evidence="x", evidence_ids=["E1"])
    assert out["ok"] is False and out["data"]["allowed_axes"] == [1, 2, 3]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"outcome": "maybe"},
        {"outcome": "pass", "verdict": "probably"},
    ],
)
def test_invalid_enums_are_rejected(kwargs):
    audit = _with_evidence()
    args = dict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    args.update(kwargs)
    assert audit.update_verdict(**args)["ok"] is False


def test_skip_is_not_a_verdict_and_leaves_the_claim_pending():
    """skip은 '안 했다'이지 '확인할 수 없었다'가 아니다 — 미확정을 해소하면 안 된다."""
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    out = audit.update_verdict(
        claim_id="C1", axis=2, outcome="skip", evidence="하지 않았다", evidence_ids=[]
    )
    assert out["ok"] is True  # 축 순서는 지나간다
    assert out["data"]["verdict"] == "pending"
    assert audit.completion_report()["pending_claims"] == ["C1"]
    # 확인을 시도했으나 판정이 안 서는 것은 다른 사건이고, 그것은 미확정을 해소한다.
    audit.update_verdict(
        claim_id="C1", axis=2, outcome="undecidable", evidence="본문으로 판단 불가", evidence_ids=[]
    )
    assert audit.claims[0]["verdict"] == "undecidable"


def test_clamp_and_budget_helpers():
    assert clamp01(-1) == 0.0 and clamp01(2) == 1.0
    assert plan_claim_budget(0, 3) == 0
    assert plan_claim_budget(12, 0) == 0
    assert plan_claim_budget(12, 5) == 2
    assert plan_claim_budget(2, 5) == 1


# ── 파생 수치 · 직렬화 ───────────────────────────────────────────────────────
def test_coverage_denominator_excludes_structure():
    audit = Audit(CHATBOT)
    claimable = sorted(audit.claimable_indices())
    _claim(audit, claimable[0], audit.sentences[claimable[0]])
    audit.register_evidence(tool="search_web", query="q", url="https://a.test")
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    covered, coverable = audit.coverage()
    assert covered == 1
    assert coverable == len(claimable) < len(audit.sentences)


def test_unsupported_rate_excludes_no_source():
    audit = Audit("가짜 통계다. 실재하는 주장이다.")
    _claim(audit, 0, "가짜 통계다")
    _claim(audit, 1, "실재하는 주장이다")
    audit.register_evidence(tool="search_web", query="q", url="https://a.test")
    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    audit.update_verdict(claim_id="C2", axis=1, outcome="pass", evidence="찾음", evidence_ids=["E1"])
    audit.update_verdict(claim_id="C2", axis=2, outcome="fail", evidence="불일치", evidence_ids=["E1"])
    assert audit.unsupported_rate() == (1, 2)
    assert audit.no_source_count() == 1


def test_completion_report_gates_on_pending_and_axis3():
    audit = _with_evidence()
    assert audit.completion_report()["complete"] is False  # 미확정 클레임
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    audit.update_verdict(claim_id="C1", axis=2, outcome="pass", evidence="e", evidence_ids=["E1"])
    report = audit.completion_report()
    assert report["complete"] is False
    assert "축3" in report["missing_actions"][0]
    audit.update_verdict(claim_id="C1", axis=3, outcome="pass", evidence="반증 없음", evidence_ids=["E1"])
    _challenge(audit, "커피 각성 효과 반박")
    assert audit.completion_report()["complete"] is False  # 누락 증거가 아직 0건이다
    audit.record_omission(claim_id="C1", evidence_id="E1", summary="이 자료가 주장을 한정한다")
    assert audit.completion_report()["complete"] is True


def _audit_with_survivors(count: int, axis3_on: int) -> Audit:
    """축1·2를 통과한 클레임 count건 중 axis3_on건에만 축3을 수행한 상태를 만든다."""
    audit = Audit(" ".join(f"주장 {i}번이 여기 있다." for i in range(count)))
    audit.register_evidence(tool="search_web", query="q", url="https://a.test", title="자료")
    for i in range(count):
        _challenge(audit, f"주장 {i} 반박 자료")
        _claim(audit, i, f"주장 {i}번이 여기 있다")
        cid = f"C{i + 1}"
        audit.update_verdict(claim_id=cid, axis=1, outcome="pass", evidence="존재", evidence_ids=["E1"])
        audit.update_verdict(claim_id=cid, axis=2, outcome="pass", evidence="대조", evidence_ids=["E1"])
        if i < axis3_on:
            audit.update_verdict(
                claim_id=cid, axis=3, outcome="pass", evidence="반증 검토", evidence_ids=["E1"]
            )
    if axis3_on:
        audit.record_omission(claim_id="C1", evidence_id="E1", summary="이 자료가 주장을 한정한다")
    return audit


def test_axis3_collapse_is_not_complete():
    """축3이 12건에서 1건으로 무너진 런을 완주라고 부르면, 안 한 것을 한 것처럼 보이게 된다."""
    collapsed = _audit_with_survivors(12, 1)
    report = collapsed.completion_report()
    assert report["complete"] is False
    assert (report["axis3_done"], report["axis3_expected"], report["axis3_required"]) == (1, 12, 6)
    assert "축3" in report["missing_actions"][0]

    assert _audit_with_survivors(12, 6).completion_report()["complete"] is True
    # 하한 max(1, …) — 생존 클레임이 1건이면 1건으로 충족된다.
    assert _audit_with_survivors(1, 1).completion_report()["complete"] is True
    assert _audit_with_survivors(1, 0).completion_report()["complete"] is False


def test_challenge_queries_are_counted_at_dispatch_not_from_the_ledger():
    """이미 본 URL만 돌려준 반증 검색은 원장에 자국을 남기지 않는다 — 쏜 것은 쏜 것이다."""
    audit = _audit_two_sentences()
    audit.register_evidence(tool="search_web", query="확증 질의", url="https://a.test/x")

    audit.note_search("challenge", "커피 각성 효과 반박 자료")
    audit.register_evidence(
        tool="search_web", query="반박 질의", url="https://a.test/x", stance="challenge"
    )
    audit.note_search("challenge", "커피 각성 효과 한계 연구")
    audit.register_evidence(
        tool="search_web", query="한계 질의", url="https://a.test/x/", stance="challenge"
    )

    assert len(audit.evidence) == 1  # URL 하나로 접혔다
    assert audit.challenge_query_count() == 2  # 그래도 두 발은 나갔다
    assert audit.search_counts() == {"support": 0, "challenge": 2}


def test_challenge_floor_gates_completion():
    audit = _audit_with_survivors(4, 4)  # 헬퍼가 클레임마다 반증을 한 발씩 쏜다
    report = audit.completion_report()
    assert report["challenge_queries"] == 4
    assert report["challenge_required"] == 2  # ceil(4 * 0.5)
    assert report["complete"] is True

    starved = _audit_with_survivors(4, 4)
    starved.searches = [e for e in starved.searches if e["stance"] != "challenge"]
    starved_report = starved.completion_report()
    assert starved_report["challenge_queries"] == 0
    assert starved_report["complete"] is False
    assert "반증 검색" in starved_report["missing_actions"][-1]


def test_challenge_floor_has_a_minimum_of_one():
    audit = _with_evidence()
    audit.note_search("support", "커피 각성 효과 근거")
    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    report = audit.completion_report()
    assert report["challenge_required"] == 1  # 감사 대상 1건이어도 반증은 한 발 나가야 한다
    assert report["complete"] is False
    _challenge(audit, "커피 각성 효과 반박")
    assert audit.completion_report()["complete"] is True


def test_repeated_and_failed_challenge_searches_do_not_fill_the_gate():
    """게이트는 발사가 아니라 회수를 센다 — 같은 질의 반복도, 전멸한 검색도 감사가 아니다."""
    audit = _audit_two_sentences()
    for _ in range(3):
        _challenge(audit, "완전히 똑같은 질의")
    _challenge(audit, "결과가 0건인 질의", results=0)
    _challenge(audit, "429로 실패한 질의", ok=False, results=0)

    assert audit.challenge_query_count() == 5  # 발사 관측치는 그대로
    assert audit.effective_challenge_count() == 1  # 게이트가 세는 것은 하나뿐이다

    _challenge(audit, "다른 각도의 반박 질의")
    assert audit.effective_challenge_count() == 2


def test_axis3_floor_survives_a_run_that_never_recorded_axis1():
    """분모가 수행 이력이면 아무것도 안 한 런에서 게이트가 사라진다 — 성실한 런만 벌한다."""
    audit = Audit(" ".join(f"주장 {i}번이 여기 있다." for i in range(4)))
    for i in range(4):
        _claim(audit, i, f"주장 {i}번이 여기 있다")
    done, expected, required = audit.axis3_progress()
    assert (done, expected, required) == (0, 4, 2)
    assert audit.completion_report()["complete"] is False


def test_axis1_failed_claims_leave_the_axis3_denominator():
    audit = _with_evidence()
    _challenge(audit, "커피 각성 효과 반박")
    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    done, expected, required = audit.axis3_progress()
    assert (done, expected, required) == (0, 0, 0)  # 종결된 클레임에 축3을 요구하지 않는다


def test_zero_omissions_is_not_a_complete_audit():
    """시그니처 산출물이 0건인 런을 완주라고 부르면 화면이 '찾아봤지만 없었다'고 단정한다."""
    audit = _with_evidence()
    _challenge(audit, "커피 각성 효과 반박")
    for axis in (1, 2, 3):
        audit.update_verdict(
            claim_id="C1", axis=axis, outcome="pass", evidence="검토했다", evidence_ids=["E1"]
        )
    report = audit.completion_report()
    assert report["complete"] is False
    assert "누락 증거가 0건" in " ".join(report["missing_actions"])
    audit.record_omission(claim_id="C1", evidence_id="E1", summary="이 자료가 주장을 한정한다")
    assert audit.completion_report()["complete"] is True


def test_note_search_normalizes_unknown_stance():
    audit = _audit_two_sentences()
    audit.note_search("sideways", "이상한 방향")
    assert audit.search_counts() == {"support": 1, "challenge": 0}


def test_skipped_axis3_does_not_count_as_executed():
    audit = _audit_with_survivors(2, 0)
    audit.update_verdict(claim_id="C1", axis=3, outcome="skip", evidence="시간 없음", evidence_ids=[])
    report = audit.completion_report()
    assert report["axis3_done"] == 0 and report["complete"] is False


def test_recorded_omission_counts_as_axis3_execution():
    audit = _audit_with_survivors(2, 0)
    audit.record_omission(claim_id="C1", evidence_id="E1", summary="이 자료가 주장을 한정한다")
    assert audit.completion_report()["axis3_done"] == 1


def test_value_claim_pending_is_not_partial_audit():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다", claim_type="normative", auditable=False)
    report = audit.completion_report()
    assert report["auditable_claims"] == 0
    assert "감사 대상 클레임" in report["missing_actions"][0]


def test_axis1_fail_only_run_can_still_complete():
    audit = _with_evidence()
    _challenge(audit, "커피 각성 효과 반박")
    audit.update_verdict(claim_id="C1", axis=1, outcome="fail", evidence="못 찾음", evidence_ids=[])
    assert audit.completion_report()["complete"] is True


def test_stream_snapshot_is_slim_but_keeps_the_ui_contract():
    audit = _with_evidence()
    audit.register_evidence(tool="search_web", query="q", url="https://unused.test")
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    stream = audit.to_dict(stream=True)
    full = audit.to_dict()
    assert "input_text" not in stream and "input_text" in full
    assert [r["id"] for r in stream["evidence"]] == ["E1"]
    assert len(full["evidence"]) == 2
    assert stream["evidence_total"] == 2 and stream["evidence_cited"] == 1
    for key in (
        "sentences",
        "sentence_kinds",
        "claims",
        "omissions",
        "coverage",
        "claims_by_index",
        "no_source_count",
        "source_mode",
        "status",
    ):
        assert key in stream


def test_classification_returns_the_host_coordinate_table():
    audit = Audit(CHATBOT)
    out = audit.record_classification(
        input_kind="ai_answer", lang="ko", auditable=True, sentence_count=3, rationale="사실 주장이 있다"
    )
    data = out["data"]
    assert data["host_sentences"][0] == {
        "index": 0,
        "text": "## 노트북이 뜨거워지는 이유",
        "kind": "heading",
    }
    assert data["auditable_sentence_count"] == len(audit.claimable_indices())
    assert data["max_claims"] == DEFAULT_MAX_CLAIMS
    assert "호스트 좌표" in data["warning"]


def test_non_auditable_classification_sets_status():
    audit = Audit("정부는 규제를 강화해야 한다.")
    audit.record_classification(
        input_kind="opinion_or_creative",
        lang="ko",
        auditable=False,
        sentence_count=1,
        rationale="가치명제다",
    )
    assert audit.status == "non_auditable"
    assert audit.completion_report()["complete"] is True
