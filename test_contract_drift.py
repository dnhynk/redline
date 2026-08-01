"""계약 골든 테스트 — 모델 노출 툴 7종의 스키마와 종결 사유 enum 을 못박는다.

여기 적힌 기대값은 모듈 경계 계약의 정본이다. 스키마를 바꾸려면
이 테스트를 함께 고쳐야 하고, 그 변경은 소비자(프롬프트·화면) 전체에 공지돼야 한다.
"""

from core.agent import RUN_REASONS, PARTIAL_REASONS
from core.model_tools import ALL_TOOLS

import main as cli


TOOL_ORDER = [
    "search_web", "search_scholar", "fetch_source",
    "record_classification", "record_claim", "update_verdict", "record_omission",
]

EXPECTED_REQUIRED = {
    "search_web": ["query", "stance", "max_results", "lang", "date_range"],
    "search_scholar": ["query", "stance", "max_results", "lang", "date_range"],
    "fetch_source": ["url", "max_chars"],
    "record_classification": ["input_kind", "lang", "auditable", "sentence_count", "rationale"],
    "record_claim": ["index", "text", "claim_type", "auditable", "cited_source"],
    "update_verdict": ["claim_id", "axis", "outcome", "evidence", "evidence_ids", "verdict"],
    "record_omission": ["claim_id", "evidence_id", "summary"],
}

DATE_RANGE_ENUM = ["past_day", "past_week", "past_month", "past_year"]
INPUT_KIND_ENUM = ["ai_answer", "research_report", "academic_paragraph",
                   "news_or_blog", "opinion_or_creative", "unknown"]
CLAIM_TYPE_ENUM = ["statistical", "causal", "attribution", "definitional", "normative"]
OUTCOME_ENUM = ["pass", "fail", "skip", "undecidable"]
VERDICT_ENUM = ["unsupported", "overstated", "supported", "no_source", "undecidable"]


def _schemas():
    return {t.name: t.params_json_schema for t in ALL_TOOLS}


def _anyof_enum(prop):
    """anyOf 로 nullable 처리된 파라미터에서 enum 을 꺼낸다."""
    for alt in prop.get("anyOf", []):
        if "enum" in alt:
            return alt["enum"]
    return prop.get("enum")


def test_tool_names_and_order():
    assert [t.name for t in ALL_TOOLS] == TOOL_ORDER


def test_every_tool_is_a_closed_object():
    for name, schema in _schemas().items():
        assert schema["type"] == "object", name
        assert schema["additionalProperties"] is False, name


def test_required_and_property_sets_match():
    for name, schema in _schemas().items():
        assert sorted(schema["required"]) == sorted(EXPECTED_REQUIRED[name]), name
        assert sorted(schema["properties"].keys()) == sorted(EXPECTED_REQUIRED[name]), name


def test_search_defaults_and_date_range_enum():
    schemas = _schemas()
    for name in ("search_web", "search_scholar"):
        props = schemas[name]["properties"]
        assert props["max_results"].get("default") == 8, name
        assert _anyof_enum(props["date_range"]) == DATE_RANGE_ENUM, name
    assert schemas["fetch_source"]["properties"]["max_chars"].get("default") == 8000


def test_search_must_declare_a_stance():
    """검색 방향 선언은 확증·반증 동시 발사를 집행 가능하게 만드는 장치다."""
    schemas = _schemas()
    for name in ("search_web", "search_scholar"):
        props = schemas[name]["properties"]
        assert props["stance"]["enum"] == ["support", "challenge"], name
        assert "stance" in schemas[name]["required"], name
    assert "stance" not in schemas["fetch_source"]["properties"]


def test_record_tool_enums():
    schemas = _schemas()
    assert schemas["record_classification"]["properties"]["input_kind"]["enum"] == INPUT_KIND_ENUM
    assert schemas["record_claim"]["properties"]["claim_type"]["enum"] == CLAIM_TYPE_ENUM
    verdict_props = schemas["update_verdict"]["properties"]
    assert verdict_props["axis"]["enum"] == [1, 2, 3]
    assert verdict_props["outcome"]["enum"] == OUTCOME_ENUM
    assert _anyof_enum(verdict_props["verdict"]) == VERDICT_ENUM


def test_nullable_fields_have_no_default_key():
    schemas = _schemas()
    assert "default" not in schemas["record_claim"]["properties"]["cited_source"]
    assert "default" not in schemas["update_verdict"]["properties"]["verdict"]


def test_record_tools_have_no_place_for_urls_or_titles():
    """판정·누락 기록에 URL·제목을 적을 자리가 없어야 날조가 구조적으로 막힌다."""
    schemas = _schemas()
    for name in ("record_claim", "update_verdict", "record_omission"):
        for banned in ("url", "title", "source_url"):
            assert banned not in schemas[name]["properties"], (name, banned)


def test_run_reason_enum():
    assert RUN_REASONS == ("complete", "incomplete", "timebox",
                           "max_turns", "non_auditable", "error")
    assert PARTIAL_REASONS == ("incomplete", "timebox", "max_turns")
    assert set(PARTIAL_REASONS) < set(RUN_REASONS)


def test_profiles_are_the_approved_constants():
    assert cli.PROFILES == {"demo": 110.0, "surprise": 90.0}
    try:
        import server
    except ImportError:
        return
    assert server.PROFILES == cli.PROFILES


def test_claim_caps_match_between_entry_points():
    assert cli.CLAIM_CAPS == {"demo": 12, "surprise": 9}
    try:
        import server
    except ImportError:
        return
    assert server.CLAIM_CAPS == cli.CLAIM_CAPS
