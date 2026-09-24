"""시장 코드 정규화와 장 운영 시간 판정."""
from __future__ import annotations

import datetime as dt
import enum
from zoneinfo import ZoneInfo

MARKETS = ("KR", "US", "GLOBAL")

_ALIASES = {
    "KR": "KR", "KRX": "KR", "KOR": "KR", "KOSPI": "KR", "KOSDAQ": "KR", "KONEX": "KR", "KS": "KR", "KQ": "KR",
    "US": "US", "USA": "US", "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "ARCA": "US", "NYSEARCA": "US",
    "GLOBAL": "GLOBAL", "ALL": "GLOBAL",
}


def normalize_market(value: str | None) -> str | None:
    if not value:
        return None
    return _ALIASES.get(value.strip().upper())


def normalize_symbol(symbol: str, market: str) -> str:
    """KR 은 숫자 코드를 6자리로 채우고, 그 외에는 대문자로 통일한다."""
    sym = (symbol or "").strip().upper().lstrip("$")
    if market == "KR":
        for suffix in (".KS", ".KQ"):
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
        if sym.isdigit():
            sym = sym.zfill(6)
    return sym


class Session(enum.StrEnum):
    OPEN = "open"
    EXTENDED = "extended"
    CLOSED = "closed"


# (시간대, 정규장, 확장 거래 시간). 공휴일은 반영하지 않는다 — 폴링 주기 결정용이라
# 공휴일에 정규장 주기로 도는 비용은 작다.
_HOURS = {
    "KR": ("Asia/Seoul", (dt.time(9, 0), dt.time(15, 30)), (dt.time(8, 0), dt.time(20, 0))),
    "US": ("America/New_York", (dt.time(9, 30), dt.time(16, 0)), (dt.time(4, 0), dt.time(20, 0))),
}


def market_session(market: str, now: dt.datetime | None = None) -> Session:
    if market == "GLOBAL":
        states = [market_session("KR", now), market_session("US", now)]
        for state in (Session.OPEN, Session.EXTENDED):
            if state in states:
                return state
        return Session.CLOSED

    tz_name, regular, extended = _HOURS[market]
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz_name))
    if local.weekday() >= 5:
        return Session.CLOSED
    t = local.time()
    if regular[0] <= t < regular[1]:
        return Session.OPEN
    if extended[0] <= t < extended[1]:
        return Session.EXTENDED
    return Session.CLOSED
