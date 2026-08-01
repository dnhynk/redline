"""REDLINE CLI — 서버 없이 감사 1회를 완주한다.

입력은 인자, --file, 또는 stdin 으로 받는다. 이벤트 스트림을 사람이 읽을 수 있게
요약해 찍고, --save-events 로 원본 이벤트를 JSONL 로 남길 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# server.py 와 같은 승인 상수. 임의 초 입력은 지원하지 않는다 — 프로파일 이름 2종만.
PROFILES = {"demo": 110.0, "surprise": 90.0}

# 프로파일별 클레임 상한 — server.py 와 같은 값. 타임박스에서 역산했다:
# 클레임 하나가 기본 28초 위에 약 3.5초를 더한다(실측).
CLAIM_CAPS = {"demo": 18, "surprise": 14}


def _read_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("입력이 비어 있다: 인자, --file, 또는 stdin 으로 텍스트를 줘야 한다")
    return data


async def _run(text: str, *, timebox_s: float, max_claims: int, model: str | None,
               save_events: Path | None) -> dict:
    from core.agent import run_audit

    sink = save_events.open("w", encoding="utf-8") if save_events else None
    final_payload: dict = {}
    counts: dict[str, int] = {}
    try:
        async for event in run_audit(text, model=model, timebox_s=timebox_s,
                                     max_claims=max_claims):
            kind = event.get("kind", "?")
            counts[kind] = counts.get(kind, 0) + 1
            if sink:
                sink.write(json.dumps(event, ensure_ascii=False) + "\n")
            if kind == "status":
                payload = event.get("payload", {})
                if payload.get("done"):
                    final_payload = payload
                else:
                    print(
                        f"[{payload.get('elapsed_s', 0):6.1f}s] {payload.get('phase', '')}"
                        f" claims={payload.get('claims')} tools={payload.get('tool_calls')}"
                        f" axis={payload.get('axis')}",
                        flush=True,
                    )
            elif kind == "audit":
                payload = event.get("payload", {})
                claims = payload.get("claims") or []
                print(f"         audit: claims={len(claims)}", flush=True)
    finally:
        if sink:
            sink.close()
    final_payload["_event_counts"] = counts
    return final_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", help="감사할 텍스트 (생략 시 --file 또는 stdin)")
    parser.add_argument("--file", help="텍스트 파일 경로 (UTF-8)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="surprise",
                        help="타임박스 프로파일 (demo=110s, surprise=90s)")
    parser.add_argument("--mock", action="store_true",
                        help="검색·수집을 픽스처로 대체 (LINER_MOCK=1)")
    parser.add_argument("--model", default=None, help="모델 이름 재정의")
    parser.add_argument("--save-events", default=None, metavar="PATH",
                        help="원본 이벤트를 JSONL 로 저장")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")
    if args.mock:
        os.environ["LINER_MOCK"] = "1"

    text = _read_text(args)
    save = Path(args.save_events) if args.save_events else None
    payload = asyncio.run(_run(text, timebox_s=PROFILES[args.profile],
                               max_claims=CLAIM_CAPS[args.profile],
                               model=args.model, save_events=save))

    report = payload.get("final_report") or ""
    if report:
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)

    timing = payload.get("timing") or {}
    audit = payload.get("audit") or {}
    completion = payload.get("completion") or {}
    print(
        f"\nreason={payload.get('reason')} partial={payload.get('partial')}"
        f" total={timing.get('total_s')}s tool_wait={timing.get('tool_wait_s')}s"
    )
    print(
        f"claims={len(audit.get('claims') or [])}"
        f" omissions={len(audit.get('omissions') or [])}"
        f" evidence={len(audit.get('evidence') or [])}"
        f" coverage={audit.get('coverage')}"
        f" source_mode={audit.get('source_mode') or payload.get('source_mode')}"
    )
    print(f"events={payload.get('_event_counts')}")
    if completion:
        print(f"completion={json.dumps(completion, ensure_ascii=False)}")
    if save:
        print(f"events saved -> {save}")
    return 1 if payload.get("reason") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
