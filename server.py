"""REDLINE 이벤트 릴레이 서버.

이벤트를 요약하거나 걸러내지 않는다. 하는 일은 넷뿐이다.

* seq 를 자기 카운터로 다시 매기고 run(run_id) 을 붙인다
* 히스토리를 들고 있다가 늦게 붙은 클라이언트에게 재생한다
* WS 접속 직후 config 이벤트를 1회 보낸다 (timebox 표시용)
* 정적 자산을 no-store 로 서빙한다

브로드캐스트는 락을 쥔 채로 네트워크 I/O 를 기다리지 않는다. 클라이언트마다
상한 있는 큐와 송신 태스크가 따로 있고, 큐가 차면 그 클라이언트만 끊긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

# 타임박스 프로파일. 사용자 소유 상수 — 이 두 값 밖의 값을 만들지 않는다.
PROFILES: dict[str, float] = {"demo": 110.0, "surprise": 90.0}

# 프로파일별 클레임 상한. 짧은 박스에서 상한 12는 여유가 1~6초라 위험하고,
# 상한 9는 누락 산출물이 같으면서 10초 빠르다 — 측정으로 정한 값.
CLAIM_CAPS: dict[str, int] = {"demo": 12, "surprise": 9}

# 감사기가 실제로 읽을 수 있는 최대치. core 는 문장 80개까지만 모델에 보이고
# (HOST_SENTENCES_MAX) 문장마다 160자에서 자른다 (HOST_SENTENCE_CHARS).
# 그 곱이 여기 값이다. 이 너머의 글자는 봉투에 실려 화면에 줄로 오르지만
# 판정 대상이 될 수 없다 — 감사하지 않을 것을 받아 두고 감사하는 척하지 않는다.
# 이 유도가 깨지면 test_server.py 의 대조 테스트가 먼저 운다.
TEXT_MAX_CHARS = 80 * 160

# 본문 바이트 상한. 위 글자 수가 한글이면 UTF-8 3바이트, JSON \uXXXX 이스케이프면
# 6바이트까지 늘어난다. 넉넉히 8배 + 여유를 두고, 그보다 큰 본문은 읽지도 않는다.
BODY_MAX_BYTES = TEXT_MAX_CHARS * 8 + 4096

UI_DIR = Path(__file__).resolve().parent / "ui"
FIXTURE_DIR = UI_DIR / "fixtures"
DEFAULT_FIXTURE = UI_DIR / "mock_events.jsonl"

NO_STORE = "no-store, must-revalidate"
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".woff2": "font/woff2",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# source(text) -> RelayEvent 를 흘리는 비동기 이터레이터
EventSource = Callable[[str], AsyncIterator[dict]]


class RunRequest(BaseModel):
    text: str = ""


# --------------------------------------------------------------------------
# 브로드캐스트 허브
# --------------------------------------------------------------------------


class _Client:
    """WS 하나. 자기 큐와 자기 송신 태스크를 갖는다."""

    __slots__ = ("ws", "queue", "backlog", "dropped", "wake", "sent")

    def __init__(self, ws: WebSocket, queue_max: int) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_max)
        self.backlog: list[dict] = []
        self.dropped = False
        self.wake = asyncio.Event()
        self.sent = 0


class Hub:
    """이벤트 재번호·히스토리 재생·팬아웃."""

    def __init__(
        self, *, timebox_s: float, client_queue_max: int, mock: bool, history_max: int = 4000
    ) -> None:
        self.timebox_s = float(timebox_s)
        self.client_queue_max = int(client_queue_max)
        self.mock = bool(mock)
        self.history_max = int(history_max)
        self.run_id: str | None = None
        self.active = False
        self.history: list[dict] = []
        self.history_dropped = 0
        self.seq = 0
        self.clients: set[_Client] = set()
        self._lock = asyncio.Lock()

    # -- 런 경계 --------------------------------------------------------
    async def start_run(self, run_id: str) -> None:
        async with self._lock:
            self.run_id = run_id
            self.active = True
            self.seq = 0
            self.history = []
            self.history_dropped = 0

    async def finish_run(self) -> None:
        async with self._lock:
            self.active = False

    def config_event(self) -> dict:
        # seq / run / t 없음 — 재생 dedup 비대상.
        return {
            "kind": "config",
            "payload": {
                "timebox_s": self.timebox_s,
                "mock": self.mock,
                "run": self.run_id,
                "active": self.active,
                # 보관 상한을 넘겨 버린 앞부분. 늦게 붙은 화면이 자기가 덜 봤다는 것을 알아야 한다.
                "history_dropped": self.history_dropped,
                "history_from": self.history[0]["seq"] if self.history else self.seq + 1,
            },
        }

    # -- 팬아웃 ---------------------------------------------------------
    async def publish(self, event: dict) -> dict:
        """이벤트 하나를 재번호해 히스토리에 넣고 전 클라이언트 큐에 밀어 넣는다.

        락 안에서 하는 일은 전부 동기다 — 네트워크를 기다리지 않는다.
        """
        async with self._lock:
            self.seq += 1
            out = dict(event)
            out["seq"] = self.seq
            out["run"] = self.run_id
            if "t" not in out:  # 원본 적재 시각이 있으면 보존한다
                out["t"] = time.time()
            self.history.append(out)
            if len(self.history) > self.history_max:
                cut = len(self.history) - self.history_max
                del self.history[:cut]
                self.history_dropped += cut
            for client in tuple(self.clients):
                if client.dropped:
                    continue
                try:
                    client.queue.put_nowait(out)
                except asyncio.QueueFull:
                    client.dropped = True
                client.wake.set()
            return out

    # -- 접속 -----------------------------------------------------------
    async def register(self, ws: WebSocket, last_seq: int, run: str | None) -> _Client:
        """재생 준비와 등록을 같은 락 안에서 — 경계 이벤트가 유실되지 않는다."""
        client = _Client(ws, self.client_queue_max)
        async with self._lock:
            replay: Iterable[dict]
            if run and run == self.run_id and last_seq > 0:
                replay = [e for e in self.history if e["seq"] > last_seq]
            else:
                replay = list(self.history)
            client.backlog = [self.config_event(), *replay]
            self.clients.add(client)
        return client

    async def unregister(self, client: _Client) -> None:
        async with self._lock:
            self.clients.discard(client)

    async def pump(self, client: _Client) -> None:
        """클라이언트 하나에게만 보낸다. 여기서 느려도 다른 클라이언트는 안 막힌다."""
        for event in client.backlog:
            await client.ws.send_json(event)
            client.sent += 1
        client.backlog = []
        while True:
            if client.dropped and client.queue.empty():
                await client.ws.close(code=1013)
                return
            try:
                event = client.queue.get_nowait()
            except asyncio.QueueEmpty:
                client.wake.clear()
                if not client.queue.empty() or client.dropped:
                    continue
                await client.wake.wait()
                continue
            await client.ws.send_json(event)
            client.sent += 1


# --------------------------------------------------------------------------
# 정적 자산
# --------------------------------------------------------------------------


def asset_version() -> str:
    """자산 URL 에 붙는 버전. stale asset 을 붙잡고 있지 않게."""
    digest = hashlib.sha1()
    for name in sorted(("index.html", "raw.html", "app.css", "app.js")):
        path = UI_DIR / name
        if path.exists():
            stat = path.stat()
            digest.update(f"{name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()[:10]


def _media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _static_headers() -> dict[str, str]:
    return {"Cache-Control": NO_STORE, "Pragma": "no-cache", "Expires": "0"}


def _page(name: str) -> HTMLResponse:
    path = UI_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{name} 을(를) 찾을 수 없습니다.")
    html = path.read_text(encoding="utf-8").replace("__V__", asset_version())
    return HTMLResponse(html, headers=_static_headers())


# --------------------------------------------------------------------------
# 앱
# --------------------------------------------------------------------------


def create_app(
    source: EventSource,
    timebox_s: float = PROFILES["surprise"],
    client_queue_max: int = 2048,
    *,
    mock: bool = False,
    history_max: int = 4000,
) -> FastAPI:
    app = FastAPI(title="REDLINE relay", docs_url=None, redoc_url=None)
    hub = Hub(
        timebox_s=timebox_s, client_queue_max=client_queue_max, mock=mock, history_max=history_max
    )
    app.state.hub = hub
    run_lock = asyncio.Lock()

    @app.exception_handler(RequestValidationError)
    async def _rejected_shape(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        """본문 모양이 틀렸을 때. pydantic 영문 원문을 화면에 올리지 않는다.

        화면은 detail 을 그대로 읽어 사용자에게 보여 준다. 그러니 여기서 나가는
        것은 언제나 한국어 한 문장이어야 한다 — 무엇이 틀렸는지가 아니라
        무엇을 보내야 하는지를 말한다.
        """
        return JSONResponse(status_code=422, content={"detail": "감사할 텍스트를 글자로 보내 주세요."})

    @app.middleware("http")
    async def _cap_body(request: Request, call_next):
        """읽기 전에 막는다. 상한을 넘는 본문은 메모리에 올리지도 않는다."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > BODY_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"보낸 내용이 너무 큽니다 — {TEXT_MAX_CHARS:,}자까지 감사합니다."},
            )
        return await call_next(request)

    async def drive(text: str) -> None:
        started = time.time()
        try:
            async for event in source(text):
                await hub.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 소스가 죽어도 화면이 영원히 진행 중으로 남지 않게
            await hub.publish(
                {
                    "kind": "status",
                    "t": time.time(),
                    "payload": {
                        "phase": "error",
                        "elapsed_s": round(time.time() - started, 2),
                        "claims": 0,
                        "tool_calls": 0,
                        "tool_calls_refused": 0,
                        "axis": 0,
                        "source_mode": "unknown",
                        "done": True,
                        "reason": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
        finally:
            await hub.finish_run()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return _page("index.html")

    @app.get("/raw", response_class=HTMLResponse)
    async def raw_page() -> HTMLResponse:
        return _page("raw.html")

    @app.get("/ui/{asset:path}")
    async def ui_asset(asset: str) -> Response:
        try:
            target = (UI_DIR / asset).resolve()
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="자산을 찾을 수 없습니다.")
        if not target.is_relative_to(UI_DIR) or not target.is_file():
            raise HTTPException(status_code=404, detail="자산을 찾을 수 없습니다.")
        body = target.read_bytes()
        if target.suffix.lower() in (".html", ".css", ".js"):
            body = body.replace(b"__V__", asset_version().encode())
        return Response(body, media_type=_media_type(target), headers=_static_headers())

    @app.post("/run")
    async def run(req: RunRequest) -> dict:
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="감사할 텍스트를 입력해 주세요.")
        if len(text) > TEXT_MAX_CHARS:
            # 자르지 않는다. 자르면 화면은 다 감사한 것처럼 보이고 뒤쪽은 조용히 사라진다.
            raise HTTPException(
                status_code=413,
                detail=(
                    f"입력이 {TEXT_MAX_CHARS:,}자를 넘었습니다 (지금 {len(text):,}자) — "
                    "감사기가 읽을 수 있는 만큼만 받습니다. 나눠서 넣어 주세요."
                ),
            )
        async with run_lock:
            if hub.active:
                raise HTTPException(
                    status_code=409,
                    detail="이미 감사가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.",
                )
            run_id = uuid.uuid4().hex[:12]
            await hub.start_run(run_id)
            task = asyncio.create_task(drive(text))
            app.state.run_task = task
        return {"run_id": run_id}

    @app.websocket("/ws/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        try:
            last_seq = int(ws.query_params.get("last_seq") or 0)
        except ValueError:
            last_seq = 0
        run = ws.query_params.get("run") or None
        client = await hub.register(ws, last_seq, run)
        pump = asyncio.create_task(hub.pump(client))
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            pump.cancel()
            await hub.unregister(client)

    return app


# --------------------------------------------------------------------------
# 이벤트 소스
# --------------------------------------------------------------------------


def jsonl_source(
    path: Path, *, speed: float = 1.0, min_gap: float = 0.02, max_gap: float = 1.2
) -> EventSource:
    """저장된 이벤트 줄을 원래 간격대로 재생한다. 입력 텍스트는 쓰지 않는다."""

    async def source(_text: str) -> AsyncIterator[dict]:
        events = load_jsonl(path)
        previous: float | None = None
        for event in events:
            stamp = event.get("t")
            if previous is None or not isinstance(stamp, (int, float)):
                gap = min_gap
            else:
                gap = max(0.0, float(stamp) - previous)
            if isinstance(stamp, (int, float)):
                previous = float(stamp)
            await asyncio.sleep(min(max_gap, max(min_gap, gap / max(speed, 0.01))))
            yield event

    return source


def load_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def agent_source(profile: str) -> EventSource:
    """실 모드. core 는 여기서만, 그것도 호출 시점에 import 한다."""

    timebox = PROFILES[profile]
    max_claims = CLAIM_CAPS[profile]

    async def source(text: str) -> AsyncIterator[dict]:
        from core.agent import run_audit  # noqa: PLC0415 — mock 모드는 core 없이 돈다

        async for event in run_audit(text, timebox_s=timebox, max_claims=max_claims):
            yield event

    return source


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="REDLINE 릴레이 서버")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="surprise")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8486)
    parser.add_argument("--mock", action="store_true", help="저장된 이벤트를 재생한다")
    parser.add_argument("--mock-file", type=Path, default=None)
    parser.add_argument("--speed", type=float, default=1.0, help="mock 재생 배속")
    return parser


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

    args = build_parser().parse_args(argv)
    mock = bool(args.mock or args.mock_file)
    if mock:
        path = args.mock_file or DEFAULT_FIXTURE
        if not path.is_file():
            print(f"이벤트 파일이 없습니다: {path}")
            return 2
        source: EventSource = jsonl_source(path, speed=args.speed)
        print(f"[mock] {path} 재생 · 배속 {args.speed}")
    else:
        source = agent_source(args.profile)
    app = create_app(source, timebox_s=PROFILES[args.profile], mock=mock)
    print(f"[redline] http://{args.host}:{args.port}/  ·  /raw  ·  프로파일 {args.profile}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
