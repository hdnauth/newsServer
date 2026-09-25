from pathlib import Path

from newsserver.config import ROOT
from newsserver.matching import (
    build_search_terms,
    extract_ticker_tags,
    is_text_symbol_match,
    strip_corp_suffixes,
    text_matches_symbol,
    ticker_needs_exact_case,
)
from newsserver.topics import TopicRules


def test_ascii_ticker_needs_word_boundary():
    assert is_text_symbol_match("APA shares rose", "APA")
    assert not is_text_symbol_match("APACHE shares rose", "APA")


def test_hangul_name_allows_particle_but_not_longer_name():
    assert is_text_symbol_match("삼성전자가 상승했다", "삼성전자")
    assert not is_text_symbol_match("삼성전자산업 주가", "삼성전자")


def test_exact_case_ticker():
    assert ticker_needs_exact_case("ALL", "US", ["Allstate Corp"])
    # 회사명 첫 단어가 티커와 같고 대문자 전용 표기가 아니면 대소문자 무시
    assert not ticker_needs_exact_case("META", "US", ["Meta Platforms, Inc."])
    assert ticker_needs_exact_case("ON", "US", ["ON Semiconductor Corp"])
    assert not ticker_needs_exact_case("005930", "KR", [])
    terms = build_search_terms("ALL", "US", ["Allstate Corp"])
    assert not text_matches_symbol("stocks hit all time high", "ALL", terms, True)
    assert text_matches_symbol("ALL shares fell", "ALL", terms, True)


def test_strip_corp_suffixes():
    assert strip_corp_suffixes("Alphabet Inc. Class A") == "Alphabet"
    assert strip_corp_suffixes("NVIDIA Corporation") == "NVIDIA"
    assert strip_corp_suffixes("SAMSUNG ELECTRONICS CO,.LTD") == "SAMSUNG ELECTRONICS"
    assert strip_corp_suffixes("Amazon.com, Inc.") == "Amazon.com"


def test_build_search_terms():
    terms = build_search_terms("5930", "KR", ["삼성전자(주)"])
    assert "005930" in terms and "삼성전자" in terms
    assert all(len(t) > 1 for t in terms)
    terms = build_search_terms("005930", "KR", ["SAMSUNG ELECTRONICS CO,.LTD"])
    assert "SAMSUNG ELECTRONICS" in terms and not any("," in t for t in terms)


def test_extract_ticker_tags():
    tags = [{"term": "NVDA"}, {"term": "John Smith"}, {"term": "ETF"}, {"term": "BRK.B"}, {"term": "nvda"}]
    assert extract_ticker_tags(tags) == ["NVDA", "BRK.B"]


def rules() -> TopicRules:
    return TopicRules.load(ROOT / "config" / "topics.yaml")


def test_topic_boundaries():
    r = rules()
    assert "bigtech" in r.extract("Apple은 신제품을 공개했다")
    assert "bigtech" not in r.extract("pineapple prices surge")
    assert "sp500" in r.extract("S&P500 hits record")
    assert "ai" in r.extract("AI 반도체 수요")
    assert "ai" not in r.extract("said the aide")  # cs_terms 는 대소문자 구분


def test_topic_for_name():
    r = rules()
    assert r.for_name("KODEX 미국나스닥100") == ["nasdaq", "bigtech"]
    assert "kospi" in r.for_name("KODEX 200")


def test_topics_yaml_rejects_unknown_hint(tmp_path: Path):
    p = tmp_path / "t.yaml"
    p.write_text("version: 1\ntopics: [{key: a, terms: [x]}]\nname_hints: [{terms: [y], topics: [b]}]\n")
    try:
        TopicRules.load(p)
    except ValueError as e:
        assert "b" in str(e)
    else:
        raise AssertionError("알 수 없는 주제를 허용했다")


def test_theme_topics_for_etf_names():
    """테마 ETF 가 주제를 받는다 — 예전에는 방산·원전·우주 상품이 전부 빈 목록이었다."""
    r = rules()
    assert r.for_name("KODEX 방산TOP10") == ["defense"]
    assert r.for_name("ACE 미국SMR원자력TOP10") == ["nuclear"]
    assert r.for_name("TIGER 미국우주테크") == ["space"]
    assert r.for_name("SOL 미국양자컴퓨팅TOP10") == ["quantum"]
    assert r.for_name("KODEX 인도Nifty50") == ["india"]
    assert r.for_name("ACE 베트남VN30(합성)") == ["vietnam"]
    assert "power" in r.for_name("KODEX 미국AI전력핵심인프라")


def test_name_hint_not_terms():
    """「인도」 힌트가 「인도네시아」 상품에 붙으면 안 된다."""
    r = rules()
    assert "india" not in r.for_name("KODEX 인도네시아MSCI")


def test_theme_topics_avoid_ambiguous_korean_words():
    """경계가 없는 한국어에서 다른 뜻과 겹치는 짧은 단어로 태깅하지 않는다."""
    r = rules()
    assert "quantum" not in r.extract("한미 양자 회담 개최")
    assert "india" not in r.extract("11월 인도분 서부텍사스산원유 하락")
    assert "india" not in r.extract("니프티 피프티의 교훈")
    assert "defense" not in r.extract("무기한 연기")
    assert "gold" not in r.extract("Goldman Sachs raises target")
    assert "india" in r.extract("인도 증시 사상 최고")
    assert "defense" in r.extract("방산株 강세")


def test_earnings_topic_real_config():
    from newsserver.config import ROOT
    rules = TopicRules.load(ROOT / "config" / "topics.yaml")

    def has(title, summary=""):
        return "earnings" in rules.extract(title, summary)

    # 공시 — DART 보고서명, EDGAR 8-K 항목
    assert has("[삼성전자] 연결재무제표기준영업(잠정)실적(공정공시)")
    assert has("[에코프로] 매출액또는손익구조30%(대규모법인은15%)이상변경")
    assert has("[현대차] 반기보고서 (2026.06)")
    assert has("8-K - COSTCO WHOLESALE CORP (0000909832) (Filer)",
               "Item 2.02: Results of Operations and Financial Condition Item 9.01: Financial Statements")
    assert not has("[삼성전자] 단일판매ㆍ공급계약체결")
    assert not has("8-K - X Corp (0000000001) (Filer)", "Item 5.02: Departure of Directors")
    # 기사
    assert has("삼성전자 3분기 영업이익 10조…어닝 서프라이즈")
    assert has("상반기 역대 최대 실적 증권사들")
    assert has("TD SYNNEX Corporation (SNX) Q3 2026 Earnings Call Transcript")
    assert has("Cameco Gains 7% Despite Q2 Earnings Miss")
    assert has("Micron beats estimates on AI memory demand")
    # 일반 용법의 '실적'·'earnings' 는 제외
    assert not has("용인시, 자동차 탄소중립포인트 실적 등록")
    assert not has("수주 실적 사상 최대", "해외 수주 실적")
    assert not has("Is UiPath Stock Cheap?", "The stock trades at 20 times forward earnings.")
    assert not has("BTS 월드투어, 3분기 누적 전 세계 매출 1위")
