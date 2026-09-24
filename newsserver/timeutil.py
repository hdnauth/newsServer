"""시각 처리 — 저장·비교는 전부 UTC, 문자열은 ``YYYY-MM-DDTHH:MM:SSZ``.

같은 형식의 UTC 문자열은 사전순 비교가 시간순 비교와 같으므로 SQLite 에서
인덱스를 그대로 탈 수 있다.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

# 발행 시각이 수집 시각보다 이만큼 넘게 미래면 타임존 오류로 보고 버린다.
FUTURE_TOLERANCE = dt.timedelta(hours=1)


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def to_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(_ISO_FMT)


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2}|\b[A-Z]{2,5})\s*$")
# RFC 822 오프셋에 콜론을 넣는 피드(매일경제 "+09:00")가 있다. parsedate_to_datetime 은
# 이를 오프셋으로 읽지 못해 naive 로 돌려주므로 "+0900" 으로 바꿔서 넘긴다.
_COLON_OFFSET_RE = re.compile(r"([+-]\d{2}):(\d{2})\s*$")


def parse_feed_datetime(raw: str | None, parsed_struct=None, naive_tz: str = "UTC") -> dt.datetime | None:
    """피드의 발행 시각 문자열을 UTC datetime 으로 변환한다.

    타임존 표기가 없는 값은 ``naive_tz`` 로 해석한다. 일부 피드는 현지 시각을
    타임존 없이 싣는데, 범용 파서는 이를 UTC 로 읽어 시차만큼 어긋난다.

    ``parsed_struct`` 는 feedparser 의 ``*_parsed`` 값(UTC 로 정규화된 struct_time)으로,
    문자열 파싱이 실패했을 때만 쓴다. ``time.mktime`` 은 로컬 시각으로 해석하므로
    반드시 ``calendar.timegm`` 을 써야 한다.
    """
    text = (raw or "").strip()
    if text:
        value: dt.datetime | None = None
        try:
            value = parsedate_to_datetime(_COLON_OFFSET_RE.sub(r"\1\2", text))
        except (TypeError, ValueError, IndexError):
            value = None
        if value is None:
            try:
                value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                value = None
        if value is not None:
            # parsedate_to_datetime 은 "-0000" 을 naive 로 돌려준다 → 표기가 있었는지 직접 확인
            if value.tzinfo is None:
                tz = UTC if _TZ_SUFFIX_RE.search(text) else ZoneInfo(naive_tz)
                value = value.replace(tzinfo=tz)
            return value.astimezone(UTC)

    if parsed_struct:
        try:
            return dt.datetime.fromtimestamp(calendar.timegm(parsed_struct), UTC)
        except (OverflowError, ValueError, TypeError):
            return None
    return None


def sanitize_published(published: dt.datetime | None, collected: dt.datetime) -> dt.datetime | None:
    """미래 시각을 걸러 낸다. 허용 오차 이내면 수집 시각으로 당기고, 넘으면 신뢰하지 않는다."""
    if published is None:
        return None
    if published > collected + FUTURE_TOLERANCE:
        return None
    if published > collected:
        return collected
    return published
