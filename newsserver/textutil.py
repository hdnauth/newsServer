"""텍스트·URL 정규화."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 태그명이 영문인 것만 HTML 태그로 본다 — 기사 제목의 ``<종합>`` 같은 표기는 남긴다
_INLINE_TAG_RE = re.compile(r"</?(?:b|i|u|em|strong|span|font|a|sup|sub|mark|small)(?:\s[^<>]*)?/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>|<!--.*?-->", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _strip_tags(text: str) -> str:
    # 인라인 서식 태그는 단어 중간에 끼므로 공백 없이 지운다 (<b>삼성전자</b>가 → 삼성전자가)
    return _TAG_RE.sub(" ", _INLINE_TAG_RE.sub("", text))


def clean_text(raw: str | None, limit: int | None = None) -> str:
    """HTML 태그·엔티티를 제거한 평문.

    엔티티를 풀지 않으면 ``S&amp;P`` 의 ``amp`` 가 단어로 매칭되는 등 검색 오탐이 생긴다.
    엔티티 해제 후 드러나는 태그(``&lt;b&gt;``)를 위해 태그 제거를 한 번 더 한다.
    """
    if not raw:
        return ""
    text = _strip_tags(raw)
    text = html.unescape(text)
    text = _strip_tags(text)
    text = _WS_RE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


# 기사 식별과 무관한 추적용 쿼리 파라미터
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid", "yclid", "ref", "ref_src",
    "cmpid", "ncid", "soc_src", "soc_trk", "rss",
}
_MOBILE_HOST_RE = re.compile(r"^(m|mobile|amp)\.")


def url_key(url: str) -> str:
    """중복 판정용 정규화 URL.

    스킴·호스트 소문자, ``www.``/모바일 서브도메인 통일, 추적 파라미터·fragment 제거,
    남은 쿼리 파라미터 정렬, 끝 슬래시 제거.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    host = _MOBILE_HOST_RE.sub("", host)
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


_TITLE_KEY_RE = re.compile(r"[^0-9a-z가-힣]+")


def title_key(title: str) -> str:
    """교차 소스 유사 기사 판정용 제목 해시 (영숫자·한글만 남겨 비교)."""
    norm = _TITLE_KEY_RE.sub("", (title or "").lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text or "")
