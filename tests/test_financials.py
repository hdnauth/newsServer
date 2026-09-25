"""재무제표 조회 — 기록해 둔 원천 응답(DART)을 재생해 정규화·계산·캐시·오류를 확인한다."""
import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from newsserver.config import Settings
from newsserver.financials.cache import MemoryCache
from newsserver.financials.model import ITEM_KEYS, NotConfigured, NotFound, UpstreamError
from newsserver.financials.service import FinancialsService
from newsserver.symbols import SymbolDirectory, SymbolEntry

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "financials_dart.json").read_text())
TODAY = dt.date(2026, 9, 25)
SAMSUNG, KB, OFS_ONLY = "00126380", "00688996", "99999999"


class DartReplay:
    """기록한 응답을 돌려준다. OFS_ONLY 는 연결이 없고 별도만 있는 회사로 흉내 낸다."""

    def __init__(self, status_override: str | None = None):
        self.calls: list[tuple] = []
        self.status_override = status_override

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        if self.status_override:
            return httpx.Response(200, json={"status": self.status_override, "message": "제한"})
        corp, year, code = p["corp_code"], p["bsns_year"], p["reprt_code"]
        if request.url.path.endswith("stockTotqySttus.json"):
            self.calls.append(("shares", corp, year, code))
            data = FIXTURE["shares"].get(f"{corp}|{year}|{code}", {"status": "013"})
            return httpx.Response(200, json=data)
        fs = p["fs_div"]
        self.calls.append(("fnltt", corp, year, code, fs))
        if corp == OFS_ONLY:
            data = FIXTURE["fnltt"].get(f"{SAMSUNG}|{year}|{code}|CFS") if fs == "OFS" else None
        else:
            data = FIXTURE["fnltt"].get(f"{corp}|{year}|{code}|{fs}")
        return httpx.Response(200, json=data or {"status": "013", "message": "조회된 데이타가 없습니다."})


def make_service(replay: DartReplay, *, key: str = "test-key", ttl: int = 3600) -> FinancialsService:
    settings = Settings(_env_file=None, dart_api_key=key, financials_cache_ttl_sec=ttl)
    d = SymbolDirectory()
    d._rebuild({
        ("KR", "005930"): SymbolEntry("KR", "005930", "삼성전자", corp_code=SAMSUNG),
        ("KR", "105560"): SymbolEntry("KR", "105560", "KB금융", corp_code=KB),
        ("KR", "900001"): SymbolEntry("KR", "900001", "별도만", corp_code=OFS_ONLY),
        ("KR", "900002"): SymbolEntry("KR", "900002", "코드없음"),
    })
    http = httpx.AsyncClient(transport=httpx.MockTransport(replay))
    return FinancialsService(settings, http, d)


async def test_quarters_are_consistent_with_annual():
    svc = make_service(DartReplay())
    q = await svc.get("KR", "005930", period="quarter", limit=8, today=TODAY)
    a = await svc.get("KR", "005930", period="annual", limit=3, today=TODAY)
    labels = [(p["fiscal_year"], p["fiscal_quarter"]) for p in q["periods"]]
    assert labels == [(2026, 2), (2026, 1), (2025, 4), (2025, 3), (2025, 2), (2025, 1), (2024, 4), (2024, 3)]
    assert [p["fiscal_year"] for p in a["periods"]] == [2025, 2024, 2023]
    assert q["currency"] == "KRW" and q["name"] == "삼성전자" and q["source"] == "dart"

    quarters = {p["fiscal_quarter"]: p for p in q["periods"] if p["fiscal_year"] == 2025}
    annual = next(p for p in a["periods"] if p["fiscal_year"] == 2025)
    for key in ("revenue", "operating_income", "net_income", "net_income_attributable",
                "operating_cash_flow", "capex", "dividends_paid", "free_cash_flow"):
        assert sum(quarters[i]["items"][key] for i in (1, 2, 3, 4)) == annual["items"][key], key
    for key in ("total_assets", "total_equity", "equity_attributable", "cash_and_equivalents"):
        assert quarters[4]["items"][key] == annual["items"][key], key

    q4, q2, q1 = quarters[4], quarters[2], quarters[1]
    assert q4["filing"]["form"] == "사업보고서" and "revenue" in q4["derived"] and "eps_basic" in q4["derived"]
    assert "revenue" not in q2["derived"] and "operating_cash_flow" in q2["derived"]
    assert q1["derived"] == ["free_cash_flow"]
    assert (q2["start"], q2["end"], q4["start"], q4["end"]) == ("2025-04-01", "2025-06-30", "2025-10-01", "2025-12-31")
    assert q1["filing"]["filed"] and q1["filing"]["id"].startswith("2025")
    # 1·3분기 보고서는 주식총수를 공시하지 않는다
    assert q1["items"]["shares_outstanding"] is None and q2["items"]["shares_outstanding"] > 0
    assert annual["items"]["capex"] > 0 and annual["items"]["dividends_paid"] > 0
    assert set(q4["items"]) == set(ITEM_KEYS)


async def test_unfinished_periods_are_not_requested_and_cache_is_used():
    replay = DartReplay()
    svc = make_service(replay)
    await svc.get("KR", "005930", limit=8, today=TODAY)
    requested = {c[2:4] for c in replay.calls if c[0] == "fnltt"}
    assert ("2026", "11014") not in requested and ("2026", "11011") not in requested
    n = len(replay.calls)
    await svc.get("KR", "005930", limit=8, today=TODAY)
    await svc.get("KR", "005930", limit=4, today=TODAY)
    assert len(replay.calls) == n  # 같은 보고서는 메모리에서


async def test_concurrent_requests_share_upstream_calls():
    replay = DartReplay()
    svc = make_service(replay)
    await asyncio.gather(*(svc.get("KR", "005930", limit=4, today=TODAY) for _ in range(5)))
    assert len(replay.calls) == len(set(replay.calls))


async def test_consolidated_falls_back_to_separate_and_separate_is_explicit():
    replay = DartReplay()
    svc = make_service(replay)
    r = await svc.get("KR", "900001", limit=2, today=TODAY)
    assert [p["basis"] for p in r["periods"]] == ["separate", "separate"]
    assert r["periods"][0]["items"]["revenue"] > 0
    replay.calls.clear()
    await svc.get("KR", "005930", limit=1, basis="separate", today=TODAY)
    assert all(c[4] == "OFS" for c in replay.calls if c[0] == "fnltt")


async def test_financial_company_single_comprehensive_income_statement():
    svc = make_service(DartReplay())
    r = await svc.get("KR", "105560", limit=1, today=TODAY)
    items = r["periods"][0]["items"]
    assert items["revenue"] is None  # 금융업은 매출 계정이 없다
    assert items["operating_income"] > 0 and items["net_income_attributable"] > 0 and items["eps_basic"] > 0


async def test_raw_accounts():
    svc = make_service(DartReplay())
    r = await svc.get("KR", "005930", limit=1, raw=True, today=TODAY)
    raw = r["periods"][0]["raw"]
    assert raw and {"statement", "account_id", "account_name", "amount", "cumulative_amount"} <= set(raw[0])
    assert any(x["account_id"] == "ifrs-full_Revenue" for x in raw)
    assert "raw" not in (await svc.get("KR", "005930", limit=1, today=TODAY))["periods"][0]


async def test_errors_are_distinguished_from_empty():
    with pytest.raises(NotFound):
        await make_service(DartReplay()).get("KR", "123456", today=TODAY)
    with pytest.raises(NotFound):
        await make_service(DartReplay()).get("KR", "900002", today=TODAY)
    with pytest.raises(NotConfigured):
        await make_service(DartReplay(), key="").get("KR", "005930", today=TODAY)
    with pytest.raises(UpstreamError, match="020"):
        await make_service(DartReplay(status_override="020")).get("KR", "005930", today=TODAY)

    def down(request):
        raise httpx.ConnectError("down")
    svc = make_service(DartReplay())
    svc.dart.http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with pytest.raises(UpstreamError):
        await svc.get("KR", "005930", today=TODAY)
    # 오류는 캐시하지 않는다 — 원천이 돌아오면 바로 정상 응답
    svc.dart.http = httpx.AsyncClient(transport=httpx.MockTransport(DartReplay()))
    assert (await svc.get("KR", "005930", limit=1, today=TODAY))["periods"]


async def test_memory_cache_ttl_lru_and_missing():
    cache = MemoryCache(ttl_sec=60, max_entries=2, missing_ttl_sec=0)
    calls = []

    async def loader(v):
        calls.append(v)
        return v

    assert await cache.get_or_load("a", lambda: loader(1)) == 1
    assert await cache.get_or_load("a", lambda: loader(99)) == 1
    await cache.get_or_load("b", lambda: loader(2))
    await cache.get_or_load("c", lambda: loader(3))  # a 가 밀려난다
    assert await cache.get_or_load("a", lambda: loader(4)) == 4
    await cache.get_or_load("none", lambda: loader(None))  # '없음'은 missing_ttl(0) — 저장 안 함
    await cache.get_or_load("none", lambda: loader(None))
    assert calls == [1, 2, 3, 4, None, None] and len(cache) <= 2


async def test_api_endpoints(app_ctx):
    client, svc = app_ctx
    items = (await client.get("/v1/financials/items")).json()["items"]
    assert [i["key"] for i in items] == list(ITEM_KEYS)
    assert (await client.get("/v1/financials/KR/005930")).status_code == 404  # 픽스처 사전에는 고유번호가 없다
    async with svc.db.write() as conn:
        await conn.execute("UPDATE symbols SET corp_code = ? WHERE market = 'KR' AND symbol = '005930'", (SAMSUNG,))
    await svc.directory.load(svc.db)
    assert (await client.get("/v1/financials/KR/005930")).status_code == 409  # DART_API_KEY 없음
    svc.settings.dart_api_key = "test-key"
    svc.financials.dart.http = httpx.AsyncClient(transport=httpx.MockTransport(DartReplay()))
    r = await client.get("/v1/financials/KR/005930", params={"period": "annual", "limit": 2})
    body = r.json()
    assert r.status_code == 200 and body["periods"] and body["periods"][0]["fiscal_quarter"] is None
    assert (await client.get("/v1/financials/US/AAPL", params={"basis": "separate"})).status_code == 422
    assert (await client.get("/v1/financials/GLOBAL/X")).status_code == 422
