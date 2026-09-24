from newsserver.symbols import SymbolDirectory, SymbolEntry


def make_directory() -> SymbolDirectory:
    d = SymbolDirectory(wordlist={"crypto", "target", "apple", "global"}, stopnames={"코리아"})
    entries = [
        SymbolEntry("KR", "005930", "삼성전자"),
        SymbolEntry("KR", "005380", "현대자동차", aliases=["현대차"]),
        SymbolEntry("KR", "011200", "HMM"),
        SymbolEntry("KR", "000001", "코리아"),
        SymbolEntry("KR", "000002", "대상"),  # 2자 자동 이름은 제외
        SymbolEntry("US", "NVDA", "NVIDIA CORP", cik="1045810", rank=2),
        SymbolEntry("US", "MS", "MORGAN STANLEY", cik="895421", rank=30),
        SymbolEntry("US", "MS-PA", "MORGAN STANLEY", cik="895421", rank=4000),
        SymbolEntry("US", "CRCW", "Crypto Co", rank=9000),
        SymbolEntry("US", "TGT", "TARGET CORP", rank=300),
        SymbolEntry("US", "GIC", "Global Industrial Co", rank=2000),
        SymbolEntry("US", "AAPL", "Apple Inc.", rank=1, aliases=["Apple", "애플"]),
        SymbolEntry("US", "GOOG", "Alphabet Inc.", rank=5, aliases=["구글"]),
    ]
    d._rebuild({(e.market, e.symbol): e for e in entries})
    return d


def symbols_of(d: SymbolDirectory, title: str, summary: str = "") -> set[str]:
    return {s for _, s, _ in d.tag(title, summary)}


def test_hangul_names_and_particles():
    d = make_directory()
    assert symbols_of(d, "삼성전자가 HBM 양산") == {"005930"}
    assert symbols_of(d, "삼성전자산업 신규 수주") == set()
    assert symbols_of(d, "현대차 노조 파업") == {"005380"}
    assert symbols_of(d, "대상 기업 선정") == set()
    assert symbols_of(d, "코리아 디스카운트 해소") == set()


def test_short_curated_alias_requires_particle():
    d = make_directory()
    assert symbols_of(d, "구글은 신제품을 공개했다") == {"GOOG"}
    assert symbols_of(d, "구글링으로 찾아보니") == set()


def test_ascii_names_require_capitalized_words():
    d = make_directory()
    assert symbols_of(d, "Stocks moving: Nvidia, Morgan Stanley and more") == {"NVDA", "MS"}
    assert symbols_of(d, "global industrial output fell") == set()
    assert symbols_of(d, "Shares of Global Industrial jumped after earnings") == {"GIC"}


def test_best_ranked_ticker_wins_for_shared_name():
    d = make_directory()
    assert "MS-PA" not in symbols_of(d, "Morgan Stanley raises target")


def test_common_word_names_are_skipped_unless_curated():
    d = make_directory()
    assert symbols_of(d, "Analysts raise price Target for chipmakers") == set()
    assert symbols_of(d, "Apple unveils new iPhone") == {"AAPL"}  # 큐레이션 별칭


def test_title_case_headline_ignores_single_word_auto_names():
    d = SymbolDirectory()
    d._rebuild({("US", "BOLT"): SymbolEntry("US", "BOLT", "Boltwise Inc", rank=100)})
    assert symbols_of(d, "Boltwise shares rose after the report") == {"BOLT"}
    assert symbols_of(d, "5 Layers Of The New Boltwise Bull Market") == set()


def test_exact_case_kr_ascii_name():
    d = make_directory()
    assert symbols_of(d, "HMM 운임 상승") == {"011200"}
    assert symbols_of(d, "Hmm, not sure about that") == set()


def test_explicit_references():
    d = make_directory()
    got = d.tag("Chipmaker (NASDAQ: NVDA) beats estimates", "")
    assert ("US", "NVDA", "ref") in got
    assert ("US", "NVDA", "ref") in d.tag("$NVDA up 3%", "")
    assert ("KR", "005930", "ref") in d.tag("삼성(005930) 주가", "")
    assert d.tag("(999999) unknown code", "") == set()


def test_cik_lookup():
    d = make_directory()
    assert d.symbols_for_cik("0001045810") == [("US", "NVDA")]
