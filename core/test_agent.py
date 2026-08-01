"""core/agent.py · core/model_tools.py 테스트 — 이벤트 계약 · 예산 · 종결 회계."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agents import set_tracing_disabled
from agents.models.interface import Model
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from core.agent import (
    DEFAULT_MODEL,
    MAX_CLAIMS_PLACEHOLDER,
    PARTIAL_REASONS,
    PROMPT_PATH,
    RUN_REASONS,
    Audit,
    fallback_report,
    load_instructions,
    resolve_model,
    run_audit,
)
from core.audit import DEFAULT_MAX_CLAIMS
from core.model_tools import (
    ALL_TOOLS,
    FIRST_TOOL_NAME,
    AuditContext,
    estimate_search_cost,
)

set_tracing_disabled(True)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT = "커피는 각성 효과가 있다. 성인의 62%가 매일 카페인을 섭취한다."


# ── FakeModel — 실 API 없이 루프를 결정적으로 돌린다 ─────────────────────────
def _message(text: str):
    return ResponseOutputMessage(
        id="m",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _tool_call(name: str, args: dict, call_id: str):
    return ResponseFunctionToolCall(
        id="f", type="function_call", call_id=call_id, name=name, arguments=json.dumps(args)
    )


class FakeModel(Model):
    """턴마다 미리 짜인 출력을 돌려주는 모델. 마지막 턴 이후에는 마무리 메시지를 낸다."""

    def __init__(self, turns, *, delay_s: float = 0.0, repeat_last: bool = False):
        self.turns = list(turns)
        self.delay_s = delay_s
        self.repeat_last = repeat_last
        self.tool_choices: list = []
        self.calls = 0

    async def get_response(self, *args, **kwargs):  # pragma: no cover - 스트림만 쓴다
        raise NotImplementedError

    async def stream_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        self.tool_choices.append(model_settings.tool_choice)
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.turns:
            output = self.turns[0] if self.repeat_last and len(self.turns) == 1 else self.turns.pop(0)
        else:
            output = [_message("마무리한다.")]
        response = Response(
            id="r",
            created_at=0.0,
            model="fake",
            object="response",
            output=output,
            parallel_tool_calls=True,
            tool_choice="auto",
            tools=[],
        )
        yield ResponseCompletedEvent(
            type="response.completed", response=response, sequence_number=0
        )


CLASSIFY = _tool_call(
    "record_classification",
    {
        "input_kind": "ai_answer",
        "lang": "ko",
        "auditable": True,
        "sentence_count": 2,
        "rationale": "검증 가능한 사실 주장이 있다",
    },
    "t1",
)
SEARCH = _tool_call(
    "search_web",
    {"query": "카페인 각성 효과", "max_results": 3, "lang": "ko", "date_range": None},
    "t2",
)
CLAIM = _tool_call(
    "record_claim",
    {
        "index": 1,
        "text": "성인의 62%가 매일 카페인을 섭취한다",
        "claim_type": "statistical",
        "auditable": True,
        "prior": 0.6,
        "cited_source": None,
    },
    "t3",
)
AXIS1 = _tool_call(
    "update_verdict",
    {
        "claim_id": "C1",
        "axis": 1,
        "outcome": "pass",
        "evidence": "카페인 섭취율을 다룬 자료가 존재한다",
        "evidence_ids": ["E1"],
        "verdict": None,
    },
    "t4",
)
FULL_RUN_TURNS = [
    [CLASSIFY],
    [SEARCH],
    [CLAIM],
    [AXIS1],
    [_message("### 최종 보고\n뒷받침 안 됨 0/1 · 출처 못 찾음 0건")],
]


async def _collect(model, **kwargs) -> list[dict]:
    events = []
    async for event in run_audit(TEXT, model=model, **kwargs):
        events.append(event)
    return events


def _last_status(events: list[dict]) -> dict:
    return events[-1]["payload"]


# ── 회귀 1: 프롬프트 자리표시자 ──────────────────────────────────────────────
def test_prompt_ships_the_placeholder_and_injects_it():
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    assert MAX_CLAIMS_PLACEHOLDER in raw
    # 상한이 등장하는 자리마다 자리표시자를 쓴다 — 숫자를 두 곳에 적지 않는다.
    assert raw.count(MAX_CLAIMS_PLACEHOLDER) >= 2

    rendered = load_instructions(7)
    assert MAX_CLAIMS_PLACEHOLDER not in rendered
    assert "{{" not in rendered
    assert "최대 7개" in rendered


def test_missing_placeholder_fails_loudly(tmp_path):
    broken = tmp_path / "system.md"
    broken.write_text("상한은 12개다.", encoding="utf-8")
    with pytest.raises(ValueError):
        load_instructions(12, path=broken)


# ── 회귀 2: 기본 클레임 상한 ─────────────────────────────────────────────────
def test_default_claim_cap_is_at_least_ten():
    import inspect

    assert DEFAULT_MAX_CLAIMS >= 10
    signature_default = inspect.signature(run_audit).parameters["max_claims"].default
    assert signature_default is DEFAULT_MAX_CLAIMS >= 10


def test_model_default_and_env_override(monkeypatch):
    monkeypatch.delenv("TRACER_MODEL", raising=False)
    assert resolve_model() == DEFAULT_MODEL == "gpt-5.6-terra"
    monkeypatch.setenv("TRACER_MODEL", "gpt-5.6-luna")
    assert resolve_model() == "gpt-5.6-luna"
    assert resolve_model("gpt-5.6-sol") == "gpt-5.6-sol"


def test_reason_inventory_is_pinned():
    assert RUN_REASONS == (
        "complete",
        "incomplete",
        "timebox",
        "max_turns",
        "non_auditable",
        "error",
    )
    assert set(PARTIAL_REASONS) < set(RUN_REASONS)
    assert "complete" not in PARTIAL_REASONS


def test_tool_inventory_is_seven_with_forced_first_call():
    names = [t.name for t in ALL_TOOLS]
    assert names == [
        "search_web",
        "search_scholar",
        "fetch_source",
        "record_classification",
        "record_claim",
        "update_verdict",
        "record_omission",
    ]
    assert FIRST_TOOL_NAME == "record_classification"


# ── 미니 런: 클레임 1개가 축1 판정까지 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_mini_run_reaches_an_axis1_verdict():
    model = FakeModel(FULL_RUN_TURNS)
    events = await _collect(model, timebox_s=20, max_claims=5)

    status = _last_status(events)
    assert events[-1]["kind"] == "status" and status["done"] is True
    assert [e for e in events if e["kind"] == "status" and e["payload"]["done"]] == [events[-1]]

    audit = status["audit"]
    claim = audit["claims"][0]
    assert claim["id"] == "C1" and claim["index"] == 1
    assert claim["axis_results"][0]["axis"] == 1
    assert claim["axis_results"][0]["outcome"] == "pass"
    assert claim["axis_results"][0]["evidence_ids"] == ["E1"]
    # URL은 모델이 쓴 것이 아니라 호스트가 원장에서 채운 것이다.
    assert claim["axis_results"][0]["source_urls"] == [audit["evidence"][0]["url"]]
    assert claim["confidence"] > claim["prior"]
    assert audit["source_mode"] in ("mock", "live", "unknown")
    assert status["final_report"].startswith("### 최종 보고")
    # 축2·3을 안 했으므로 완주가 아니다 — 안 한 것을 한 것처럼 말하지 않는다.
    assert status["reason"] == "incomplete" and status["partial"] is True
    assert "C1" in " ".join(status["completion"]["missing_actions"])


@pytest.mark.asyncio
async def test_first_tool_call_is_forced_to_classification():
    model = FakeModel(FULL_RUN_TURNS)
    await _collect(model, timebox_s=20)
    assert model.tool_choices[0] == FIRST_TOOL_NAME
    # 툴을 한 번 쓰면 SDK가 auto로 되돌린다 — 강제가 루프에 갇히지 않는다.
    assert model.tool_choices[1] is None


@pytest.mark.asyncio
async def test_all_four_relay_event_kinds_are_emitted_with_the_wire_shape():
    model = FakeModel(FULL_RUN_TURNS)
    events = await _collect(model, timebox_s=20)
    kinds = {e["kind"] for e in events}
    assert {"raw", "run_item", "audit", "status"} <= kinds
    assert kinds <= {"raw", "run_item", "audit", "status"}
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for e in events:
        assert set(e) == {"kind", "seq", "t", "payload"}
        assert isinstance(e["t"], float)
        json.dumps(e, ensure_ascii=False, default=str)  # 와이어로 나갈 수 있어야 한다


@pytest.mark.asyncio
async def test_tool_output_channel_carries_the_tool_result():
    """tool_result는 raw 스트림에 없다 — run_item(tool_output)이 규칙 충족의 필수 조건이다."""
    model = FakeModel(FULL_RUN_TURNS)
    events = await _collect(model, timebox_s=20)
    raw_dump = json.dumps([e["payload"] for e in events if e["kind"] == "raw"], default=str)
    assert "tool_call_output" not in raw_dump

    outputs = [
        e["payload"] for e in events if e["kind"] == "run_item" and e["payload"]["name"] == "tool_output"
    ]
    assert len(outputs) == 4
    first = outputs[0]["item"]["output"]
    assert first["ok"] is True
    assert "budget" in first and first["budget"]["timebox_s"] == 20
    called = [e for e in events if e["kind"] == "run_item" and e["payload"]["name"] == "tool_called"]
    assert len(called) == 4


@pytest.mark.asyncio
async def test_audit_events_are_emitted_only_for_successful_records():
    bad_claim = _tool_call(
        "record_claim",
        {
            "index": 0,
            "text": "성인의 62%가 매일 카페인을 섭취한다",
            "claim_type": "statistical",
            "auditable": True,
            "prior": 0.6,
            "cited_source": None,
        },
        "bad",
    )
    model = FakeModel([[CLASSIFY], [bad_claim], [CLAIM], [_message("끝")]])
    events = await _collect(model, timebox_s=20)
    audit_events = [e for e in events if e["kind"] == "audit"]
    # 분류 1 + 성공한 클레임 1 = 2건. 거부된 호출은 화면에 없는 상태 변화를 그리지 않는다.
    assert len(audit_events) == 2
    outputs = [
        e["payload"]["item"]["output"]
        for e in events
        if e["kind"] == "run_item" and e["payload"]["name"] == "tool_output"
    ]
    assert outputs[1]["ok"] is False
    assert outputs[1]["data"]["index_candidates"][0]["index"] == 1


# ── 종결 사유 ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_timebox_is_never_disguised_as_complete():
    model = FakeModel([[CLASSIFY]], delay_s=5.0)
    started = time.monotonic()
    events = await _collect(model, timebox_s=0.5)
    elapsed = time.monotonic() - started
    status = _last_status(events)
    assert status["reason"] == "timebox"
    assert status["partial"] is True
    assert status["audit"]["status"] == "partial"
    assert elapsed < 4.0  # 드레인 상한이 있어 예산을 크게 넘기지 않는다


@pytest.mark.asyncio
async def test_turn_backstop_has_its_own_reason():
    model = FakeModel([[CLASSIFY]], repeat_last=True)
    events = await _collect(model, timebox_s=20, max_turns=2)
    status = _last_status(events)
    assert status["reason"] == "max_turns"
    assert status["turn_backstop"] == 2
    assert status["tool_calls"] == 0  # 네트워크 상한과 무관한 사건이다


@pytest.mark.asyncio
async def test_turn_backstop_defaults_to_tool_budget_derivation():
    model = FakeModel(FULL_RUN_TURNS)
    events = await _collect(model, timebox_s=20, max_tool_calls=4)
    assert _last_status(events)["turn_backstop"] == 4 * 2 + 10


@pytest.mark.asyncio
async def test_non_auditable_classification_ends_the_run_with_its_own_reason():
    classify = _tool_call(
        "record_classification",
        {
            "input_kind": "opinion_or_creative",
            "lang": "ko",
            "auditable": False,
            "sentence_count": 2,
            "rationale": "가치명제뿐이다",
        },
        "n1",
    )
    model = FakeModel([[classify], [_message("사실 주장이 없어 감사하지 않았다.")]])
    events = await _collect(model, timebox_s=20)
    status = _last_status(events)
    assert status["reason"] == "non_auditable"
    assert status["partial"] is False
    assert status["audit"]["status"] == "non_auditable"


@pytest.mark.asyncio
async def test_complete_requires_the_completion_gate():
    axis2 = _tool_call(
        "update_verdict",
        {
            "claim_id": "C1",
            "axis": 2,
            "outcome": "pass",
            "evidence": "출처가 주장을 그대로 지지한다",
            "evidence_ids": ["E1"],
            "verdict": None,
        },
        "t5",
    )
    axis3 = _tool_call(
        "update_verdict",
        {
            "claim_id": "C1",
            "axis": 3,
            "outcome": "fail",
            "evidence": "반대 자료를 찾았다",
            "evidence_ids": ["E1"],
            "verdict": None,
        },
        "t6",
    )
    omission = _tool_call(
        "record_omission",
        {"claim_id": "C1", "evidence_id": "E2", "summary": "섭취율 추정치를 낮게 잡은 자료다"},
        "t7",
    )
    model = FakeModel(
        [[CLASSIFY], [SEARCH], [CLAIM], [AXIS1], [axis2], [axis3, omission], [_message("### 최종 보고")]]
    )
    events = await _collect(model, timebox_s=20)
    status = _last_status(events)
    assert status["reason"] == "complete"
    assert status["completion"]["complete"] is True
    assert status["audit"]["status"] == "complete"
    assert status["audit"]["omissions"][0]["url"]  # 호스트가 원장에서 채운 서지 정보
    assert status["audit"]["claims"][0]["verdict"] == "overstated"


@pytest.mark.asyncio
async def test_network_cap_refuses_the_call_but_not_the_run():
    model = FakeModel([[CLASSIFY], [SEARCH, SEARCH], [CLAIM], [AXIS1], [_message("끝")]])
    events = await _collect(model, timebox_s=20, max_tool_calls=1)
    status = _last_status(events)
    assert status["tool_calls"] == 1
    assert status["tool_calls_refused"] == 1
    assert status["reason"] in ("complete", "incomplete")  # 런은 끝까지 간다
    assert status["audit"]["claims"][0]["axis_results"]


@pytest.mark.asyncio
async def test_timing_block_reports_wall_clock_and_serial_time():
    model = FakeModel(FULL_RUN_TURNS)
    events = await _collect(model, timebox_s=20)
    timing = _last_status(events)["timing"]
    assert set(timing) == {
        "total_s",
        "model_s",
        "tool_wait_s",
        "tool_time_serial_s",
        "max_concurrent_tools",
        "parallel_speedup",
    }
    assert timing["model_s"] >= 0
    assert timing["total_s"] >= timing["tool_wait_s"]


@pytest.mark.asyncio
async def test_event_time_is_the_enqueue_time_not_the_drain_time():
    """느린 소비자가 붙어도 RAW 레인의 시간축은 사건이 일어난 시각이어야 한다."""
    model = FakeModel(FULL_RUN_TURNS)
    stamps, started = [], time.monotonic()
    async for event in run_audit(TEXT, model=model, timebox_s=20):
        if event["kind"] in ("raw", "run_item", "audit"):
            stamps.append(event["t"])
        await asyncio.sleep(0.03)
    consumed_s = time.monotonic() - started
    assert consumed_s > 0.3
    assert max(stamps) - min(stamps) < consumed_s / 2


# ── 예산 봉투 ────────────────────────────────────────────────────────────────
def _ctx(**kwargs) -> AuditContext:
    clock = kwargs.pop("clock", None)
    return AuditContext(audit=Audit(TEXT), clock=clock or time.monotonic, **kwargs)


def test_budget_envelope_is_judgeable_not_raw_counters():
    ctx = _ctx(timebox_s=90, max_tool_calls=30, max_claims=12)
    snap = ctx.budget_snapshot()
    for key in (
        "remaining_tool_calls",
        "remaining_s",
        "time_fraction",
        "claims_remaining",
        "fetch_allowed",
        "fetch_cutoff_s",
        "record_calls_used",
        "tool_calls_refused",
    ):
        assert key in snap
    assert snap["fetch_cutoff_s"] == 54.0
    assert snap["fetch_allowed"] is True
    # 카운터가 없으면 가격 키도 빠진다 — 셀 수 없는 것을 지어내지 않는다.
    assert ("endpoint_calls" in snap) == ("estimated_search_cost_usd" in snap)


def test_fetch_cutoff_is_machine_enforced():
    now = [0.0]
    ctx = _ctx(timebox_s=100, clock=lambda: now[0])
    assert ctx.fetch_allowed() is True
    now[0] = 61.0
    assert ctx.fetch_allowed() is False
    assert ctx.budget_snapshot()["fetch_allowed"] is False


def test_record_calls_do_not_spend_the_network_budget():
    ctx = _ctx(max_tool_calls=2)
    for _ in range(10):
        assert ctx.charge_record() is True
    assert ctx.tool_calls_used == 0
    assert ctx.budget_snapshot()["remaining_tool_calls"] == 2


def test_record_soft_cap_refuses_without_ending_the_run():
    ctx = _ctx(max_record_calls=2)
    assert ctx.charge_record() and ctx.charge_record()
    assert ctx.charge_record() is False
    assert ctx.record_calls_refused == 1


def test_network_cap_counts_only_calls_that_were_carried_out():
    ctx = _ctx(max_tool_calls=2)
    assert ctx.charge_call() and ctx.charge_call(is_fetch=True)
    assert ctx.charge_call() is False
    assert ctx.tool_calls_used == 2 and ctx.fetch_calls_used == 1
    assert ctx.tool_calls_refused == 1


def test_overlapping_tool_calls_are_counted_once_on_the_wall_clock():
    """동시 발사를 각자 더하면 벽시계 1초가 3초로 계상된다 — 우리가 인용하는 수치가 왜곡된다."""
    now = [0.0]
    ctx = _ctx(clock=lambda: now[0])
    spans = []
    for _ in range(3):
        spans.append(ctx.open_tool_call())
    now[0] = 1.0
    for span in spans:
        ctx.close_tool_call(span)
    assert ctx.tool_wait_s() == 1.0
    assert ctx.tool_time_serial_s() == 3.0
    assert ctx.parallel_speedup() == 3.0
    assert ctx.max_concurrent_tools == 3


def test_sequential_tool_calls_are_summed_normally():
    now = [0.0]
    ctx = _ctx(clock=lambda: now[0])
    for _ in range(2):
        span = ctx.open_tool_call()
        now[0] += 1.0
        ctx.close_tool_call(span)
    assert ctx.tool_wait_s() == 2.0
    assert ctx.parallel_speedup() == 1.0
    assert ctx.max_concurrent_tools == 1


def test_open_spans_are_closed_on_cancellation():
    now = [0.0]
    ctx = _ctx(clock=lambda: now[0])
    ctx.open_tool_call()
    now[0] = 2.0
    ctx.close_open_spans()
    now[0] = 10.0
    assert ctx.tool_wait_s() == 2.0


def test_search_cost_bills_successful_uncached_calls_only():
    calls = {
        "web": {"calls": 10, "cache_hits": 2, "failures": 1},
        "scholar": {"calls": 4, "cache_hits": 0, "failures": 0},
        "fetch": {"calls": 3, "cache_hits": 0, "failures": 0},
    }
    assert estimate_search_cost(calls) == pytest.approx(7 * 1.0 / 1000 + 4 * 0.3 / 1000)
    assert estimate_search_cost({"web": {"calls": 0, "cache_hits": 3, "failures": 0}}) == 0.0


def test_search_price_direction_is_not_inverted():
    from core.model_tools import SEARCH_PRICE_PER_1K_USD

    assert SEARCH_PRICE_PER_1K_USD["scholar"] < SEARCH_PRICE_PER_1K_USD["web"]
    scholar_doc = [t for t in ALL_TOOLS if t.name == "search_scholar"][0].description
    assert "2 QPS" in scholar_doc and "$0.3" in scholar_doc


# ── stub 경로 ────────────────────────────────────────────────────────────────
def test_core_runs_on_local_stubs_when_the_io_layer_is_absent():
    code = (
        "import sys, asyncio; sys.modules['tools']=None; sys.modules['tools.liner']=None;"
        "import core.model_tools as m;"
        "r = asyncio.run(m._io_search_web('q', max_results=2));"
        "print(m.TOOLS_STUBBED, m.resolve_source_mode(), r['ok'], len(r['data']))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("True mock True 2")


# ── 폴백 보고 ────────────────────────────────────────────────────────────────
def test_fallback_report_says_what_was_not_done():
    audit = Audit(TEXT)
    audit.record_claim(
        index=1,
        text="성인의 62%가 매일 카페인을 섭취한다",
        claim_type="statistical",
        auditable=True,
        prior=0.6,
    )
    audit.register_evidence(tool="search_web", query="q", url="https://a.test", title="자료")
    audit.update_verdict(
        claim_id="C1", axis=1, outcome="fail", evidence="지목된 자료를 찾지 못함", evidence_ids=[]
    )
    report = fallback_report(audit, audit.completion_report(), "timebox")
    assert "출처 못 찾음 1건" in report
    assert "[출처 못 찾음]" in report  # 영어 enum을 본문에 쓰지 않는다
    assert "부분 감사" in report


def test_fallback_report_for_non_auditable_input():
    audit = Audit("정부는 규제를 강화해야 한다.")
    audit.record_classification(
        input_kind="opinion_or_creative",
        lang="ko",
        auditable=False,
        sentence_count=1,
        rationale="가치명제뿐이다",
    )
    report = fallback_report(audit, audit.completion_report(), "non_auditable")
    assert report.startswith("감사하지 않았다")


def test_fallback_report_lists_the_counter_evidence_panel():
    audit = Audit(TEXT)
    audit.record_claim(
        index=1,
        text="성인의 62%가 매일 카페인을 섭취한다",
        claim_type="statistical",
        auditable=True,
        prior=0.6,
    )
    audit.register_evidence(
        tool="search_scholar", query="q", url="https://s.test", title="메타분석"
    )
    audit.update_verdict(
        claim_id="C1", axis=1, outcome="pass", evidence="자료가 있다", evidence_ids=["E1"]
    )
    audit.record_omission(claim_id="C1", evidence_id="E1", summary="추정치를 낮게 본다")
    report = fallback_report(audit, audit.completion_report(), "complete")
    assert "### 이 글에 대한 반박" in report
    assert "메타분석 — 추정치를 낮게 본다" in report


# ── 프롬프트 계약 가드 ───────────────────────────────────────────────────────
def test_prompt_pins_the_ui_recognised_section_titles():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "### 이 글에 대한 반박" in prompt
    assert "### 추천 수정안" in prompt
    for label in ("뒷받침 안 됨", "지나친 단정", "출처 못 찾음", "확인 불가", "의견·권고"):
        assert label in prompt


def test_prompt_states_the_rules_the_run_depends_on():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "evidence_id" in prompt  # 인용의 유일한 화폐
    assert "축1" in prompt and "축2" in prompt and "축3" in prompt
    assert "동시에 발사" in prompt  # 확증·반증 앵커링 방지
    assert "환각으로 단정하지 마라" in prompt  # 1순위 리스크
    assert "record_classification" in prompt
