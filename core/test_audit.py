"""core/audit.py 단위 테스트 — 좌표계 · 증거 원장 · 판정 계산."""

from __future__ import annotations

import pytest

from core.audit import (
    BASE_CONFIDENCE,
    DEFAULT_MAX_CLAIMS,
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


def test_axis3_counter_evidence_downgrades_supported_only():
    assert decide_verdict(3, "fail", None, "supported") == "overstated"
    assert decide_verdict(3, "fail", None, "unsupported") == "unsupported"


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


def test_skip_keeps_the_claim_out_of_pending():
    audit = _with_evidence()
    audit.update_verdict(claim_id="C1", axis=1, outcome="pass", evidence="e", evidence_ids=["E1"])
    out = audit.update_verdict(claim_id="C1", axis=2, outcome="skip", evidence="시간 없음", evidence_ids=[])
    assert out["data"]["verdict"] == "undecidable"


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
    assert audit.completion_report()["complete"] is True


def test_value_claim_pending_is_not_partial_audit():
    audit = _audit_two_sentences()
    _claim(audit, 0, "커피는 각성 효과가 있다", claim_type="normative", auditable=False)
    report = audit.completion_report()
    assert report["auditable_claims"] == 0
    assert "감사 대상 클레임" in report["missing_actions"][0]


def test_axis1_fail_only_run_can_still_complete():
    audit = _with_evidence()
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
