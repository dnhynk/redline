"""server.py 회귀 + 화면 규율 정적 가드.

정적 가드가 있는 이유: 화면 규칙이 사람 기억이 아니라 테스트로 지켜져야 한다.
출처 못 찾음에 빨강이 다시 붙거나, 문장 목록 전체 재생성이 돌아오거나,
영어 enum 이 화면에 새면 여기서 깨진다.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import server
from server import PROFILES, Hub, create_app, load_jsonl

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"
CSS = (UI / "app.css").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")
RAW = (UI / "raw.html").read_text(encoding="utf-8")

RUN_REASONS = {"complete", "incomplete", "timebox", "max_turns", "non_auditable", "error"}
EVENT_KINDS = {"raw", "run_item", "audit", "status"}
CONTRACT_STRUCTURAL_KINDS = {"heading", "table_header", "code_fence", "divider"}
CONTRACT_NETWORK_TOOLS = {"search_web", "search_scholar", "fetch_source"}

MAIN_IDS = [
    "stage-rail", "phase-badge", "galley", "sentences", "unsupported-rate", "no-source-count",
    "coverage", "claim-count", "axis", "evidence-summary", "omissions-section", "omissions",
    "terminal-summary", "terminal-title", "terminal-reason", "terminal-coverage", "missing-actions",
    "final-report", "follow-run", "source-banner", "intake", "intake-process", "intake-collapsed",
    "intake-open", "run-form", "input-text", "run-button", "form-error", "connection-label",
]
RAW_IDS = [
    "raw-events", "state-claims", "tool-count", "elapsed", "timebox", "axis-track",
    "pause-scroll", "connection-label",
]


# --------------------------------------------------------------------------
# 도우미
# --------------------------------------------------------------------------


def sample_events() -> list[dict]:
    return [
        {"kind": "raw", "t": 100.0, "payload": {"type": "response.created", "response": {"model": "m"}}},
        {"kind": "audit", "t": 100.5, "payload": {"sentences": ["가.", "나."], "claims": [], "source_mode": "mock"}},
        {"kind": "status", "t": 101.0, "payload": {"phase": "종결", "elapsed_s": 1.0, "claims": 0,
                                                   "tool_calls": 0, "axis": 1, "done": True,
                                                   "reason": "complete", "final_report": "## 끝"}},
    ]


def list_source(events: list[dict], gate: asyncio.Event | None = None):
    async def source(_text: str) -> AsyncIterator[dict]:
        for event in events:
            yield event
        if gate is not None:
            await gate.wait()

    return source


def strip_tags(html: str) -> str:
    without_script = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html)
    return re.sub(r"<[^>]+>", " ", without_script)


# --------------------------------------------------------------------------
# 계약 상수
# --------------------------------------------------------------------------


def test_profiles_are_the_approved_constants():
    assert PROFILES == {"demo": 110.0, "surprise": 90.0}


def test_create_app_defaults_to_surprise():
    app = create_app(list_source([]))
    assert app.state.hub.timebox_s == 90.0
    assert app.state.hub.client_queue_max == 2048


# --------------------------------------------------------------------------
# /run
# --------------------------------------------------------------------------


def test_run_returns_run_id():
    with TestClient(create_app(list_source(sample_events()))) as client:
        res = client.post("/run", json={"text": "감사할 문장입니다."})
        assert res.status_code == 200
        assert re.fullmatch(r"[0-9a-f]{12}", res.json()["run_id"])


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_run_rejects_blank_text(text):
    with TestClient(create_app(list_source([]))) as client:
        assert client.post("/run", json={"text": text}).status_code == 422


def test_run_is_409_while_running():
    gate = asyncio.Event()
    with TestClient(create_app(list_source(sample_events(), gate))) as client:
        assert client.post("/run", json={"text": "첫 번째"}).status_code == 200
        second = client.post("/run", json={"text": "두 번째"})
        assert second.status_code == 409
        assert "진행 중" in second.json()["detail"]


def test_source_failure_ends_with_a_done_status():
    async def broken(_text: str) -> AsyncIterator[dict]:
        yield {"kind": "raw", "t": 1.0, "payload": {"type": "response.created"}}
        raise RuntimeError("모델이 응답하지 않았다")

    with TestClient(create_app(broken)) as client:
        client.post("/run", json={"text": "감사할 문장"})
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_json()  # config
            events = [ws.receive_json() for _ in range(2)]
    assert events[-1]["kind"] == "status"
    assert events[-1]["payload"]["done"] is True
    assert events[-1]["payload"]["reason"] == "error"
    assert "RuntimeError" in events[-1]["payload"]["error"]


# --------------------------------------------------------------------------
# WS 와이어
# --------------------------------------------------------------------------


def test_config_arrives_first_and_carries_timebox():
    app = create_app(list_source([]), timebox_s=PROFILES["demo"], mock=True)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        config = ws.receive_json()
    assert config["kind"] == "config"
    assert config["payload"]["timebox_s"] == 110.0
    assert config["payload"]["mock"] is True
    # 재생 dedup 비대상 — seq 도 run 도 없다
    assert "seq" not in config and "run" not in config


def test_seq_is_renumbered_and_run_is_added_and_t_is_preserved():
    with TestClient(create_app(list_source(sample_events()))) as client:
        run_id = client.post("/run", json={"text": "문장"}).json()["run_id"]
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_json()
            events = [ws.receive_json() for _ in range(3)]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert {e["run"] for e in events} == {run_id}
    assert [e["t"] for e in events] == [100.0, 100.5, 101.0]


def test_late_join_replays_history_from_the_first_event():
    with TestClient(create_app(list_source(sample_events()))) as client:
        client.post("/run", json={"text": "문장"})
        time.sleep(0.05)
        with client.websocket_connect("/ws/events") as ws:
            assert ws.receive_json()["kind"] == "config"
            replay = [ws.receive_json() for _ in range(3)]
    assert [e["seq"] for e in replay] == [1, 2, 3]


def test_reconnect_with_last_seq_skips_what_was_already_seen():
    with TestClient(create_app(list_source(sample_events()))) as client:
        run_id = client.post("/run", json={"text": "문장"}).json()["run_id"]
        time.sleep(0.05)
        with client.websocket_connect(f"/ws/events?last_seq=2&run={run_id}") as ws:
            ws.receive_json()
            rest = [ws.receive_json()]
    assert [e["seq"] for e in rest] == [3]


def test_reconnect_from_a_different_run_gets_the_whole_history():
    with TestClient(create_app(list_source(sample_events()))) as client:
        client.post("/run", json={"text": "문장"})
        time.sleep(0.05)
        with client.websocket_connect("/ws/events?last_seq=2&run=stale") as ws:
            ws.receive_json()
            replay = [ws.receive_json() for _ in range(3)]
    assert [e["seq"] for e in replay] == [1, 2, 3]


def test_a_new_run_resets_the_history_and_the_counter():
    with TestClient(create_app(list_source(sample_events()))) as client:
        first = client.post("/run", json={"text": "첫"}).json()["run_id"]
        time.sleep(0.05)
        second = client.post("/run", json={"text": "둘"}).json()["run_id"]
        time.sleep(0.05)
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_json()
            replay = [ws.receive_json() for _ in range(3)]
    assert first != second
    assert [e["seq"] for e in replay] == [1, 2, 3]
    assert {e["run"] for e in replay} == {second}


# --------------------------------------------------------------------------
# F15 — 느린 클라이언트는 자기만 손해
# --------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.received: list[dict] = []
        self.stamps: list[float] = []
        self.closed: int | None = None

    async def send_json(self, event: dict) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.received.append(event)
        self.stamps.append(time.perf_counter())

    async def close(self, code: int = 1000) -> None:
        self.closed = code


@pytest.mark.asyncio
async def test_a_slow_client_does_not_delay_a_fast_one():
    hub = Hub(timebox_s=90.0, client_queue_max=2048, mock=False)
    await hub.start_run("r1")
    slow, fast = FakeSocket(delay=0.05), FakeSocket()
    slow_client = await hub.register(slow, 0, None)
    fast_client = await hub.register(fast, 0, None)
    pumps = [asyncio.create_task(hub.pump(slow_client)), asyncio.create_task(hub.pump(fast_client))]

    started = time.perf_counter()
    for n in range(20):
        await hub.publish({"kind": "raw", "t": float(n), "payload": {"n": n}})
    published = time.perf_counter() - started

    await asyncio.sleep(0.2)
    for task in pumps:
        task.cancel()

    assert published < 0.05, f"브로드캐스트가 느린 클라이언트를 기다렸다 ({published:.3f}s)"
    assert len(fast.received) == 21  # config + 20
    fast_span = fast.stamps[-1] - fast.stamps[0]
    assert fast_span < 0.1, f"빠른 클라이언트 전달이 밀렸다 ({fast_span:.3f}s)"
    assert len(slow.received) < len(fast.received)


@pytest.mark.asyncio
async def test_a_client_that_cannot_keep_up_is_the_only_one_dropped():
    hub = Hub(timebox_s=90.0, client_queue_max=4, mock=False)
    await hub.start_run("r1")
    stuck, healthy = FakeSocket(delay=5.0), FakeSocket()
    stuck_client = await hub.register(stuck, 0, None)
    healthy_client = await hub.register(healthy, 0, None)
    pumps = [asyncio.create_task(hub.pump(stuck_client)), asyncio.create_task(hub.pump(healthy_client))]
    for n in range(40):
        await hub.publish({"kind": "raw", "t": float(n), "payload": {"n": n}})
        await asyncio.sleep(0)  # 실제 루프처럼 송신 태스크에 차례를 준다
    await asyncio.sleep(0.05)
    for task in pumps:
        task.cancel()
    assert stuck_client.dropped is True
    assert healthy_client.dropped is False
    assert len(healthy.received) == 41


@pytest.mark.asyncio
async def test_registration_and_replay_share_one_lock():
    hub = Hub(timebox_s=90.0, client_queue_max=2048, mock=False)
    await hub.start_run("r1")
    for n in range(5):
        await hub.publish({"kind": "raw", "t": float(n), "payload": {"n": n}})
    sock = FakeSocket()
    client = await hub.register(sock, 0, None)
    await hub.publish({"kind": "raw", "t": 99.0, "payload": {"n": 99}})
    pump = asyncio.create_task(hub.pump(client))
    await asyncio.sleep(0.05)
    pump.cancel()
    seqs = [e.get("seq") for e in sock.received if e.get("kind") == "raw"]
    assert seqs == [1, 2, 3, 4, 5, 6], "경계 이벤트가 유실됐다"


# --------------------------------------------------------------------------
# 정적 자산
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/raw", "/ui/app.css", "/ui/app.js"])
def test_static_responses_are_no_store(path):
    with TestClient(create_app(list_source([]))) as client:
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store, must-revalidate"


def test_asset_urls_carry_a_version_query():
    with TestClient(create_app(list_source([]))) as client:
        body = client.get("/").text
    assert "__V__" not in body
    assert re.search(r'app\.css\?v=[0-9a-f]{10}"', body)
    assert re.search(r'app\.js\?v=[0-9a-f]{10}"', body)


@pytest.mark.parametrize("asset", ["..%2Fserver.py", "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fserver.py"])
def test_asset_path_escape_is_blocked(asset):
    with TestClient(create_app(list_source([]))) as client:
        assert client.get(f"/ui/{asset}").status_code == 404


def test_font_files_are_served_from_the_repo():
    with TestClient(create_app(list_source([]))) as client:
        res = client.get("/ui/fonts/PretendardVariable.subset.woff2")
    assert res.status_code == 200
    assert res.headers["content-type"] == "font/woff2"


# --------------------------------------------------------------------------
# 요소 id 계약
# --------------------------------------------------------------------------


@pytest.mark.parametrize("element_id", MAIN_IDS)
def test_main_screen_keeps_its_element_ids(element_id):
    assert f'id="{element_id}"' in INDEX


@pytest.mark.parametrize("element_id", RAW_IDS)
def test_projector_keeps_its_element_ids(element_id):
    assert f'id="{element_id}"' in RAW


# --------------------------------------------------------------------------
# 화면 규율 — 정적 가드
# --------------------------------------------------------------------------


def test_no_remote_font_or_cdn_request():
    """행사장 네트워크를 믿지 않는다 — 런타임 외부 요청이 하나도 없어야 한다."""
    for url in re.findall(r"url\(([^)]+)\)", CSS):
        url = url.strip("\"'")
        if url.startswith("data:"):
            continue  # 인라인 — 네트워크를 타지 않는다
        assert not re.match(r"(https?:)?//", url), f"원격 자산: {url}"
    for html in (INDEX, RAW):
        for attr in re.findall(r'(?:href|src)="([^"]+)"', html):
            assert not re.match(r"(https?:)?//", attr), f"외부 요청: {attr}"


def test_every_declared_font_file_exists():
    faces = re.findall(r"@font-face\s*\{[^}]*\}", CSS)
    assert len(faces) == 5, f"@font-face 5건이어야 한다 (현재 {len(faces)})"
    for face in faces:
        path = re.search(r'url\("([^"]+)"\)', face).group(1)
        assert (ROOT / path.lstrip("/")).is_file(), f"폰트 파일 없음: {path}"


def test_no_english_enum_or_internal_vocab_on_screen():
    banned = [
        "unsupported", "overstated", "no_source", "undecidable", "non_auditable", "supported",
        "미지지", "환각", "결박", "실수신", "감사 제외", "판정 불가", "출처 없음", "지지됨", "미지지율",
    ]
    for name, html in (("index.html", INDEX), ("raw.html", RAW)):
        text = strip_tags(html)
        for word in banned:
            assert word not in text, f"{name} 화면 문구에 {word}"


def test_screen_labels_match_the_dictionary():
    labels = dict(re.findall(r"^\s*(\w+): \"([^\"]+)\",?$", JS.split("VERDICT_LABEL = {")[1].split("}")[0], re.M))
    assert labels == {
        "pending": "확인 중",
        "supported": "뒷받침됨",
        "unsupported": "뒷받침 안 됨",
        "overstated": "지나친 단정",
        "no_source": "출처 못 찾음",
        "undecidable": "확인 불가",
        "non_auditable": "의견·권고",
    }


def _rules(css: str) -> list[tuple[str, str]]:
    body = re.sub(r"@media[^{]*\{", "", css)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", body)


def test_no_red_where_a_source_was_merely_not_found():
    red = ("#d0021b", "#a3000f", "--v-unsupported", "--p-red")
    for selector, block in _rules(CSS):
        if "no_source" not in selector and "undecidable" not in selector:
            continue
        for token in red:
            assert token not in block.lower(), f"확인 실패에 빨강: {selector.strip()}"


def test_the_projector_meter_never_borrows_red_for_a_missing_source():
    block = CSS.split(".state-card[data-mark=\"no_source\"]")[1].split("}")[0]
    assert "--p-red" not in block


def test_section_accents_do_not_borrow_verdict_colours():
    assert "--rebut-line: #1e3a5f" in CSS
    assert "--fix-line: #12594e" in CSS
    rebut = CSS.split(".rebut {")[1].split("}")[0]
    assert "--v-unsupported" not in rebut, "반박 섹션이 판정 빨강을 빌려 썼다"


def test_every_motion_stays_inside_the_budget():
    longest = 0.0
    for prop in re.findall(r"(?:animation|transition)(?:-duration)?\s*:\s*([^;]+);", CSS):
        for value, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s)\b", prop):
            seconds = float(value) / (1000 if unit == "ms" else 1)
            if seconds > 0.5:  # 0.001ms 강제 차단 규칙·큰 지연은 없다
                pytest.fail(f"모션 예산 초과: {prop.strip()}")
            longest = max(longest, seconds)
    assert longest <= 0.3, f"최장 모션 {longest}s"


def test_nothing_loops_forever():
    assert "infinite" not in CSS, "무한 반복 애니메이션은 끝나지 않는다"


def test_reduced_motion_reaches_pseudo_elements():
    block = CSS.split("@media (prefers-reduced-motion: reduce)")[1].split("}")[0]
    assert "*::before" in block and "*::after" in block
    assert "force-reduced *::before" in CSS and "force-reduced *::after" in CSS


def test_stage_classes_only_touch_paint_properties():
    for selector, block in _rules(CSS):
        if "is-receded" in selector or "is-focus" in selector:
            assert "display" not in block, f"단계 클래스가 레이아웃을 건드린다: {selector.strip()}"
            for prop in re.findall(r"([a-z-]+)\s*:", block):
                assert prop in {"opacity", "transform", "box-shadow", "transition"}, prop


def test_live_lists_are_never_rebuilt_wholesale():
    for host in ("#sentences", "#state-claims", "#omissions"):
        assert not re.search(rf'\$\("{host}"\)\.innerHTML\s*=', JS), f"{host} 전체 재생성"
    assert "ensureRows" in JS and "whenIdle" in JS


def test_one_shot_animation_classes_are_removed_when_they_finish():
    assert 'node.classList.remove(cls)' in JS
    assert '"animationend"' in JS


def test_structural_kinds_match_the_contract():
    block = JS.split("STRUCTURAL_KINDS = {")[1].split("}")[0]
    assert set(re.findall(r"(\w+):", block)) == CONTRACT_STRUCTURAL_KINDS


def test_the_tool_gauge_counts_network_tools_only():
    block = JS.split("NETWORK_TOOLS = {")[1].split("}")[0]
    assert set(re.findall(r"(\w+):", block)) == CONTRACT_NETWORK_TOOLS
    counter = [line for line in JS.splitlines() if "state.netCalls++" in line]
    assert counter and all("NETWORK_TOOLS" in line for line in counter)


def test_the_screen_does_not_explain_its_own_design_decisions():
    for phrase in ("본문을 가리지 않", "여백에 붙습니다", "그래서 빨강", "설계", "은유"):
        assert phrase not in strip_tags(INDEX), phrase
        assert phrase not in strip_tags(RAW), phrase


def test_terminal_wording_covers_every_reason():
    block = JS.split("var TERMINAL = {")[1].split("\n  };")[0]
    assert set(re.findall(r"^\s{4}(\w+):", block, re.M)) == RUN_REASONS


# --------------------------------------------------------------------------
# 합성 이벤트 픽스처
# --------------------------------------------------------------------------

FIXTURES = sorted((UI / "fixtures").glob("*.jsonl"))


def test_fixtures_exist():
    assert {p.stem for p in FIXTURES} == {"complete", "timebox", "non_auditable", "error", "structured"}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_the_wire_contract(path):
    events = load_jsonl(path)
    assert events, "빈 파일"
    for event in events:
        assert event["kind"] in EVENT_KINDS
        assert isinstance(event["t"], (int, float))
        assert isinstance(event["payload"], dict)
    done = [e for e in events if e["kind"] == "status" and e["payload"].get("done")]
    assert len(done) == 1, "status(done=true) 는 정확히 1회"
    assert done[0] is events[-1]
    assert done[0]["payload"]["reason"] in RUN_REASONS


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_is_openly_synthetic(path):
    """합성 데이터를 실 결과로 위장하지 않는다 — 재생 중 화면이 배너를 띄운다."""
    for event in load_jsonl(path):
        if event["kind"] in ("audit", "status"):
            assert event["payload"].get("source_mode") == "mock"


def test_the_complete_fixture_walks_the_whole_screen():
    events = load_jsonl(UI / "fixtures" / "complete.jsonl")
    final = events[-1]["payload"]
    audit = final["audit"]
    assert final["reason"] == "complete"
    assert len(audit["claims"]) == 5
    assert {c["verdict"] for c in audit["claims"]} >= {"unsupported", "overstated", "no_source"}
    assert len(audit["omissions"]) >= 2
    kinds = {e["kind"] for e in events}
    assert kinds == EVENT_KINDS
    types = {e["payload"].get("type") for e in events if e["kind"] == "raw"}
    for required in (
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_text.delta",
        "response.completed",
    ):
        assert required in types, required
    names = {e["payload"].get("name") for e in events if e["kind"] == "run_item"}
    assert {"tool_called", "tool_output"} <= names


def test_structural_fixture_carries_every_sentence_kind():
    events = load_jsonl(UI / "fixtures" / "structured.jsonl")
    audit = events[-1]["payload"]["audit"]
    assert set(audit["sentence_kinds"]) >= CONTRACT_STRUCTURAL_KINDS
    assert len(audit["sentences"]) == len(audit["sentence_kinds"])


def test_replaying_a_fixture_reaches_the_terminal_status():
    events = load_jsonl(UI / "fixtures" / "non_auditable.jsonl")
    app = create_app(server.jsonl_source(UI / "fixtures" / "non_auditable.jsonl", speed=1000), mock=True)
    with TestClient(app) as client:
        client.post("/run", json={"text": "산책을 다녀오는 편이 좋다고 생각합니다."})
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_json()
            received = [ws.receive_json() for _ in range(len(events))]
    assert received[-1]["payload"]["reason"] == "non_auditable"
    assert received[-1]["seq"] == len(events)
