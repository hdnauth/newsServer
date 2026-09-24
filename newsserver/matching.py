"""조회 시점 종목 텍스트 매칭 규칙.

수집 시 확정된 종목 태그(``article_symbols``)가 없는 기사도 찾기 위해, 제목·요약에서
종목 코드·회사명·별칭을 찾는다. 1차로 SQL(FTS/LIKE)이 후보를 좁히고, 여기 규칙이
토큰 경계까지 정밀 검증한다.

규칙 요약
  * 영문/숫자 키워드는 영숫자 경계 필요 (``APA`` ≠ ``APACHE``)
  * 한글 키워드는 뒤에 조사가 붙어도 인정하되, 조사가 아닌 한글이 이어지면 다른 이름으로 본다
    (``삼성전자에서`` ✓ / ``삼성전자산업`` ✗)
  * 영어 단어와 같은 미국 티커(``ALL``·``ON``·``IT``·``NOW``)는 대문자 표기만 인정
  * 법인 접미사를 뗀 상호도 후보로 추가 (``NVIDIA Corporation`` → ``NVIDIA``)
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

_ASCII_TERM_RE = re.compile(r"[a-z0-9.\-]{1,10}")

_CORP_SUFFIX_RE = re.compile(
    r"[,.\s]+(?:"
    r"corporation|corp|incorporated|inc|company|co|limited|ltd|plc|llc|l\.l\.c|lp|"
    r"holdings?|group|trust|partners|n\.?v|s\.?a|a\.?g|se|ab|asa|oyj|sas|kgaa|"
    r"class\s+[a-c]|/[a-z]{2}/?"
    r")\.?$",
    re.IGNORECASE,
)
_KR_CORP_RE = re.compile(r"(주식회사|㈜|\(주\))")
_PAREN_RE = re.compile(r"\s*\(.*?\)\s*")

# 접미사를 뗀 상호가 이보다 짧으면 오탐이 압도적이다
MIN_BASE_NAME_LEN = 3


def strip_corp_suffixes(name: str) -> str:
    """상호에서 법인 접미사를 반복 제거한다 (``Alphabet Inc. Class A`` → ``Alphabet``)."""
    out = (name or "").strip().rstrip(".,")
    for _ in range(4):
        stripped = _CORP_SUFFIX_RE.sub("", out).strip().rstrip(".,")
        if stripped == out:
            break
        out = stripped
    return out


def build_search_terms(symbol: str, market: str, names: Sequence[str] = ()) -> list[str]:
    """종목 매칭 키워드 목록 (심볼, 이름, 괄호·법인 표기 제거형, 공백 제거형)."""
    candidates: list[str] = [symbol or ""]
    for name in [n for n in names if n]:
        candidates.append(name)
        cleaned = _PAREN_RE.sub(" ", name).strip()
        if not cleaned:
            continue
        candidates.append(cleaned)
        kr_cleaned = _KR_CORP_RE.sub("", cleaned).strip()
        if kr_cleaned and kr_cleaned != cleaned:
            candidates.append(kr_cleaned)
        base = strip_corp_suffixes(kr_cleaned or cleaned)
        if base and base != cleaned and len(base) >= MIN_BASE_NAME_LEN:
            candidates.append(base)
    if market == "KR" and symbol and symbol.isdigit():
        candidates.append(symbol.zfill(6))

    terms: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        t = (c or "").strip()
        # 쉼표가 남은 정식 법인 표기("SAMSUNG ELECTRONICS CO,.LTD")는 기사에 그대로 나오지 않는다 —
        # 접미사를 뗀 상호가 이미 후보에 있다
        if "," in t:
            continue
        for variant in (t, t.replace(" ", "")):
            low = variant.lower()
            # 1글자 키워드는 오탐이 압도적이라 제외
            if len(low) <= 1 or low in seen:
                continue
            seen.add(low)
            terms.append(variant)
    return terms


def ticker_needs_exact_case(symbol: str, market: str, names: Sequence[str] = ()) -> bool:
    """미국 티커 단독 매칭을 대문자 표기로만 인정해야 하는지.

    기사는 종목을 가리킬 때 티커를 대문자로 쓰지만, 대소문자를 무시하면 영어 단어와 같은
    티커가 무관한 기사에 붙는다. 예외로, 회사명 첫 단어가 티커와 같고 회사가 그 단어를
    대문자 전용으로 쓰지 않으면(Meta·Arm·Dell) 기사도 그 단어로 회사를 부르므로 대소문자를
    무시한다.
    """
    sym = (symbol or "").strip()
    if not sym or market != "US" or not sym.isalpha():
        return False
    for name in names:
        if not name:
            continue
        first = re.split(r"[\s,.'’\-]+", name.strip())[0]
        if first.lower() == sym.lower() and not first.isupper():
            return False
    return True


# 한글 이름 뒤에 붙어도 같은 이름으로 보는 조사·어미.
# 한 글자는 대부분 조사라 모두 허용하고, 두 글자 이상은 목록에 있을 때만 허용한다
# (``삼성전자에서`` ✓ / ``삼성전자산업`` ✗ — '산업' 같은 두 글자 상호 접미와 구분).
HANGUL_PARTICLES_1 = frozenset("은는이가을를의에와과도로만측엔랑")
HANGUL_PARTICLES = frozenset({
    "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "마저", "조차", "이나", "이다", "이며", "이자",
    "와의", "과의", "에는", "에도", "에선", "으론", "로는", "로서", "로써", "이랑", "한테", "께서", "라는",
    "만의", "만큼", "측은", "측이", "측의", "측에", "측도", "등이", "등은", "등의", "등과", "등을", "등도",
    "에서는", "에서도", "에서의", "으로는", "으로도", "으로서", "로부터", "에게서", "이라는", "이라고", "처럼은",
    "으로부터",
})
_LEADING_HANGUL_RE = re.compile(r"[가-힣]*")


def hangul_suffix_ok(following: str, *, strict: bool = False) -> bool:
    """한글 이름 바로 뒤 텍스트가 이름의 끝으로 볼 수 있는지.

    Args:
        following: 이름 바로 뒤의 텍스트
        strict: True 면 한 글자도 조사 목록에 있어야 한다 (두 글자 이하 짧은 이름용)
    """
    run = _LEADING_HANGUL_RE.match(following or "").group(0)
    if not run:
        return True
    if len(run) == 1:
        return run in HANGUL_PARTICLES_1 if strict else True
    return run in HANGUL_PARTICLES


def is_text_symbol_match(text: str, term: str, *, case_sensitive: bool = False) -> bool:
    """키워드가 텍스트에 독립 토큰으로 존재하는지."""
    if not text or not term:
        return False
    t = term.strip()
    if not t:
        return False
    if case_sensitive:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])"
        return re.search(pattern, text) is not None

    term_norm = t.lower()
    lowered = text.lower()
    if _ASCII_TERM_RE.fullmatch(term_norm):
        pattern = rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])"
        return re.search(pattern, lowered) is not None
    if re.search(r"[가-힣]", term_norm):
        strict = len(term_norm) <= 2
        return any(hangul_suffix_ok(lowered[m.end():], strict=strict)
                   for m in re.finditer(re.escape(term_norm), lowered))
    return term_norm in lowered


def text_matches_symbol(text: str, symbol: str, terms: Iterable[str], exact_case_ticker: bool) -> bool:
    sym_upper = (symbol or "").strip().upper()
    for term in terms:
        case_sensitive = exact_case_ticker and term.strip().upper() == sym_upper
        if is_text_symbol_match(text, term, case_sensitive=case_sensitive):
            return True
    return False


# ── 피드 태그에서 티커 추출 ────────────────────────────────────────────────
# 일부 피드는 <category> 에 종목 티커를 정확히 싣는다 (예: ['CRK', 'Long Player']).
# 작성자명·섹션명을 걸러 내기 위해 형태 + 블랙리스트로 판별한다.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,3})?$")
_TICKER_BLACKLIST = frozenset({
    "SA", "ETF", "IPO", "CEO", "CFO", "FED", "FOMC", "GDP", "CPI", "PCE", "US", "USA", "UK", "EU",
    "OPEC", "SEC", "NYSE", "IRA", "AI", "EPS", "M&A", "REIT", "REITS", "NEWS", "PRO", "ON", "THE",
    "AND", "FOR",
})


def extract_ticker_tags(entry_tags: list | None, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in entry_tags or []:
        term = (tag.get("term") if isinstance(tag, dict) else getattr(tag, "term", "")) or ""
        term = term.strip()
        if not term or " " in term or "," in term:
            continue
        upper = term.upper()
        if upper in _TICKER_BLACKLIST or upper in seen or not _TICKER_RE.match(upper):
            continue
        seen.add(upper)
        out.append(upper)
        if len(out) >= limit:
            break
    return out
