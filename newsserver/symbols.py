"""심볼 사전 — 종목 코드 ↔ 회사명·별칭, 그리고 수집 시점 사전 태깅.

사전 원천
  * SEC ``company_tickers.json`` (미국: ticker, CIK, 회사명)
  * OpenDART ``corpCode.xml`` (한국: 종목코드, 고유번호, 회사명)
  * ``config/symbols/aliases.yaml`` (수동 큐레이션 별칭) 과 API 로 등록한 별칭

사전 태깅은 오탐을 줄이는 쪽으로 보수적이다. 놓친 기사는 조회 시점 텍스트
매칭(``matching.py``)이 보완한다.
  * 자동 수집 이름: 한글 3자 이상, 영문 단일 단어는 4자 이상이면서 일반 영어 단어가 아닐 것
  * 영문 이름은 기사에서 각 단어가 대문자로 시작할 때만 인정 (``global industrial`` ≠ ``Global Industrial``)
  * 한글 이름 뒤에 조사가 아닌 한글이 이어지면 다른 이름으로 본다 (``matching.hangul_suffix_ok``)
  * 큐레이션 별칭은 길이·단어 목록 제한을 받지 않되 경계 규칙은 같다
  * 명시적 참조 — ``$NVDA``, ``(NASDAQ: NVDA)``, ``(005930)`` — 는 사전에 있는 코드만 인정
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import httpx
import yaml
from loguru import logger

from newsserver.markets import normalize_market, normalize_symbol
from newsserver.matching import hangul_suffix_ok, strip_corp_suffixes
from newsserver.storage.db import Database
from newsserver.textutil import has_hangul
from newsserver.timeutil import to_iso, utcnow

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DART_CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"

Key = tuple[str, str]  # (market, symbol)

_HANGUL_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.\-]*")

_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9$])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")
_EXCHANGE_REF_RE = re.compile(
    r"\((?:NASDAQ|NYSE|NYSE\s+American|NYSE\s+Arca|AMEX|NasdaqGS|NasdaqGM|NasdaqCM|OTC(?:QX|QB|MKTS)?)"
    r"\s*:\s*([A-Za-z]{1,6}(?:\.[A-Za-z])?)\)",
    re.IGNORECASE,
)
_KR_CODE_REF_RE = re.compile(r"\(\s*(?:[가-힣A-Za-z]+\s*[:：]?\s*)?(\d{6})\s*\)")

MAX_NAME_WORDS = 6


@dataclass
class SymbolEntry:
    market: str
    symbol: str
    name: str
    name_en: str = ""
    aliases: list[str] = field(default_factory=list)
    cik: str | None = None
    rank: int | None = None

    def names(self) -> list[str]:
        return [n for n in [self.name, self.name_en, *self.aliases] if n]


@dataclass
class _AsciiName:
    key: Key
    original: str
    exact: bool  # True: 원문 표기와 정확히 같아야 함, False: 각 단어 대문자 시작이면 인정
    curated: bool
    rank: int | None

    @property
    def single_word(self) -> bool:
        return " " not in self.original


_TITLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")


def _is_title_case(segment: str) -> bool:
    """영문 헤드라인식 Title Case 인지 (4자 이상 단어의 85% 이상이 대문자로 시작).

    회사명을 나열한 문장형 제목("Stocks making the biggest moves: Nvidia, Intel ...")은
    소문자 일반 단어가 섞여 있어 여기에 걸리지 않는다.
    """
    words = [w for w in _TITLE_WORD_RE.findall(segment) if len(w) >= 4]
    if len(words) < 3:
        return False
    return sum(w[0].isupper() for w in words) / len(words) >= 0.85


def _ascii_words(text: str) -> list[str]:
    return [w.rstrip(".").removesuffix("'s").removesuffix("’s") for w in _ASCII_WORD_RE.findall(text)]


class SymbolDirectory:
    def __init__(self, wordlist: set[str] | None = None, stopnames: set[str] | None = None):
        self.entries: dict[Key, SymbolEntry] = {}
        self._by_cik: dict[str, list[Key]] = {}
        self._hangul: dict[str, list[Key]] = {}
        self._hangul_maxlen = 0
        self._ascii: dict[str, list[_AsciiName]] = {}
        self._wordlist = wordlist or set()
        self._stopnames = {s.lower() for s in (stopnames or set())}

    # ── 로드 ────────────────────────────────────────────────────────────────
    @staticmethod
    def load_wordlist(path: Path) -> set[str]:
        try:
            return {line.strip().lower() for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()}
        except OSError:
            logger.info("단어 목록 {} 없음 — 영문 일반명사 필터 생략", path)
            return set()

    @staticmethod
    def load_stopnames(path: Path) -> set[str]:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}

    async def load(self, db: Database) -> None:
        async with db.read() as conn:
            cur = await conn.execute("SELECT market, symbol, name, name_en, aliases_json, cik, rank FROM symbols")
            rows = await cur.fetchall()
        entries: dict[Key, SymbolEntry] = {}
        for r in rows:
            entries[(r["market"], r["symbol"])] = SymbolEntry(
                market=r["market"], symbol=r["symbol"], name=r["name"], name_en=r["name_en"] or "",
                aliases=list(json.loads(r["aliases_json"] or "[]")), cik=r["cik"], rank=r["rank"],
            )
        self._rebuild(entries)
        logger.info("심볼 사전 로드 — {}개 종목, 한글 이름 {}개, 영문 이름 {}개",
                    len(entries), len(self._hangul), len(self._ascii))

    def _rebuild(self, entries: dict[Key, SymbolEntry]) -> None:
        by_cik: dict[str, list[Key]] = {}
        hangul: dict[str, list[Key]] = {}
        ascii_names: dict[str, list[_AsciiName]] = {}

        for key, entry in entries.items():
            if entry.cik:
                by_cik.setdefault(entry.cik, []).append(key)
            auto = [entry.name, entry.name_en]
            for name, curated in [(n, False) for n in auto if n] + [(a, True) for a in entry.aliases if a]:
                self._index_name(entry, name, curated, hangul, ascii_names)

        # 자동 수집 이름 하나가 여러 종목(우선주·OTC 티커, 동명 법인)에 걸리면 원천 순위가
        # 가장 높은 종목 하나만 남긴다. 큐레이션 별칭은 종목별로 명시한 것이므로 그대로 둔다
        for name, cands in ascii_names.items():
            auto_cands = [c for c in cands if not c.curated]
            if len(auto_cands) > 1:
                best = min(auto_cands, key=lambda c: (c.rank is None, c.rank or 0, c.key[1]))
                ascii_names[name] = [c for c in cands if c.curated] + [best]

        self.entries = entries
        self._by_cik = by_cik
        self._hangul = hangul
        self._hangul_maxlen = max((len(k) for k in hangul), default=0)
        self._ascii = ascii_names

    def _index_name(self, entry: SymbolEntry, name: str, curated: bool, hangul: dict, ascii_names: dict) -> None:
        key, market = (entry.market, entry.symbol), entry.market
        name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
        name = re.sub(r"(주식회사|㈜)", "", name).strip()
        if not name or name.lower() in self._stopnames:
            return

        if has_hangul(name):
            if not re.fullmatch(r"[0-9A-Za-z가-힣]+", name):
                return  # 공백·기호가 섞인 한글 이름은 토큰 매칭 대상이 아니다
            if not curated and len(name) < 3:
                return
            hangul.setdefault(name.lower(), []).append(key)
            return

        base = name if curated else strip_corp_suffixes(name)
        words = _ascii_words(base)
        if not words or len(words) > MAX_NAME_WORDS:
            return
        joined = " ".join(words)
        exact = False
        if not curated:
            if len(words) == 1:
                word = words[0]
                if word.isupper() and 3 <= len(word) <= 5 and market == "KR":
                    exact = True  # HMM, KCC 같은 대문자 약칭 상호
                elif len(word) < 4 or word.lower() in self._wordlist:
                    return
            if all(w.isdigit() for w in words):
                return
        else:
            exact = joined.isupper() and len(joined) <= 5
        ascii_names.setdefault(joined.lower(), []).append(_AsciiName(key, joined, exact, curated, entry.rank))

    # ── 조회 ────────────────────────────────────────────────────────────────
    def get(self, market: str, symbol: str) -> SymbolEntry | None:
        return self.entries.get((market, symbol))

    def names_for(self, market: str, symbol: str) -> list[str]:
        entry = self.entries.get((market, symbol))
        return entry.names() if entry else []

    def symbols_for_cik(self, cik: str) -> list[Key]:
        return list(self._by_cik.get(str(cik).lstrip("0"), []))

    # ── 태깅 ────────────────────────────────────────────────────────────────
    def tag(self, title: str, summary: str = "") -> set[tuple[str, str, str]]:
        """텍스트에서 종목을 찾는다. 반환: {(market, symbol, method)} — method 는 dict | ref."""
        text = f"{title}\n{summary}"
        found: dict[Key, str] = {}

        for key in self._tag_refs(text):
            found[key] = "ref"
        for key in self._tag_hangul(text):
            found.setdefault(key, "dict")
        for segment in (title, summary):
            for key in self._tag_ascii(segment):
                found.setdefault(key, "dict")
        return {(m, s, method) for (m, s), method in found.items()}

    def _tag_refs(self, text: str) -> list[Key]:
        out: list[Key] = []
        for m in _CASHTAG_RE.finditer(text):
            key = ("US", m.group(1).replace(".", "-"))
            if key in self.entries:
                out.append(key)
        for m in _EXCHANGE_REF_RE.finditer(text):
            key = ("US", m.group(1).upper().replace(".", "-"))
            if key in self.entries:
                out.append(key)
        for m in _KR_CODE_REF_RE.finditer(text):
            key = ("KR", m.group(1))
            if key in self.entries:
                out.append(key)
        return out

    def _tag_hangul(self, text: str) -> list[Key]:
        if not self._hangul:
            return []
        out: list[Key] = []
        for m in _HANGUL_TOKEN_RE.finditer(text):
            token = m.group(0)
            if not has_hangul(token):
                continue
            lowered = token.lower()
            for length in range(min(len(token), self._hangul_maxlen), 1, -1):
                hits = self._hangul.get(lowered[:length])
                if not hits:
                    continue
                if hangul_suffix_ok(token[length:], strict=length <= 2):
                    out.extend(hits)
                    break
        return out

    def _tag_ascii(self, text: str) -> list[Key]:
        if not self._ascii:
            return []
        words = [(m.start(), m.end(), m.group(0)) for m in _ASCII_WORD_RE.finditer(text)]
        # Title Case 헤드라인은 모든 단어가 대문자로 시작해 한 단어 자동 이름의 대문자
        # 검증이 무력해진다 (예: "New Crypto Bull Market" 의 Crypto)
        title_case = _is_title_case(text)
        out: list[Key] = []
        i = 0
        while i < len(words):
            matched = 0
            for n in range(min(MAX_NAME_WORDS, len(words) - i), 0, -1):
                span = words[i:i + n]
                # 단어 사이가 공백으로만 이어져야 하나의 이름이다
                if any(not text[span[k][1]:span[k + 1][0]].isspace() or len(text[span[k][1]:span[k + 1][0]]) > 2
                       for k in range(n - 1)):
                    continue
                originals = [w.rstrip(".").removesuffix("'s").removesuffix("’s") for _, _, w in span]
                candidates = self._ascii.get(" ".join(originals).lower())
                if not candidates:
                    continue
                joined = " ".join(originals)
                for cand in candidates:
                    if title_case and cand.single_word and not (cand.curated or cand.exact):
                        continue
                    if cand.exact:
                        ok = joined == cand.original
                    else:
                        ok = all(w[:1].isupper() or w[:1].isdigit() for w in originals)
                    if ok:
                        out.append(cand.key)
                        matched = n
                if matched:
                    break
            i += matched or 1
        return out

    # ── 갱신 ────────────────────────────────────────────────────────────────
    async def refresh_remote(self, db: Database, http: httpx.AsyncClient, *, edgar_user_agent: str,
                             dart_api_key: str) -> dict[str, int]:
        """원격 사전을 내려받아 DB 를 갱신한다. 자격 증명이 없는 원천은 건너뛴다."""
        stats: dict[str, int] = {}
        if edgar_user_agent:
            try:
                stats["sec"] = await self._refresh_sec(db, http, edgar_user_agent)
            except Exception as e:  # noqa: BLE001 — 원천 하나의 실패가 다른 원천을 막지 않게
                logger.warning("SEC 티커 사전 갱신 실패: {}", e)
        if dart_api_key:
            try:
                stats["dart"] = await self._refresh_dart(db, http, dart_api_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("DART 기업코드 사전 갱신 실패: {}", e)
        return stats

    async def _refresh_sec(self, db: Database, http: httpx.AsyncClient, user_agent: str) -> int:
        resp = await http.get(SEC_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30)
        resp.raise_for_status()
        rows = []
        # 키("0", "1", ...) 순서가 대략 시가총액 순이다 → rank 로 보존
        for rank, item in resp.json().items():
            ticker = normalize_symbol(str(item.get("ticker") or ""), "US")
            title = str(item.get("title") or "").strip()
            if ticker and title:
                cik = str(item.get("cik_str") or "").lstrip("0") or None
                rows.append(("US", ticker, title, "", cik, None, int(rank) if str(rank).isdigit() else None))
        return await self._replace_origin(db, "sec", rows)

    async def _refresh_dart(self, db: Database, http: httpx.AsyncClient, api_key: str) -> int:
        resp = await http.get(DART_CORPCODE_URL, params={"crtfc_key": api_key}, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        rows = []
        for node in ElementTree.fromstring(xml_bytes).iter("list"):
            stock_code = (node.findtext("stock_code") or "").strip()
            name = (node.findtext("corp_name") or "").strip()
            if not stock_code or not name:
                continue
            rows.append((
                "KR", normalize_symbol(stock_code, "KR"), name,
                (node.findtext("corp_eng_name") or "").strip(), None,
                (node.findtext("corp_code") or "").strip() or None, None,
            ))
        return await self._replace_origin(db, "dart", rows)

    async def _replace_origin(self, db: Database, origin: str, rows: list[tuple]) -> int:
        # 응답이 비정상적으로 작으면 기존 사전을 지우지 않는다
        if len(rows) < 500:
            raise ValueError(f"{origin} 사전 항목이 {len(rows)}개뿐 — 갱신 중단")
        now = to_iso(utcnow())
        async with db.write() as conn:
            await conn.executemany(
                "INSERT INTO symbols(market, symbol, name, name_en, cik, corp_code, rank, origin, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(market, symbol) DO UPDATE SET name = excluded.name, name_en = excluded.name_en, "
                "cik = excluded.cik, corp_code = excluded.corp_code, rank = excluded.rank, origin = excluded.origin, "
                "updated_at = excluded.updated_at",
                [(*r, origin, now) for r in rows],
            )
            # 이번 갱신에 없는 종목(상장폐지 등) 제거. 별칭이 달린 항목은 수동 관리로 보고 남긴다
            await conn.execute(
                "DELETE FROM symbols WHERE origin = ? AND updated_at < ? AND aliases_json = '[]'",
                (origin, now),
            )
        logger.info("심볼 사전 갱신 — {} {}건", origin, len(rows))
        return len(rows)

    @staticmethod
    async def apply_manual_aliases(db: Database, path: Path) -> int:
        """``aliases.yaml`` 의 별칭을 사전에 합친다. 사전에 없는 종목은 name 이 있을 때만 추가."""
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError:
            return 0
        now = to_iso(utcnow())
        count = 0
        async with db.write() as conn:
            for item in data.get("symbols") or []:
                market = normalize_market(str(item.get("market") or ""))
                if not market or market == "GLOBAL":
                    raise ValueError(f"aliases.yaml: market 이 올바르지 않습니다 — {item}")
                symbol = normalize_symbol(str(item.get("symbol") or ""), market)
                aliases = [str(a) for a in item.get("aliases") or [] if str(a).strip()]
                cur = await conn.execute(
                    "SELECT aliases_json FROM symbols WHERE market = ? AND symbol = ?", (market, symbol)
                )
                row = await cur.fetchone()
                if row is None:
                    if not item.get("name"):
                        continue
                    await conn.execute(
                        "INSERT INTO symbols(market, symbol, name, aliases_json, origin, updated_at) "
                        "VALUES (?, ?, ?, ?, 'manual', ?)",
                        (market, symbol, str(item["name"]), json.dumps(aliases, ensure_ascii=False), now),
                    )
                else:
                    merged = list(dict.fromkeys([*json.loads(row[0] or "[]"), *aliases]))
                    await conn.execute(
                        "UPDATE symbols SET aliases_json = ? WHERE market = ? AND symbol = ?",
                        (json.dumps(merged, ensure_ascii=False), market, symbol),
                    )
                count += 1
        return count
