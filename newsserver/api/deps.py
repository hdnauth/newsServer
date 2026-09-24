"""API 의존성 — 서비스 객체 접근과 쓰기 인증."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request

from newsserver.markets import normalize_market


def services(request: Request):
    return request.app.state.services


async def require_token(
    request: Request,
    authorization: str | None = Header(None),
    x_api_token: str | None = Header(None),
) -> None:
    """``API_TOKEN`` 이 설정돼 있으면 쓰기 요청에 토큰을 요구한다."""
    expected = request.app.state.services.settings.api_token
    if not expected:
        return
    given = x_api_token or (authorization or "").removeprefix("Bearer ").strip()
    if not given or not secrets.compare_digest(given, expected):
        raise HTTPException(401, "API 토큰이 필요합니다")


def split_csv(values: list[str] | None) -> list[str]:
    """``?topics=a,b&topics=c`` 처럼 반복·콤마 구분을 모두 허용한다."""
    out: list[str] = []
    for v in values or []:
        out += [p.strip() for p in v.split(",") if p.strip()]
    return list(dict.fromkeys(out))


def parse_market(value: str | None, *, required: bool = False) -> str | None:
    if not value:
        if required:
            raise HTTPException(422, "market 이 필요합니다 (KR | US)")
        return None
    market = normalize_market(value)
    if market is None:
        raise HTTPException(422, f"알 수 없는 market: {value}")
    return market
