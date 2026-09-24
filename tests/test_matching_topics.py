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
