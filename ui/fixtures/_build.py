"""개발용 합성 이벤트 파일을 만든다.

여기서 나오는 것은 **실제 감사 결과가 아니다.** 화면 개발과 회귀 검증을 위해
와이어 계약(RelayEvent)의 모양만 그대로 흉내 낸 데이터이고, 모든 audit·status
페이로드에 `source_mode: "mock"` 이 박혀 있어 화면이 모의 재생 배너를 띄운다.

    python ui/fixtures/_build.py

complete / timebox / incomplete / non_auditable / error / no_start / structured
일곱 개를 다시 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
BASE_T = 1_754_000_000.0

# 오류 런에서 화면이 쓰는 문장. 제공자 원문 예외는 `error` 에만 남고 화면에는 이것이 뜬다.
# 코어의 ERROR_REPORT_TEXT 와 같은 값이어야 하고, 어긋나면 test_server.py 가 운다.
ERROR_DISPLAY = "감사를 시작하지 못했습니다 — 내부 오류로 런이 중단됐습니다."

TOOL_SCHEMA_BLURB = (
    "당신은 문장 단위 근거 감사자다. 입력 글을 문장으로 나누고, 검증할 값어치가 있는 "
    "사실 주장만 골라 등록한 뒤 세 축으로 판정한다. 축1 존재: 글이 지목한 출처가 실재하는지 "
    "검색으로 확인한다. 축2 충실도: 그 출처가 주장을 실제로 뒷받침하는지 원문을 열어 대조한다. "
    "축3 완전성: 판정을 통과한 주장에 대해서도 반대·한정 문헌을 따로 찾는다. "
    "출처를 확인하지 못한 것을 존재하지 않는다고 단정하지 마라 — 확인 실패와 부재는 다르다. "
    "URL 은 쓰지 않는다. 증거는 원장 id 로만 인용한다. "
) * 6


def ev(kind: str, payload: dict, t: float) -> dict:
    return {"kind": kind, "t": round(BASE_T + t, 3), "payload": payload}


# 어느 클레임을 위해 쏜 검색인가. 클레임에 매이지 않은 탐색은 "explore" 로 남는다.
SEARCH_CLAIM = {
    "AI health chatbot diagnostic accuracy versus physicians": "C1",
    "AI health chatbot diagnostic accuracy limitations contrary evidence": "C1",
    "한 연구 진단 오류 80% 감소 AI 건강 상담": "C2",
    "conversational agent outcomes by age group": "C3",
    "conversational agent adverse events no effect systematic review": "C3",
    "telemedicine versus in person visit outcome equivalence": "C4",
    "medicare telehealth still need in person visit": "C5",
    "physical examination limits of telehealth": "C4",
    "systematic review diagnostic performance AI versus clinicians": "C1",
    "generative AI wellness apps amplify vulnerabilities": "C3",
    "missed physical findings remote only consultation": "C4",
    "when telehealth requires in person medical visit guidance": "C5",
}


def ledger_from(events: list[dict]) -> list[dict]:
    """지금까지 흐름에 실제로 실린 검색만으로 발사 원장을 만든다.

    선언한 숫자를 옮겨 적지 않고 이벤트에서 되짚는다 — 잘린 시나리오에서도
    원장이 그 시나리오가 실제로 쏜 것과 어긋나지 않는다.
    """
    replies: dict[str, dict] = {}
    for event in events:
        payload = event["payload"]
        if event["kind"] == "run_item" and payload.get("name") == "tool_output":
            replies[payload["item"]["raw_item"]["call_id"]] = payload["item"]["output"]
    entries = []
    for event in events:
        payload = event["payload"]
        if event["kind"] != "run_item" or payload.get("name") != "tool_called":
            continue
        raw_item = payload["item"]["raw_item"]
        if not raw_item["name"].startswith("search_"):
            continue
        args = json.loads(raw_item["arguments"])
        data = (replies.get(raw_item["call_id"]) or {}).get("data") or []
        entries.append(
            {
                "claim_id": SEARCH_CLAIM.get(args["query"], "explore"),
                "endpoint": "scholar" if raw_item["name"].endswith("scholar") else "web",
                "stance": args.get("stance") or "support",
                "query": args["query"],
                "dispatched_at": round(event["t"] - BASE_T, 3),
                # 결과 0건은 실패가 아니라 빈 결과다 — 둘을 같게 적지 않는다.
                "result_status": "ok" if data else "empty",
            }
        )
    return entries


# --------------------------------------------------------------------------
# raw 조각 — OpenAI Responses 스트림 모양
# --------------------------------------------------------------------------


class Raw:
    """output_index 를 스스로 관리하며 raw / run_item 이벤트를 낸다."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.index = 0

    def add(self, kind: str, payload: dict, t: float) -> None:
        self.events.append(ev(kind, payload, t))

    def created(self, t: float, model: str = "gpt-5.6-terra") -> None:
        self.add(
            "raw",
            {
                "type": "response.created",
                "sequence_number": len(self.events),
                "response": {
                    "id": "resp_6f2a1b9c",
                    "object": "response",
                    "model": model,
                    "status": "in_progress",
                    "parallel_tool_calls": True,
                    "instructions": TOOL_SCHEMA_BLURB,
                    "tools": [
                        {"type": "function", "name": name}
                        for name in (
                            "search_web",
                            "search_scholar",
                            "fetch_source",
                            "record_classification",
                            "record_claim",
                            "update_verdict",
                            "record_omission",
                        )
                    ],
                    "output": [],
                },
            },
            t,
        )

    def call(self, name: str, args: dict, t: float, *, gap: float = 0.09) -> tuple[int, str]:
        """output_item.added → args.delta ×2 → args.done → output_item.done → run_item."""
        idx = self.index
        self.index += 1
        call_id = f"call_{abs(hash((name, idx, json.dumps(args, sort_keys=True)))) % 10**24:024d}"
        item_id = f"fc_{idx:04d}"
        self.add(
            "raw",
            {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {
                    "id": item_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "name": name,
                    "call_id": call_id,
                    "arguments": "",
                },
            },
            t,
        )
        blob = json.dumps(args, ensure_ascii=False)
        cut = max(1, len(blob) // 2)
        for piece, offset in ((blob[:cut], gap), (blob[cut:], gap * 2)):
            self.add(
                "raw",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": idx,
                    "item_id": item_id,
                    "delta": piece,
                },
                t + offset,
            )
        self.add(
            "raw",
            {
                "type": "response.function_call_arguments.done",
                "output_index": idx,
                "item_id": item_id,
                "arguments": blob,
            },
            t + gap * 3,
        )
        self.add(
            "raw",
            {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": {
                    "id": item_id,
                    "type": "function_call",
                    "status": "completed",
                    "name": name,
                    "call_id": call_id,
                    "arguments": blob,
                },
            },
            t + gap * 4,
        )
        self.add(
            "run_item",
            {
                "name": "tool_called",
                "item": {
                    "type": "tool_call_item",
                    "raw_item": {"type": "function_call", "name": name, "call_id": call_id, "arguments": blob},
                },
            },
            t + gap * 5,
        )
        return idx, call_id

    def output(self, name: str, call_id: str, reply: dict, t: float) -> None:
        self.add(
            "run_item",
            {
                "name": "tool_output",
                "item": {
                    "type": "tool_call_output_item",
                    "raw_item": {"type": "function_call_output", "call_id": call_id, "name": name},
                    "output": reply,
                },
            },
            t,
        )

    def text(self, chunks: list[str], t: float, *, gap: float = 0.05) -> None:
        idx = self.index
        self.index += 1
        item_id = f"msg_{idx:04d}"
        self.add(
            "raw",
            {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
            },
            t,
        )
        for n, chunk in enumerate(chunks):
            self.add(
                "raw",
                {
                    "type": "response.output_text.delta",
                    "output_index": idx,
                    "item_id": item_id,
                    "content_index": 0,
                    "delta": chunk,
                },
                t + gap * (n + 1),
            )
        self.add(
            "raw",
            {
                "type": "response.output_text.done",
                "output_index": idx,
                "item_id": item_id,
                "content_index": 0,
                "text": "".join(chunks),
            },
            t + gap * (len(chunks) + 1),
        )

    def completed(self, t: float, model: str = "gpt-5.6-terra") -> None:
        self.add(
            "raw",
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_6f2a1b9c",
                    "object": "response",
                    "model": model,
                    "status": "completed",
                    "usage": {"input_tokens": 24_118, "output_tokens": 3_402, "total_tokens": 27_520},
                },
            },
            t,
        )


def budget(elapsed: float, tool_calls: int, claims: int, axis: int, timebox: float = 110.0) -> dict:
    return {
        "elapsed_s": round(elapsed, 2),
        "timebox_s": timebox,
        "tool_calls_used": tool_calls,
        "tool_calls_max": 30,
        "tool_calls_refused": 0,
        "fetch_calls_used": max(0, tool_calls - 8),
        "record_calls_used": claims * 3,
        "record_calls_max": 150,
        "record_calls_refused": 0,
        "claims_recorded": claims,
        "max_claims": 12,
        "axis_reached": axis,
        "remaining_tool_calls": 30 - tool_calls,
        "remaining_s": round(timebox - elapsed, 2),
        "time_fraction": round(elapsed / timebox, 3),
        "claims_remaining": max(0, 12 - claims),
        "fetch_allowed": elapsed < timebox * 0.6,
        "fetch_cutoff_s": timebox * 0.6,
    }


def status(
    phase: str,
    elapsed: float,
    *,
    claims: int,
    tool_calls: int,
    axis: int,
    done: bool = False,
    reason: str | None = None,
    extra: dict | None = None,
    timebox: float = 110.0,
) -> dict:
    payload = {
        "phase": phase,
        "elapsed_s": round(elapsed, 2),
        "claims": claims,
        "tool_calls": tool_calls,
        "tool_calls_refused": 0,
        "axis": axis,
        "source_mode": "mock",
        "max_tool_calls": 30,
        "done": done,
        "reason": reason,
    }
    if done:
        payload.update({
            "error": None,
            "axis3_done": 0,
            "axis3_expected": 0,
            # 화면에 쓸 문장은 사유에서 나온다 — 오류로 끝난 런에만 있다.
            "error_display": ERROR_DISPLAY if reason == "error" else None,
            # 상태를 바꾸지 않아 눌러 삼킨 기록 이벤트 수. 시나리오별로 아래에서 덮는다.
            "audit_events_suppressed": 0,
        })
    if extra:
        payload.update(extra)
    return payload


# --------------------------------------------------------------------------
# 시나리오 1 — 완주 (5문장 · 5클레임 · 반박 4건)
# --------------------------------------------------------------------------

SENTENCES = [
    "AI 기반 건강 상담은 의사 진료보다 항상 정확합니다.",
    "한 연구에 따르면 진단 오류를 80% 줄였습니다.",
    "모든 연령대에서 부작용 없이 효과가 입증되었습니다.",
    "원격 상담은 대면 진료를 완전히 대체할 수 있습니다.",
    "따라서 병원 방문은 더 이상 필요하지 않습니다.",
]
KINDS = ["prose"] * 5

CLAIM_SPEC = [
    ("C1", 0, "AI 기반 건강 상담은 의사 진료보다 항상 정확합니다", "causal", None, 0.55),
    ("C2", 1, "한 연구에 따르면 진단 오류를 80% 줄였습니다", "statistical", "한 연구", 0.50),
    ("C3", 2, "모든 연령대에서 부작용 없이 효과가 입증되었습니다", "statistical", None, 0.45),
    ("C4", 3, "원격 상담은 대면 진료를 완전히 대체할 수 있습니다", "causal", None, 0.50),
    ("C5", 4, "따라서 병원 방문은 더 이상 필요하지 않습니다", "causal", None, 0.45),
]

# 여덟 번째 값은 그 자료를 받아 온 검색의 방향이다.
EVIDENCE = [
    ("E1", "search_web", "AI health chatbot diagnostic accuracy versus physicians",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC10517477/",
     "Diagnostic accuracy of generative AI in clinical vignettes: a meta-analysis",
     "83개 연구를 종합한 결과 생성형 AI의 전체 진단 정확도는 52.1%였고 의사와 유의한 차이는 없었다.",
     {"date": "2024-11-03", "citation_count": 214, "journal": "npj Digital Medicine"}, "challenge"),
    ("E2", "search_scholar", "AI mental health chatbot adverse effects age groups",
     "https://www.nature.com/articles/s41746-024-01123-7",
     "Age-stratified outcomes of conversational agents in primary care",
     "연령대별 하위군 분석에서 65세 이상 집단의 임상 결과 개선은 통계적으로 유의하지 않았다.",
     {"date": "2024-06-18", "citation_count": 61, "journal": "npj Digital Medicine"}, "support"),
    ("E3", "search_web", "conversational agent trial adverse events",
     "https://www.ncbi.nlm.nih.gov/books/NBK595113/",
     "Digital health interventions: evidence summary",
     "부작용 보고 자체가 드물어 '부작용 없음'을 결론으로 삼기 어렵다고 정리한다.",
     {"date": "2023-09-01"}, "challenge"),
    ("E4", "search_web", "APA advisory generative AI wellness applications",
     "https://www.apa.org/topics/artificial-intelligence-machine-learning/health-advisory",
     "Health advisory on generative AI wellness applications",
     "생성형 AI 상담 도구가 기존 취약성을 증폭할 수 있다고 경고한다.",
     {"date": "2025-02-24"}, "challenge"),
    ("E5", "search_scholar", "telemedicine versus in person visit outcome equivalence",
     "https://pubmed.ncbi.nlm.nih.gov/37201983/",
     "Comparative effectiveness of telemedicine and in-person care",
     "일부 만성질환 관리에서 결과가 비슷했으나 신체검사가 필요한 진료는 제외한 비교였다.",
     {"date": "2023-05-12", "citation_count": 138, "journal": "JAMA Network Open"}, "support"),
    ("E6", "search_web", "physical examination limits of telehealth",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC9110000/",
     "Limits of the remote physical examination",
     "촉진·청진이 필요한 검사는 원격으로 대체할 수 없다고 명시한다.",
     {"date": "2022-04-30", "citation_count": 47}, "challenge"),
    ("E7", "search_web", "when telehealth requires in person medical visit guidance",
     "https://connectwithcare.org/telehealth-and-in-person-care/",
     "Telehealth and in-person care: how to choose",
     "원격 상담 뒤 대면 진료가 필요한 상황을 목록으로 제시한다.",
     {"date": "2024-01-09"}, "challenge"),
    ("E8", "search_web", "medicare telehealth still need in person visit",
     "https://www.medicare.gov/coverage/telehealth",
     "Telehealth coverage",
     "일부 항목은 대면 방문을 전제로만 급여된다고 안내한다.",
     {"date": "2025-01-01"}, "support"),
    ("E9", "search_web", "CMS telehealth providers in person requirement",
     "https://www.cms.gov/medicare/coverage/telehealth",
     "Telehealth for providers: what you need to know",
     "원격 논의로 대면 방문 필요성을 판단할 수 있다고 명시한다.",
     {"date": "2024-08-20"}, "challenge"),
    ("E10", "search_scholar", "systematic review diagnostic performance AI versus clinicians",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC11004411/",
     "A systematic review and meta-analysis of diagnostic performance",
     "AI의 진단 정확도는 52.1%였고 의사보다 항상 우월하지 않았다.",
     {"date": "2024-01-12", "citation_count": 214, "journal": "The Lancet Digital Health"}, "challenge"),
    ("E11", "search_web", "generative AI amplify existing vulnerabilities warning",
     "https://www.apa.org/news/press/releases/2025/03/ai-wellness-apps",
     "AI wellness apps may amplify existing vulnerabilities",
     "부작용이 없다는 서술과 달리 취약 집단에서 해악 사례가 보고됐다고 지적한다.",
     {"date": "2025-03-11"}, "challenge"),
    ("E12", "search_scholar", "remote consultation misses physical findings cohort",
     "https://pubmed.ncbi.nlm.nih.gov/38102244/",
     "Missed physical findings in remote-only consultation pathways",
     "원격 단독 경로에서 신체 소견 누락이 대면 대비 2.3배였다.",
     {"date": "2023-12-06", "citation_count": 32, "journal": "BMJ Quality & Safety"}, "challenge"),
    ("E13", "search_web", "cms telehealth providers what you need to know",
     "https://www.cms.gov/files/document/telehealth-toolkit-providers.pdf",
     "TELEHEALTH FOR PROVIDERS: WHAT YOU NEED TO KNOW",
     "CMS 자료는 원격 논의로 대면 방문 필요성을 판단할 수 있다고 명시한다.",
     {"date": "2024-08-20"}, "challenge"),
]

OMISSIONS = [
    ("C1", "E10", "83개 연구 메타분석은 AI의 진단 정확도를 52.1%로 보고했고, 의사보다 항상 우월하지 않았다."),
    # 확증 검색에서 걸려 나온 반박 자료 — 화면이 그 사실을 밝힌다
    ("C3", "E2", "65세 이상 집단에서는 임상 결과 개선이 통계적으로 유의하지 않았다."),
    ("C4", "E12", "원격 단독 경로에서 신체 소견 누락이 대면 대비 2.3배였다는 코호트 연구가 있다."),
    ("C5", "E13", "CMS 자료는 원격 논의로 대면 방문 필요성을 판단할 수 있다고 명시해 병원 방문이 전혀 불필요하다는 결론을 반박한다."),
]

FINAL_REPORT = """## 감사 결과

### 뒷받침 안 됨 4/5 · 출처 못 찾음 1건

- **C1 [unsupported]** AI 건강 상담이 의사보다 항상 정확하다는 주장 — 웹 검색으로 확인한 83개 연구 메타분석은 AI의 전체 진단 정확도를 52.1%로 보고했으며, 의사와 유의한 차이가 없었다.
- **C2 [no_source]** 진단 오류를 80% 줄였다는 주장 — 출처가 "한 연구"로만 적혀 있어 특정할 수 없었고, 해당 수치와 일치하는 자료도 확인하지 못했다.
- **C3 [overstated]** 모든 연령대에서 효과가 있고 부작용이 없다는 주장 — 일부 효과 연구는 있지만 임상 결과 개선은 불명확하며, 미국심리학회는 생성형 AI가 기존 취약성을 증폭할 수 있다고 경고한다.
- **C4 [overstated]** 원격 상담이 대면 진료를 완전히 대체할 수 있다는 주장 — 일부 진료 결과는 비슷할 수 있지만 포괄적인 신체검사에는 분명한 한계가 있다.
- **C5 [unsupported]** 병원 방문이 더 이상 필요하지 않다는 결론 — CMS 기관 자료는 원격 상담 과정에서도 대면 방문의 필요성을 판단해야 할 수 있다고 설명한다.

### 이 글에 대한 반박

- **A systematic review and meta-analysis of diagnostic performance** — AI의 진단 정확도는 52.1%였고 의사보다 항상 우월하지 않았다.
- **AI wellness apps may amplify existing vulnerabilities** — 부작용이 없다는 서술과 달리 취약 집단에서 해악 사례가 보고됐다.
- **Missed physical findings in remote-only consultation pathways** — 원격 단독 경로에서 신체 소견 누락이 대면 대비 2.3배였다.

### 추천 수정안

- **C1** 원문: "AI 기반 건강 상담은 의사 진료보다 항상 정확합니다." → 추천: "AI 기반 건강 상담은 일부 조건에서 의사 진료와 비슷한 정확도를 보였다는 보고가 있습니다."
- **C2** 원문: "한 연구에 따르면 진단 오류를 80% 줄였습니다." → 추천: "특정 연구에서 보고된 오류 감소 수치는 연구 설계와 대상에 따라 달라 그대로 일반화하기 어렵습니다."
- **C5** 원문: "따라서 병원 방문은 더 이상 필요하지 않습니다." → 추천: "원격 상담으로 해결되는 경우도 있지만, 대면 방문이 필요한지는 진료 과정에서 판단해야 합니다."
"""


def evidence_records() -> list[dict]:
    out = []
    for n, (eid, tool, query, url, title, snippet, extra, stance) in enumerate(EVIDENCE):
        out.append(
            {
                "id": eid,
                "tool": tool,
                "query": query,
                "url": url,
                "title": title,
                "snippet": snippet,
                "retrieved_at": round(6.0 + n * 2.4, 2),
                "extra": extra,
                "stance": stance,
            }
        )
    return out


def by_id(eid: str) -> dict:
    return next(r for r in evidence_records() if r["id"] == eid)


AXIS_PLAN = {
    # claim: [(axis, outcome, evidence_ids, confidence_after, verdict_after, evidence_text)]
    "C1": [
        (1, "pass", ["E1"], 0.55, "pending", "웹 검색에서 해당 주제를 다룬 메타분석이 실재하는 것을 확인했다."),
        (2, "fail", ["E1"], 0.30, "unsupported", "그 메타분석은 AI 진단 정확도를 52.1%로 보고해 '항상 더 정확하다'를 뒷받침하지 않는다."),
        (3, "fail", ["E10"], 0.00, "unsupported", "같은 결론을 정면으로 반박하는 체계적 문헌고찰을 추가로 확인했다."),
    ],
    "C2": [
        (1, "fail", [], 0.20, "no_source", "글이 지목한 '한 연구'를 특정할 수 없었고 80% 수치와 일치하는 자료도 찾지 못했다."),
    ],
    "C3": [
        (1, "pass", ["E2"], 0.45, "pending", "연령대별 결과를 다룬 연구가 실재하는 것을 확인했다."),
        (2, "fail", ["E2", "E3"], 0.28, "overstated", "효과 방향은 맞지만 '모든 연령대·부작용 없음'은 자료 범위를 넘는 단정이다."),
        (3, "fail", ["E4"], 0.15, "overstated", "취약 집단에서의 위험을 경고한 기관 자료가 있다."),
    ],
    "C4": [
        (1, "pass", ["E5"], 0.50, "pending", "원격·대면 비교 연구가 실재하는 것을 확인했다."),
        (2, "fail", ["E5", "E6"], 0.32, "overstated", "일부 결과는 비슷했으나 신체검사가 필요한 진료는 비교에서 제외돼 '완전히 대체'는 과하다."),
        (3, "fail", ["E7"], 0.18, "overstated", "대면 진료가 필요한 상황을 정리한 안내 자료를 확인했다."),
    ],
    "C5": [
        (1, "pass", ["E8"], 0.45, "pending", "급여 조건을 다룬 기관 자료가 실재하는 것을 확인했다."),
        (2, "fail", ["E8"], 0.22, "unsupported", "그 자료는 일부 항목이 대면 방문을 전제로만 급여된다고 안내해 결론을 뒷받침하지 않는다."),
        (3, "fail", ["E9"], 0.05, "unsupported", "대면 방문 필요성 판단을 명시한 기관 문서를 추가로 확인했다."),
    ],
}


def build_claims(upto: dict[str, int] | None = None) -> list[dict]:
    """upto[claim] = 적용할 축 판정 개수. None 이면 전부."""
    claims = []
    for cid, index, text, ctype, cited, prior in CLAIM_SPEC:
        steps = AXIS_PLAN[cid]
        take = len(steps) if upto is None else upto.get(cid, 0)
        if upto is not None and cid not in upto:
            continue
        confidence = prior
        verdict = "pending"
        axis_results = []
        previous = prior
        for axis, outcome, eids, after, verd, note in steps[:take]:
            axis_results.append(
                {
                    "axis": axis,
                    "outcome": outcome,
                    "evidence": note,
                    "evidence_ids": eids,
                    "source_urls": [by_id(e)["url"] for e in eids],
                    "delta": round(after - previous, 4),
                    "raw_delta": round(after - previous, 4),
                    "suggested_verdict": verd if verd != "pending" else None,
                }
            )
            previous = after
            confidence = after
            verdict = verd
        claims.append(
            {
                "id": cid,
                "index": index,
                "text": text,
                "claim_type": ctype,
                "auditable": True,
                "cited_source": cited,
                "base_confidence": prior,
                "confidence": round(confidence, 4),
                "verdict": verdict,
                "axis_results": axis_results,
            }
        )
    return claims


def audit_payload(
    *,
    claims: list[dict],
    omissions: list[dict],
    status_value: str,
    sentences: list[str] | None = None,
    kinds: list[str] | None = None,
    evidence_total: int = 48,
    classification: dict | None = None,
    searches: list[dict] | None = None,
) -> dict:
    sentences = SENTENCES if sentences is None else sentences
    kinds = KINDS if kinds is None else kinds
    cited: list[str] = []
    for claim in claims:
        for axis in claim["axis_results"]:
            cited.extend(axis["evidence_ids"])
    cited.extend(o["evidence_id"] for o in omissions)
    seen: list[str] = []
    for eid in cited:
        if eid not in seen:
            seen.append(eid)
    ledger = [r for r in evidence_records() if r["id"] in seen]
    audited = [c for c in claims if c["axis_results"]]
    missed = sum(1 for c in claims if c["verdict"] in ("unsupported", "overstated"))
    no_source = sum(1 for c in claims if c["verdict"] == "no_source")
    covered = sorted({c["index"] for c in audited})
    claimable = sum(1 for k in kinds if k not in ("heading", "table_header", "code_fence", "divider"))
    return {
        "sentences": sentences,
        "sentence_kinds": kinds,
        "searches": list(searches or []),
        "claims": claims,
        "omissions": omissions,
        "evidence": ledger,
        "evidence_total": evidence_total,
        "evidence_cited": len(ledger),
        "omission_count": len(omissions),
        "status": status_value,
        "classification": classification
        or {
            "input_kind": "ai_answer",
            "lang": "ko",
            "auditable": True,
            "sentence_count": len(sentences),
            "rationale": "검증 가능한 수치·인과 주장이 여러 문장에 걸쳐 있다.",
            "auditable_sentence_count": sum(
                1 for k in kinds if k not in ("heading", "table_header", "code_fence", "divider")
            ),
        },
        "source_mode": "mock",
        "max_claims": 12,
        "unsupported_rate": [missed, len(audited)],
        "no_source_count": no_source,
        "coverage": [len(covered), claimable],
        "claimable_sentence_count": claimable,
        "audited_claim_count": len(audited),
        "claims_by_index": {str(c["index"]): [c["id"]] for c in claims},
    }


def chunk(text: str, size: int = 46) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def build_complete() -> list[dict]:
    raw = Raw()
    out: list[dict] = []
    t = 0.0
    raw.created(t)
    t += 0.6

    _, cid = raw.call(
        "record_classification",
        {"input_kind": "ai_answer", "lang": "ko", "auditable": True, "sentence_count": 5,
         "rationale": "검증 가능한 수치·인과 주장이 여러 문장에 걸쳐 있다."},
        t,
    )
    t += 0.8
    raw.output("record_classification", cid,
               {"ok": True, "error": None,
                "data": {"host_sentences": [{"index": i, "kind": "prose", "text": s} for i, s in enumerate(SENTENCES)],
                         "auditable_sentence_count": 5},
                "budget": budget(t, 0, 0, 0)}, t)
    out.extend(raw.events)
    raw.events = []
    out.append(ev("audit", audit_payload(claims=[], omissions=[], status_value="running", searches=ledger_from(out)), t + 0.1))
    out.append(ev("status", status("분류", t + 0.15, claims=0, tool_calls=0, axis=0), t + 0.15))
    t += 1.1

    # 클레임 5건 등록
    for n, (cid_, index, text, ctype, cited_src, prior) in enumerate(CLAIM_SPEC):
        _, call_id = raw.call(
            "record_claim",
            {"index": index, "text": text, "claim_type": ctype, "auditable": True,
             "cited_source": cited_src},
            t,
        )
        raw.output("record_claim", call_id,
                   {"ok": True, "error": None,
                    "data": {"claim_id": cid_, "index": index, "auditable": True, "sentence": SENTENCES[index],
                             "expected_next_axis": 1, "next_action": f"{cid_}의 다음은 축1이다."},
                    "budget": budget(t, 0, n + 1, 0)}, t + 0.6)
        out.extend(raw.events)
        raw.events = []
        out.append(ev("audit", audit_payload(claims=build_claims({c: 0 for c, *_ in CLAIM_SPEC[: n + 1]}),
                                             omissions=[], status_value="running",
                                             searches=ledger_from(out)), t + 0.7))
        t += 1.0
    out.append(ev("status", status("클레임 등록", t, claims=5, tool_calls=0, axis=0), t))
    t += 0.4

    # 축1 — 검색 팬아웃
    # 확증과 반증을 같은 구간에서 나란히 발사한다 — 화면에서 두 방향이 붙어 뜬다.
    searches = [
        ("search_web", "AI health chatbot diagnostic accuracy versus physicians", "en", "support", []),
        ("search_web", "AI health chatbot diagnostic accuracy limitations contrary evidence", "en", "challenge", ["E1"]),
        ("search_web", "한 연구 진단 오류 80% 감소 AI 건강 상담", "ko", "support", []),
        ("search_scholar", "conversational agent outcomes by age group", "en", "support", ["E2"]),
        ("search_scholar", "conversational agent adverse events no effect systematic review", "en", "challenge", ["E3"]),
        ("search_scholar", "telemedicine versus in person visit outcome equivalence", "en", "support", ["E5"]),
        ("search_web", "medicare telehealth still need in person visit", "en", "support", ["E8"]),
    ]
    calls = 0
    for name, query, lang, stance, eids in searches:
        args = {"query": query, "max_results": 8, "lang": lang, "date_range": None, "stance": stance}
        _, call_id = raw.call(name, args, t)
        calls += 1
        results = []
        for eid in eids:
            rec = by_id(eid)
            results.append({"title": rec["title"], "url": rec["url"], "hostname": rec["url"].split("/")[2],
                            "description": rec["snippet"], "date": rec["extra"].get("date"),
                            "source": "scholar" if name.endswith("scholar") else "web",
                            "citation_count": rec["extra"].get("citation_count"),
                            "authors": None, "journal": rec["extra"].get("journal"),
                            "evidence_id": eid})
        raw.output(name, call_id,
                   {"ok": bool(results), "data": results,
                    "error": None if results else "not_found: 일치하는 결과가 없습니다",
                    "budget": budget(t, calls, 5, 1)}, t + 0.9)
        t += 1.3
    out.extend(raw.events)
    raw.events = []
    out.append(ev("status", status("논문·웹 출처 확인", t, claims=5, tool_calls=calls, axis=1), t))
    t += 0.3

    applied: dict[str, int] = {c: 0 for c, *_ in CLAIM_SPEC}
    omissions_so_far: list[dict] = []

    def verdict_call(claim_id: str, step_index: int, when: float) -> float:
        axis, outcome, eids, after, verd, note = AXIS_PLAN[claim_id][step_index]
        _, call_id = raw.call(
            "update_verdict",
            {"claim_id": claim_id, "axis": axis, "outcome": outcome, "evidence": note,
             "evidence_ids": eids, "verdict": None},
            when,
        )
        before = CLAIM_SPEC[int(claim_id[1:]) - 1][5] if step_index == 0 else AXIS_PLAN[claim_id][step_index - 1][3]
        raw.output("update_verdict", call_id,
                   {"ok": True, "error": None,
                    "data": {"claim_id": claim_id, "axis": axis, "outcome": outcome, "replaced": False,
                             "evidence_ids": eids, "source_urls": [by_id(e)["url"] for e in eids],
                             "confidence_before": before, "confidence_after": after,
                             "delta": round(after - before, 4),
                             "score_means": "확보한 지지 근거의 양(진실 확률 아님)",
                             "verdict": verd, "axis_chain": [a for a in range(1, axis + 1)],
                             "expected_next_axis": axis + 1 if axis < 3 else None,
                             "claim_terminal": axis == 3 or outcome == "fail" and axis == 1},
                    "budget": budget(when, calls, 5, axis)}, when + 0.5)
        applied[claim_id] = step_index + 1
        out.extend(raw.events)
        raw.events.clear()
        out.append(ev("audit", audit_payload(claims=build_claims(applied), omissions=list(omissions_so_far),
                                             status_value="running", searches=ledger_from(out)), when + 0.6))
        return when + 1.05

    for claim_id in ("C1", "C2", "C3", "C4", "C5"):
        t = verdict_call(claim_id, 0, t)
    out.append(ev("status", status("논문·웹 출처 확인", t, claims=5, tool_calls=calls, axis=1), t))
    t += 0.4

    # 축2 — 원문 대조
    for name, args, eids in [
        ("fetch_source", {"url": by_id("E1")["url"], "max_chars": 8000}, ["E1"]),
        ("fetch_source", {"url": by_id("E5")["url"], "max_chars": 8000}, ["E5"]),
        ("search_web", {"query": "physical examination limits of telehealth", "max_results": 8, "lang": "en", "date_range": None, "stance": "challenge"}, ["E6"]),
    ]:
        _, call_id = raw.call(name, args, t)
        calls += 1
        rec = by_id(eids[0])
        payload = ({"url": rec["url"], "title": rec["title"], "text": rec["snippet"] * 3, "is_pdf": False,
                    "truncated": True, "source_truncated": False, "text_truncated": True,
                    "original_chars": 41_200, "returned_chars": 8000, "evidence_id": eids[0]}
                   if name == "fetch_source" else
                   [{"title": rec["title"], "url": rec["url"], "hostname": rec["url"].split("/")[2],
                     "description": rec["snippet"], "date": rec["extra"].get("date"), "source": "web",
                     "citation_count": rec["extra"].get("citation_count"), "authors": None,
                     "journal": rec["extra"].get("journal"), "evidence_id": eids[0]}])
        raw.output(name, call_id, {"ok": True, "data": payload, "error": None, "budget": budget(t, calls, 5, 2)}, t + 1.1)
        t += 1.5
    out.extend(raw.events)
    raw.events = []
    out.append(ev("status", status("내용 확인", t, claims=5, tool_calls=calls, axis=2), t))
    t += 0.3

    for claim_id in ("C1", "C3", "C4", "C5"):
        t = verdict_call(claim_id, 1, t)
    out.append(ev("status", status("내용 확인", t, claims=5, tool_calls=calls, axis=2), t))
    t += 0.4

    # 축3 — 반박 탐색
    for name, args, eids in [
        ("search_scholar", {"query": "systematic review diagnostic performance AI versus clinicians", "max_results": 8, "lang": "en", "date_range": None, "stance": "challenge"}, ["E10"]),
        ("search_web", {"query": "generative AI wellness apps amplify vulnerabilities", "max_results": 8, "lang": "en", "date_range": None, "stance": "challenge"}, ["E4", "E11"]),
        ("search_scholar", {"query": "missed physical findings remote only consultation", "max_results": 8, "lang": "en", "date_range": None, "stance": "challenge"}, ["E12"]),
        ("search_web", {"query": "when telehealth requires in person medical visit guidance", "max_results": 8, "lang": "en", "date_range": None, "stance": "challenge"}, ["E7", "E9", "E13"]),
    ]:
        _, call_id = raw.call(name, args, t)
        calls += 1
        results = []
        for eid in eids:
            rec = by_id(eid)
            results.append({"title": rec["title"], "url": rec["url"], "hostname": rec["url"].split("/")[2],
                            "description": rec["snippet"], "date": rec["extra"].get("date"),
                            "source": "scholar" if name.endswith("scholar") else "web",
                            "citation_count": rec["extra"].get("citation_count"), "authors": None,
                            "journal": rec["extra"].get("journal"), "evidence_id": eid})
        raw.output(name, call_id, {"ok": True, "data": results, "error": None, "budget": budget(t, calls, 5, 3)}, t + 0.9)
        t += 1.25
    out.extend(raw.events)
    raw.events = []
    out.append(ev("status", status("반박 찾기", t, claims=5, tool_calls=calls, axis=3), t))
    t += 0.3

    for claim_id in ("C1", "C3", "C4", "C5"):
        t = verdict_call(claim_id, 2, t)

    for claim_id, evidence_id, summary in OMISSIONS:
        _, call_id = raw.call("record_omission",
                              {"claim_id": claim_id, "evidence_id": evidence_id, "summary": summary}, t)
        rec = by_id(evidence_id)
        omissions_so_far.append({"claim_id": claim_id, "evidence_id": evidence_id, "title": rec["title"],
                                 "url": rec["url"], "date": rec["extra"].get("date"),
                                 "citation_count": rec["extra"].get("citation_count"), "summary": summary})
        raw.output("record_omission", call_id,
                   {"ok": True, "data": {"recorded": True, "omission_count": len(omissions_so_far)},
                    "error": None, "budget": budget(t, calls, 5, 3)}, t + 0.45)
        out.extend(raw.events)
        raw.events = []
        out.append(ev("audit", audit_payload(claims=build_claims(applied), omissions=list(omissions_so_far),
                                             status_value="running", searches=ledger_from(out)), t + 0.55))
        t += 0.95

    out.append(ev("status", status("반박 찾기", t, claims=5, tool_calls=calls, axis=3), t))
    t += 0.3

    raw.text(chunk(FINAL_REPORT), t)
    t += 0.05 * (len(chunk(FINAL_REPORT)) + 2)
    raw.completed(t)
    out.extend(raw.events)
    raw.events = []
    t += 0.4

    final_claims = build_claims(applied)
    final_audit = audit_payload(claims=final_claims, omissions=list(omissions_so_far),
                                status_value="complete", searches=ledger_from(out))
    out.append(ev("audit", final_audit, t))
    out.append(
        ev(
            "status",
            status("종결", t + 0.2, claims=5, tool_calls=calls, axis=3, done=True, reason="complete",
                   extra={
                       "final_report": FINAL_REPORT,
                       "audit": {**final_audit, "input_text": " ".join(SENTENCES)},
                       "timing": {"total_s": round(t + 0.2, 2), "model_s": 41.8, "tool_wait_s": 22.4,
                                  "tool_time_serial_s": 48.1, "max_concurrent_tools": 4, "parallel_speedup": 2.15},
                       "completion": {"complete": True, "audited_claims": 5, "omissions": len(omissions_so_far),
                                      "missing_actions": [], "axis3_done": 4, "axis3_expected": 4,
                                      "axis3_required": 3, "challenge_queries": 7,
                                      "challenge_required": 2, "omission_count": len(omissions_so_far),
                                      "search_counts": {"support": 5, "challenge": 7}},
                       "partial": False,
                       "challenge_queries": 7,
                       "challenge_required": 2,
                       "axis3_done": 4,
                       "axis3_expected": 4,
                       "turn_backstop": False,
                       "events_dropped": 0,
                   }),
            t + 0.2,
        )
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 2 — 타임박스로 잘림
# --------------------------------------------------------------------------


def build_timebox() -> list[dict]:
    full = build_complete()
    out: list[dict] = []
    applied = {"C1": 3, "C2": 1, "C3": 2, "C4": 1, "C5": 0}
    stop = 0
    for n, event in enumerate(full):
        payload = event["payload"]
        if event["kind"] == "status" and payload.get("done"):
            stop = n
            break
        if event["kind"] == "audit" and len(payload.get("omissions", [])) >= 1:
            stop = n + 1
            break
        out.append(event)
    t = full[max(stop - 1, 0)]["t"] - BASE_T
    claims = build_claims(applied)
    audit = audit_payload(claims=claims, omissions=[], status_value="partial", evidence_total=41,
                          searches=ledger_from(out))
    out.append(ev("audit", audit, t + 0.3))
    out.append(
        ev(
            "status",
            status("종결", t + 0.5, claims=5, tool_calls=12, axis=3, done=True, reason="timebox",
                   extra={
                       "final_report": "## 감사 결과\n\n시간 제한에 도달해 확인한 범위까지만 보고한다.\n\n"
                                       "- **C1 [unsupported]** 메타분석 수치가 주장을 뒷받침하지 않는다.\n"
                                       "- **C2 [no_source]** 지목한 출처를 특정하지 못했다.\n"
                                       "- **C3 [overstated]** 자료 범위를 넘는 단정이다.\n",
                       "audit": {**audit, "input_text": " ".join(SENTENCES)},
                       "timing": {"total_s": 110.0, "model_s": 63.2, "tool_wait_s": 39.5,
                                  "tool_time_serial_s": 71.0, "max_concurrent_tools": 3, "parallel_speedup": 1.8},
                       "completion": {"complete": False, "audited_claims": 4, "omissions": 0,
                                      "missing_actions": ["미확정 클레임 C5",
                                                          "축3(완전성)을 1/4 클레임에만 실행했다(최소 3)"],
                                      "axis3_done": 1, "axis3_expected": 4, "axis3_required": 3,
                                      "challenge_queries": 4, "challenge_required": 2,
                                      "omission_count": 0,
                                      "search_counts": {"support": 4, "challenge": 4}},
                       "partial": True,
                       "challenge_queries": 4,
                       "challenge_required": 2,
                       "axis3_done": 1,
                       "axis3_expected": 4,
                       "turn_backstop": False,
                       "events_dropped": 0,
                       # 시계를 끝까지 쓴 런이다. 이미 쓴 판정을 다시 적은 호출이
                       # 그 시간을 먹었다 — 완주 런(0)과 같은 값일 수 없다.
                       "audit_events_suppressed": 6,
                   }),
            t + 0.5,
        )
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 2b — 모델이 스스로 마무리했으나 남은 항목이 있다
#
# 타임박스와 다른 사건이다. 시간은 남았고 모델이 끝냈는데 축3 실행량이 모자라고
# 판정 전인 클레임이 남았다 — 화면이 두 경우를 같게 보이면 안 된다.
# --------------------------------------------------------------------------


REPORT_INCOMPLETE = "\n".join([
    "## 감사 결과",
    "",
    "확인한 범위까지 보고한다.",
    "",
    "- **C1 [unsupported]** 메타분석 수치가 주장을 뒷받침하지 않는다.",
    "- **C2 [no_source]** 지목한 출처를 특정하지 못했다.",
    "- **C3 [overstated]** 자료 범위를 넘는 단정이다.",
    "- **C4 [overstated]** 신체검사 한계가 결론을 한정한다.",
    "",
])


def build_incomplete() -> list[dict]:
    full = build_complete()
    out: list[dict] = []
    for event in full:
        if event["kind"] == "audit" and len(event["payload"].get("omissions", [])) >= 1:
            out.append(event)
            break
        if event["kind"] == "status" and event["payload"].get("done"):
            break
        out.append(event)
    t = out[-1]["t"] - BASE_T
    applied = {"C1": 3, "C2": 1, "C3": 2, "C4": 2, "C5": 0}
    claims = build_claims(applied)
    rec = by_id(OMISSIONS[0][1])
    omissions = [{"claim_id": OMISSIONS[0][0], "evidence_id": OMISSIONS[0][1], "title": rec["title"],
                  "url": rec["url"], "date": rec["extra"].get("date"),
                  "citation_count": rec["extra"].get("citation_count"), "summary": OMISSIONS[0][2]}]
    audit = audit_payload(claims=claims, omissions=omissions, status_value="partial", evidence_total=44,
                          searches=ledger_from(out))
    out.append(ev("audit", audit, t + 0.3))
    out.append(
        ev(
            "status",
            status("종결", t + 0.5, claims=5, tool_calls=14, axis=3, done=True, reason="incomplete",
                   extra={
                       "partial": True,
                       "final_report": REPORT_INCOMPLETE,
                       "audit": {**audit, "input_text": " ".join(SENTENCES)},
                       "timing": {"total_s": 74.2, "model_s": 44.0, "tool_wait_s": 26.5,
                                  "tool_time_serial_s": 52.0, "max_concurrent_tools": 4, "parallel_speedup": 1.96},
                       "completion": {"complete": False, "audited_claims": 4, "omissions": 1,
                                      "missing_actions": ["미확정 클레임 C5",
                                                          "축3(완전성)을 1/4 클레임에만 실행했다(최소 3)"],
                                      "axis3_done": 1, "axis3_expected": 4, "axis3_required": 3,
                                      "challenge_queries": 4, "challenge_required": 2,
                                      "omission_count": 0,
                                      "search_counts": {"support": 4, "challenge": 4}},
                       "partial": True,
                       "challenge_queries": 4,
                       "challenge_required": 2,
                       "axis3_done": 1,
                       "axis3_expected": 4,
                       "turn_backstop": False,
                       "events_dropped": 0,
                       # 시간은 남았고 모델이 스스로 멈췄다 — 헛돈 기록이 시계를
                       # 다 쓴 런보다 적다.
                       "audit_events_suppressed": 2,
                   }),
            t + 0.5,
        )
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 3 — 감사 대상 아님
# --------------------------------------------------------------------------

OPINION_SENTENCES = [
    "저는 이번 주말에 산책을 다녀오는 편이 좋다고 생각합니다.",
    "날씨가 좋으면 기분도 한결 나아지니까요.",
    "무엇보다 스스로에게 쉬는 시간을 주는 일이 중요합니다.",
]


def build_non_auditable() -> list[dict]:
    raw = Raw()
    out: list[dict] = []
    raw.created(0.0)
    _, call_id = raw.call(
        "record_classification",
        {"input_kind": "opinion_or_creative", "lang": "ko", "auditable": False, "sentence_count": 3,
         "rationale": "검증 가능한 사실 주장 없이 권고와 소감으로만 이루어져 있다."},
        0.6,
    )
    raw.output("record_classification", call_id,
               {"ok": True, "error": None,
                "data": {"host_sentences": [{"index": i, "kind": "prose", "text": s}
                                            for i, s in enumerate(OPINION_SENTENCES)],
                         "auditable_sentence_count": 3, "status": "non_auditable"},
                "budget": budget(1.4, 0, 0, 0)}, 1.4)
    raw.completed(2.0)
    out.extend(raw.events)
    classification = {"input_kind": "opinion_or_creative", "lang": "ko", "auditable": False,
                      "sentence_count": 3, "rationale": "검증 가능한 사실 주장 없이 권고와 소감으로만 이루어져 있다.",
                      "auditable_sentence_count": 3}
    audit = audit_payload(claims=[], omissions=[], status_value="non_auditable",
                          sentences=OPINION_SENTENCES, kinds=["prose"] * 3, evidence_total=0,
                          classification=classification, searches=ledger_from(out))
    out.append(ev("audit", audit, 1.6))
    out.append(
        ev(
            "status",
            status("종결", 2.2, claims=0, tool_calls=0, axis=0, done=True, reason="non_auditable",
                   extra={
                       "final_report": "",
                       "audit": {**audit, "input_text": " ".join(OPINION_SENTENCES)},
                       "timing": {"total_s": 2.2, "model_s": 2.0, "tool_wait_s": 0.2,
                                  "tool_time_serial_s": 0.2, "max_concurrent_tools": 1, "parallel_speedup": 1.0},
                       "partial": False,
                       "challenge_queries": 0,
                       "challenge_required": 0,
                       "completion": {"complete": True, "audited_claims": 0, "omissions": 0,
                                      "missing_actions": [], "omission_count": 0,
                                      "challenge_queries": 0, "challenge_required": 0,
                                      "search_counts": {"support": 0, "challenge": 0}},
                       "turn_backstop": False,
                       "events_dropped": 0,
                   }),
            2.2,
        )
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 4 — 오류로 중단
# --------------------------------------------------------------------------


def build_error() -> list[dict]:
    full = build_complete()
    out: list[dict] = []
    for event in full:
        if event["kind"] == "status" and event["payload"].get("axis") == 1:
            out.append(event)
            break
        out.append(event)
    t = out[-1]["t"] - BASE_T
    claims = build_claims({"C1": 1, "C2": 1, "C3": 0, "C4": 0, "C5": 0})
    audit = audit_payload(claims=claims, omissions=[], status_value="partial", evidence_total=11,
                          searches=ledger_from(out))
    out.append(ev("audit", audit, t + 0.3))
    out.append(
        ev(
            "status",
            status("중단", t + 0.5, claims=5, tool_calls=5, axis=1, done=True, reason="error",
                   extra={
                       "error": "UpstreamTimeout: 모델 응답이 도착하지 않았습니다 (60s).",
                       "final_report": "",
                       "audit": {**audit, "input_text": " ".join(SENTENCES)},
                       "timing": {"total_s": round(t + 0.5, 2), "model_s": 18.0, "tool_wait_s": 9.1,
                                  "tool_time_serial_s": 12.0, "max_concurrent_tools": 3, "parallel_speedup": 1.3},
                       "completion": {"complete": False, "audited_claims": 2, "omissions": 0,
                                      "missing_actions": ["미확정 클레임 C3, C4, C5"],
                                      "axis3_done": 0, "axis3_expected": 4, "axis3_required": 3,
                                      "challenge_queries": 1, "challenge_required": 2,
                                      "omission_count": 0,
                                      "search_counts": {"support": 4, "challenge": 1}},
                       "partial": False,
                       "challenge_queries": 1,
                       "challenge_required": 2,
                       "axis3_done": 0,
                       "axis3_expected": 4,
                       "turn_backstop": False,
                       "events_dropped": 0,
                       # 상류가 끊기기 직전 같은 판정을 한 번 되풀이했다.
                       "audit_events_suppressed": 1,
                   }),
            t + 0.5,
        )
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 5 — 마크다운 구조 (제목·목록·표·코드·인용 + 감사 안 한 문장)
# --------------------------------------------------------------------------

STRUCT_SENTENCES = [
    "## 재택근무 생산성에 대한 정리",
    "재택근무는 최근 몇 년 사이 가장 많이 논의된 근무 형태다.",
    "여러 자료를 종합하면 다음과 같이 정리할 수 있다.",
    "재택근무는 평균 생산성을 13% 높인다.",
    "출퇴근 시간이 사라져 하루 여유가 늘어난다.",
    "장기적으로는 협업 밀도가 떨어진다는 지적도 있다.",
    "| 항목 | 효과 |",
    "|---|---|",
    "| 집중 업무 | 향상 |",
    "> 다만 직무에 따라 편차가 크다는 점은 대부분의 연구가 공통으로 짚는다.",
    "```",
    "productivity_delta = 0.13",
    "```",
    "---",
    "결국 제도 설계가 결과를 가른다.",
    "각 조직이 자기 맥락에서 실험해 보기를 권한다.",
]
STRUCT_KINDS = [
    "heading", "prose", "prose", "list_item", "list_item", "list_item",
    "table_header", "table_header", "table_row", "quote",
    "code_fence", "code", "code_fence", "divider", "prose", "prose",
]

STRUCT_CLAIM_SPEC = [
    ("C1", 3, "재택근무는 평균 생산성을 13% 높인다", "statistical", None, 0.55),
    ("C2", 5, "장기적으로는 협업 밀도가 떨어진다는 지적도 있다", "causal", None, 0.5),
    ("C3", 8, "집중 업무 | 향상", "definitional", None, 0.4),
]


def build_structured() -> list[dict]:
    raw = Raw()
    out: list[dict] = []
    raw.created(0.0)
    _, call_id = raw.call(
        "record_classification",
        {"input_kind": "ai_answer", "lang": "ko", "auditable": True, "sentence_count": len(STRUCT_SENTENCES),
         "rationale": "소제목과 목록 아래에 검증 가능한 수치 주장이 있다."},
        0.5,
    )
    raw.output("record_classification", call_id,
               {"ok": True, "error": None,
                "data": {"host_sentences": [{"index": i, "kind": k, "text": s}
                                            for i, (s, k) in enumerate(zip(STRUCT_SENTENCES, STRUCT_KINDS))],
                         "auditable_sentence_count": 12},
                "budget": budget(1.2, 0, 0, 0)}, 1.2)
    out.extend(raw.events)
    raw.events = []

    def struct_audit(claims: list[dict], status_value: str) -> dict:
        return audit_payload(claims=claims, omissions=[], status_value=status_value,
                             sentences=STRUCT_SENTENCES, kinds=STRUCT_KINDS, evidence_total=18,
                             searches=ledger_from(out))

    out.append(ev("audit", struct_audit([], "running"), 1.4))
    t = 1.8
    made: list[dict] = []
    plan = [
        ("C1", "unsupported", 0.18, ["E1"], "인용된 13% 수치는 특정 표본의 자기보고 결과였다."),
        ("C2", "supported", 0.72, ["E5"], "장기 협업 밀도 저하를 보고한 코호트 연구를 확인했다."),
        ("C3", "undecidable", 0.4, [], "표의 '향상'이 무엇 대비인지 특정되지 않아 판정할 수 없다."),
    ]
    for n, (cid_, verdict, conf, eids, note) in enumerate(plan):
        spec = STRUCT_CLAIM_SPEC[n]
        _, call_id = raw.call("record_claim",
                              {"index": spec[1], "text": spec[2], "claim_type": spec[3],
                               "auditable": True, "cited_source": None}, t)
        raw.output("record_claim", call_id,
                   {"ok": True, "data": {"claim_id": cid_, "index": spec[1], "sentence_kind": STRUCT_KINDS[spec[1]]},
                    "error": None, "budget": budget(t, n, n + 1, 0)}, t + 0.4)
        made.append({"id": cid_, "index": spec[1], "text": spec[2], "claim_type": spec[3], "auditable": True,
                     "cited_source": None, "base_confidence": spec[5], "confidence": spec[5],
                     "verdict": "pending",
                     "axis_results": []})
        out.extend(raw.events)
        raw.events = []
        out.append(ev("audit", struct_audit([dict(c) for c in made], "running"), t + 0.5))
        t += 0.9

    for n, (cid_, verdict, conf, eids, note) in enumerate(plan):
        _, call_id = raw.call("update_verdict",
                              {"claim_id": cid_, "axis": 2, "outcome": "fail" if verdict != "supported" else "pass",
                               "evidence": note, "evidence_ids": eids, "verdict": verdict}, t)
        raw.output("update_verdict", call_id,
                   {"ok": True, "data": {"claim_id": cid_, "verdict": verdict}, "error": None,
                    "budget": budget(t, 3 + n, 3, 2)}, t + 0.4)
        target = next(c for c in made if c["id"] == cid_)
        target["verdict"] = verdict
        target["confidence"] = conf
        target["axis_results"] = [
            {"axis": 1, "outcome": "pass", "evidence": "관련 자료가 실재함을 확인했다.",
             "evidence_ids": eids, "source_urls": [by_id(e)["url"] for e in eids],
             "delta": 0.0, "raw_delta": 0.0, "suggested_verdict": None},
            {"axis": 2, "outcome": "fail" if verdict != "supported" else "pass", "evidence": note,
             "evidence_ids": eids, "source_urls": [by_id(e)["url"] for e in eids],
             "delta": round(conf - target["base_confidence"], 4),
             "raw_delta": round(conf - target["base_confidence"], 4),
             "suggested_verdict": verdict},
        ]
        out.extend(raw.events)
        raw.events = []
        out.append(ev("audit", struct_audit([dict(c) for c in made], "running"), t + 0.5))
        t += 0.9

    raw.text(chunk("## 감사 결과\n\n- **C1 [unsupported]** 13% 수치의 근거가 약하다.\n"
                   "- **C2 [supported]** 협업 밀도 저하는 자료로 뒷받침된다.\n"
                   "- **C3 [undecidable]** 표의 서술은 비교 기준이 없어 판정할 수 없다.\n"), t)
    t += 1.2
    raw.completed(t)
    out.extend(raw.events)
    audit = struct_audit([dict(c) for c in made], "complete")
    out.append(ev("audit", audit, t + 0.3))
    out.append(
        ev("status",
           status("종결", t + 0.5, claims=3, tool_calls=6, axis=2, done=True, reason="complete",
                 extra={
                     "final_report": "## 감사 결과\n\n- **C1 [unsupported]** 13% 수치의 근거가 약하다.\n"
                                     "- **C2 [supported]** 협업 밀도 저하는 자료로 뒷받침된다.\n"
                                     "- **C3 [undecidable]** 표의 서술은 비교 기준이 없어 판정할 수 없다.\n",
                     "audit": {**audit, "input_text": "\n".join(STRUCT_SENTENCES)},
                     "timing": {"total_s": round(t + 0.5, 2), "model_s": 9.0, "tool_wait_s": 4.0,
                                "tool_time_serial_s": 5.5, "max_concurrent_tools": 2, "parallel_speedup": 1.4},
                     "partial": False,
                     "challenge_queries": 3,
                     "challenge_required": 2,
                     "completion": {"complete": True, "audited_claims": 3, "omissions": 0,
                                    "missing_actions": [], "omission_count": 0,
                                    "challenge_queries": 3, "challenge_required": 2,
                                    "search_counts": {"support": 3, "challenge": 3}},
                     "turn_backstop": False,
                     "events_dropped": 0,
                 }),
           t + 0.5)
    )
    return out


# --------------------------------------------------------------------------
# 시나리오 6 — 시작조차 못 했다
#
# 자격증명이 없어 모델을 부르지도 못한 런. 이벤트는 종결 하나뿐이고 감사 수치는
# 존재하지 않는다. 화면이 이것을 완주한 감사처럼 그리면 안 된다.
# --------------------------------------------------------------------------


def build_no_start() -> list[dict]:
    empty = {
        "sentences": [],
        "sentence_kinds": [],
        "searches": [],
        "claims": [],
        "omissions": [],
        "evidence": [],
        "evidence_total": 0,
        "evidence_cited": 0,
        "omission_count": 0,
        "status": "partial",
        "classification": None,
        "source_mode": "mock",
        "max_claims": 12,
        "unsupported_rate": [0, 0],
        "no_source_count": 0,
        "coverage": [0, 1],
        "claimable_sentence_count": 1,
        "audited_claim_count": 0,
        "claims_by_index": {},
    }
    return [
        ev(
            "status",
            status("중단", 0.83, claims=0, tool_calls=0, axis=0, done=True, reason="error",
                   extra={
                       "partial": False,
                       "error": "OpenAIError: Missing credentials. Please pass an `api_key`, "
                                "`workload_identity`, `admin_api_key`, or set the `OPENAI_API_KEY` "
                                "or `OPENAI_ADMIN_KEY` environment variable.",
                       "final_report": "뒷받침 안 됨 0/0 · 출처 못 찾음 0건 · 커버리지 0/1",
                       "audit": {**empty, "input_text": " ".join(SENTENCES)},
                       "timing": {"total_s": 0.83, "model_s": 0.83, "tool_wait_s": 0.0,
                                  "tool_time_serial_s": 0.0, "max_concurrent_tools": 0,
                                  "parallel_speedup": 1.0},
                       "completion": {"complete": False, "audited_claims": 0, "omissions": 0,
                                      "missing_actions": [], "omission_count": 0,
                                      "challenge_queries": 0, "challenge_required": 0,
                                      "search_counts": {"support": 0, "challenge": 0}},
                       "challenge_queries": 0,
                       "challenge_required": 0,
                       "turn_backstop": False,
                       "events_dropped": 0,
                   }),
            0.83,
        )
    ]


SCENARIOS = {
    "complete": build_complete,
    "timebox": build_timebox,
    "incomplete": build_incomplete,
    "non_auditable": build_non_auditable,
    "error": build_error,
    "no_start": build_no_start,
    "structured": build_structured,
}


def main() -> None:
    for name, builder in SCENARIOS.items():
        events = sorted(builder(), key=lambda e: e["t"])  # t = 릴레이 큐 적재 시각
        path = OUT / f"{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"{path.name}: {len(events)} events")


if __name__ == "__main__":
    main()
