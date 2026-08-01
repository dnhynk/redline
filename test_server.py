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
    "final-report", "terminal-note", "error-detail", "follow-run", "source-banner", "intake",
    "intake-process",
    "intake-collapsed",
    "intake-open", "run-form", "input-text", "run-button", "form-error", "connection-label",
]
RAW_IDS = [
    "raw-events", "state-claims", "tool-count", "tool-max", "elapsed", "timebox", "axis-track",
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
    assert {p.stem for p in FIXTURES} == {
        "complete", "timebox", "incomplete", "non_auditable", "error", "no_start", "structured",
    }


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


# --------------------------------------------------------------------------
# 검색 방향 — 확증과 반증이 나란히 뜨는 순간이 보여야 한다
# --------------------------------------------------------------------------

STANCES = {"support", "challenge"}


def test_the_two_search_directions_do_not_share_a_colour():
    support = re.search(r"--p-support:\s*(#[0-9a-f]{6})", CSS).group(1)
    challenge = re.search(r"--p-challenge:\s*(#[0-9a-f]{6})", CSS).group(1)
    assert support != challenge, "같은 색이면 두 방향이 안 보인다"
    for token in ("#ff8078", "#ff8c84", "#d0021b", "#a3000f"):
        assert challenge != token, "반증 검색이 판정 빨강을 빌려 썼다"
    assert '.ev[data-tool="search"][data-stance="challenge"]' in CSS
    assert '.ev[data-tool="search"][data-stance="support"]' in CSS


def test_direction_is_never_carried_by_colour_alone():
    assert 'el("span", "ev-stance", STANCE_LABEL[info.stance])' in JS
    assert 'class="stance-legend"' in RAW
    text = strip_tags(RAW)
    assert "뒷받침 검색" in text and "반박 검색" in text


def test_stance_labels_use_the_screen_vocabulary():
    labels = JS.split("STANCE_LABEL = {")[1].split("}")[0]
    assert '"뒷받침"' in labels and '"반박"' in labels
    searches = JS.split("STANCE_SEARCH = {")[1].split("}")[0]
    assert '"뒷받침 검색"' in searches and '"반박 검색"' in searches
    for enum in ("확증", "반증"):
        assert enum not in strip_tags(RAW), f"화면에 내부 어휘 {enum}"


def test_stance_is_read_from_a_half_arrived_argument_blob():
    """조각난 인자 위에서도, 파이썬 repr 봉투에서도 방향이 읽혀야 한다."""
    pattern = re.search(r"var found = /(.+?)/\.exec", JS).group(1)
    probe = re.compile(pattern)
    assert probe.search('{"query":"x","stance":"challenge"')
    assert probe.search("{'ok': True, 'stance': 'support'}")
    assert not probe.search('{"query":"stance of the paper"}')


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_search_call_declares_a_direction(path):
    seen = set()
    for event in load_jsonl(path):
        payload = event["payload"]
        if event["kind"] != "raw" or payload.get("type") != "response.function_call_arguments.done":
            continue
        args = json.loads(payload["arguments"])
        if "query" not in args:
            continue
        assert args.get("stance") in STANCES, f"방향 없는 검색: {args.get('query')}"
        seen.add(args["stance"])
    if seen:
        assert seen == STANCES, f"한 방향만 발사됐다: {seen}"


def test_the_ledger_remembers_which_direction_found_each_source():
    audit = load_jsonl(UI / "fixtures" / "complete.jsonl")[-1]["payload"]["audit"]
    ledger = {r["id"]: r for r in audit["evidence"]}
    assert ledger, "인용된 원장이 비었다"
    for record in ledger.values():
        assert record.get("stance") in STANCES
    found = {ledger[o["evidence_id"]]["stance"] for o in audit["omissions"]}
    assert "support" in found, "확증 검색에서 나온 반박 자료가 한 건은 있어야 그 경우가 화면에 뜬다"


def test_the_screen_says_which_direction_found_a_rebuttal():
    assert 'el("span", "stance-tag", STANCE_SEARCH[stance])' in JS
    assert ".stance-tag" in CSS
    assert 'chip.stance) node.dataset.stance' in JS


def test_stance_display_is_defensive_when_the_ledger_has_none():
    """원장에 방향이 없으면 표시를 생략한다 — 없는 것을 그리지 않는다."""
    assert "STANCE_SEARCH[stance]" in JS
    assert 'var stance = om.stance || record.stance || "";' in JS
    assert 'out.stance = declared || state.stance[index] || ""' in JS


# --------------------------------------------------------------------------
# 툴 예산 분모
# --------------------------------------------------------------------------


def test_the_tool_ceiling_is_not_written_into_the_page():
    assert 'id="tool-max"' in RAW
    assert not re.search(r'id="tool-count"[^<]*</b>\s*/\s*30', RAW), "분모가 하드코딩돼 있다"
    assert "status.max_tool_calls" in JS
    assert "DEFAULT_TOOL_MAX = 30" in JS


def test_the_tool_ceiling_falls_back_when_the_run_does_not_say():
    block = JS.split("function toolMaxOf(")[1].split("\n  }")[0]
    assert "max_tool_calls" in block and "tool_calls_max" in block
    assert "state.toolMax || DEFAULT_TOOL_MAX" in JS


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_status_payloads_carry_the_tool_ceiling(path):
    for event in load_jsonl(path):
        if event["kind"] == "status":
            assert event["payload"].get("max_tool_calls") == 30


# --------------------------------------------------------------------------
# 시간이 없어 못 한 것과, 하기로 하고 안 한 것
# --------------------------------------------------------------------------


def test_incomplete_does_not_read_like_a_timebox_cut():
    block = JS.split("var TERMINAL = {")[1].split("\n  };")[0]
    notes = dict(re.findall(r"(\w+): \{[^}]*note: \"([^\"]*)\"", block))
    reasons = dict(re.findall(r"(\w+): \{[^}]*reason: \"([^\"]*)\"", block))
    assert notes["timebox"] and notes["incomplete"] and notes["max_turns"]
    assert len({notes["timebox"], notes["incomplete"], notes["max_turns"]}) == 3
    assert reasons["timebox"] != reasons["incomplete"]
    assert 'map.note ? " · " + map.note : ""' in JS, "배지가 사유를 달고 나오지 않는다"


def test_the_incomplete_fixture_ends_for_its_own_reason():
    incomplete = load_jsonl(UI / "fixtures" / "incomplete.jsonl")[-1]["payload"]
    timebox = load_jsonl(UI / "fixtures" / "timebox.jsonl")[-1]["payload"]
    assert incomplete["reason"] == "incomplete" and timebox["reason"] == "timebox"
    assert incomplete["axis3_done"] < incomplete["axis3_expected"]


def test_missing_actions_are_rewritten_into_screen_words():
    block = JS.split("var ACTION_TERMS = [")[1].split("\n  ];")[0]
    for internal in ("축", "미확정 클레임"):
        assert internal in block, f"{internal} 를 옮기는 규칙이 없다"
    for shown in ("논문·웹 출처 확인", "내용 확인", "반박 찾기", "판정이 남은 클레임"):
        assert shown in block
    assert "humanizeAction" in JS and "missingActions(status)" in JS
    # 바꿔 넣은 말 뒤의 조사가 어긋나면 안 된다 ("반박 찾기을")
    assert '[/반박 찾기을/g, "반박 찾기를"]' in block


def test_the_axis3_shortfall_can_stand_in_for_a_missing_reason():
    block = JS.split("function missingActions(")[1].split("\n  }")[0]
    assert "axis3_done" in block and "axis3_expected" in block
    assert "반박 찾기를 " in block


# --------------------------------------------------------------------------
# 시작조차 못 한 런을 완주한 감사처럼 그리지 않는다
# --------------------------------------------------------------------------


def test_a_crashed_run_shows_a_fixed_sentence_not_the_providers_words():
    assert 'if (status.reason === "error") reason = "감사를 시작하지 못했습니다.";' in JS
    assert "status.error" not in JS.split("setText($(\"#terminal-reason\"), reason);")[0].split(
        "var reason = map.reason;"
    )[-1], "제공자 원문이 사유 자리에 그대로 들어간다"
    # 원문은 접힌 상세 안에만 산다
    detail = JS.split("function paintErrorDetail(")[1].split("\n  }")[0]
    assert 'el("summary", null, "기술 상세")' in detail
    assert 'id="error-detail"' in INDEX and "<details" in INDEX


def test_a_crashed_run_publishes_no_audit_numbers():
    assert "function crashed()" in JS
    block = JS.split("if (crashed()) {")[1].split("} else {")[0]
    assert 'setDash($("#unsupported-rate"))' in block
    assert 'setDash($("#coverage"))' in block
    assert "감사가 중단돼 집계를 내지 않았습니다" in block
    # 중단 런의 최종 보고(수치 줄)는 싣지 않는다
    assert 'status.reason === "error" ? "" : status.final_report' in JS


def test_the_no_start_fixture_reproduces_the_crash_before_anything_ran():
    events = load_jsonl(UI / "fixtures" / "no_start.jsonl")
    assert len(events) == 1, "시작도 못 한 런은 종결 이벤트 하나뿐이다"
    payload = events[0]["payload"]
    assert payload["reason"] == "error" and payload["done"] is True
    assert payload["error"], "원문 오류가 봉투에 실려 있어야 화면 처리가 검증된다"
    assert payload["audit"]["claims"] == [] and payload["audit"]["evidence_total"] == 0


# --------------------------------------------------------------------------
# 완주 문구가 데이터를 앞서지 않는다
# --------------------------------------------------------------------------


def test_the_confident_zero_rebuttal_sentence_needs_evidence_behind_it():
    block = JS.split("function emptyRebuttalCopy(")[1].split("\n  }")[0]
    assert "challengeSearches(status)" in block and "evidenceReceived(audit)" in block
    confident = "반박까지 찾아봤지만 나오지 않았습니다"
    assert confident in block
    head, tail = block.split(confident)
    assert "if (!fired)" in head and "if (!received)" in head, "근거 확인 없이 단정한다"
    assert "0건은 탐색 결과가 아닙니다" in head, "근거가 없을 때의 중립 문구가 없다"


def test_the_challenge_count_is_read_from_the_terminal_envelope():
    block = JS.split("function challengeSearches(")[1].split("\n  }")[0]
    for key in ("search_counts", "challenge_queries"):
        assert key in block


def test_fixtures_carry_the_challenge_accounting_the_screen_reads():
    for path in FIXTURES:
        terminal = load_jsonl(path)[-1]["payload"]
        completion = terminal.get("completion") or {}
        assert "search_counts" in completion, f"{path.stem}: 반증 검색 회계 없음"
        assert set(completion["search_counts"]) == {"support", "challenge"}
        assert isinstance(terminal.get("challenge_queries"), int)


# --------------------------------------------------------------------------
# 종결 봉투의 audit 이 정본이다
# --------------------------------------------------------------------------


def test_the_terminal_audit_overwrites_the_stream_snapshot():
    block = JS.split('} else if (event.kind === "status") {')[1].split("\n    }")[0]
    assert "if (state.status.audit) state.audit = state.status.audit;" in block


def test_the_fixture_terminal_carries_the_full_audit():
    for path in FIXTURES:
        audit = load_jsonl(path)[-1]["payload"]["audit"]
        assert "input_text" in audit, f"{path.stem}: 종결 audit 이 정본(기본형 전체)이 아니다"


# --------------------------------------------------------------------------
# 픽스처가 현재 빌드 스키마다
# --------------------------------------------------------------------------

LIVE_CAPTURE = UI / "mock_events.jsonl"


def _terminal(path: Path) -> dict:
    for event in reversed(load_jsonl(path)):
        if event["kind"] == "status" and event["payload"].get("done"):
            return event["payload"]
    raise AssertionError(f"{path} 에 종결 이벤트가 없다")


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_the_shape_the_build_emits(path):
    """폴백이 제품이 더 이상 내지 않는 스키마로 검증되면 안 된다."""
    build = _terminal(LIVE_CAPTURE)
    mine = _terminal(path)
    assert set(mine) == set(build), "종결 봉투 키가 빌드와 다르다"
    assert set(mine["audit"]) == set(build["audit"]), "audit 키가 빌드와 다르다"
    if mine["audit"]["claims"]:
        assert set(mine["audit"]["claims"][0]) == set(build["audit"]["claims"][0])
        assert "base_confidence" in mine["audit"]["claims"][0]
        assert "prior" not in mine["audit"]["claims"][0]


def test_the_mock_fallback_plays_the_current_build():
    assert server.DEFAULT_FIXTURE == LIVE_CAPTURE


# --------------------------------------------------------------------------
# 릴레이 대역폭 — 히스토리 상한과 그 사실의 공개
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_stops_growing_without_a_bound():
    hub = Hub(timebox_s=90.0, client_queue_max=2048, mock=False, history_max=50)
    await hub.start_run("r1")
    for n in range(200):
        await hub.publish({"kind": "audit", "t": float(n), "payload": {"n": n}})
    assert len(hub.history) == 50
    assert hub.history_dropped == 150
    assert hub.history[0]["seq"] == 151


@pytest.mark.asyncio
async def test_a_late_joiner_is_told_what_it_missed():
    hub = Hub(timebox_s=90.0, client_queue_max=2048, mock=False, history_max=10)
    await hub.start_run("r1")
    for n in range(40):
        await hub.publish({"kind": "audit", "t": float(n), "payload": {"n": n}})
    config = hub.config_event()["payload"]
    assert config["history_dropped"] == 30
    assert config["history_from"] == 31, "어디서부터 보고 있는지 화면이 알아야 한다"


@pytest.mark.asyncio
async def test_a_fresh_run_forgets_the_previous_truncation():
    hub = Hub(timebox_s=90.0, client_queue_max=2048, mock=False, history_max=5)
    await hub.start_run("r1")
    for n in range(20):
        await hub.publish({"kind": "audit", "t": float(n), "payload": {"n": n}})
    await hub.start_run("r2")
    config = hub.config_event()["payload"]
    assert config["history_dropped"] == 0 and config["history_from"] == 1


def test_the_screen_says_the_replay_is_missing_its_head():
    block = JS.split("function noteHistoryGap(")[1].split("\n  }")[0]
    assert "history_dropped" in block and "history_from" in block
    assert "재생되지 않았습니다" in block
    assert ".ev-gap" in CSS


def test_the_default_history_bound_is_generous_enough_for_a_run():
    assert re.search(r"history_max: int = (\d+)", server.__doc__ or "") is None
    bound = int(re.search(r"history_max: int = (\d+)", (ROOT / "server.py").read_text(encoding="utf-8")).group(1))
    assert bound >= len(load_jsonl(LIVE_CAPTURE)), "실 런 한 번이 상한에 걸리면 안 된다"


# --------------------------------------------------------------------------
# 두 번째 런의 경과 시간
# --------------------------------------------------------------------------


def test_the_second_run_does_not_inherit_the_first_runs_clock():
    block = JS.split("function resetRun(")[1].split("\n  }")[0]
    assert "state.elapsed = 0;" in block
    assert "state.elapsedWall = 0;" in block, "직전 런 종료 이후 흐른 시간이 그대로 표시된다"
