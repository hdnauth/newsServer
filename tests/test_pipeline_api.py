"""파이프라인 저장·중복 처리와 HTTP API (lifespan 포함, 스케줄러 비활성)."""
import datetime as dt

from newsserver.collectors.base import RawItem, SymbolTag, SymbolTarget
from newsserver.timeutil import utcnow

UTC = dt.timezone.utc


def item(title, url, summary="", hours_ago=1.0, **kw) -> RawItem:
    return RawItem(title=title, url=url, summary=summary,
                   published_at=utcnow() - dt.timedelta(hours=hours_ago), **kw)


async def ingest(svc, key, items, target=None):
    return await svc.ingestor.ingest(svc.specs[key], items, target=target)


async def seed(svc):
    await ingest(svc, "yonhap_market", [
        item("삼성전자, HBM4 양산 돌입", "https://yna.test/1", "메모리 반도체 공급 확대", 2),
        item("삼성전자산업 신규 수주", "https://yna.test/2", "", 3),
        item("코스피 외국인 순매수", "https://yna.test/3", "환율 하락과 금리 안정", 4),
    ])
    await ingest(svc, "cnbc_investing", [
        item("Stocks making moves: Nvidia, Intel and more", "https://cnbc.test/1", "", 1),
        item("ALL shares slip after guidance cut", "https://cnbc.test/2", "", 1),
        item("stocks hit all time high", "https://cnbc.test/3", "", 1),
    ])
    await ingest(svc, "dart", [
        RawItem(title="[삼성전자] 주요사항보고서", url="https://dart.test/1", summary="법인구분: 유가증권",
                symbols=[SymbolTag("KR", "005930", "dart_code")]),
        RawItem(title="[삼성전자] 임원ㆍ주요주주특정증권등소유상황보고서", url="https://dart.test/2", summary="-"),
    ])
    await ingest(svc, "yonhap_sports", [item("축구 대표팀 승리", "https://yna.test/sports/1", "", 1)])


async def test_duplicate_url_keeps_one_article_and_all_feeds(app_ctx):
    client, svc = app_ctx
    s1 = await ingest(svc, "einfomax_all", [item("금리 동결", "https://einfomax.test/a?idxno=1&utm_source=x", "", 1)])
    s2 = await ingest(svc, "einfomax_stock",
                      [item("금리 동결", "http://www.einfomax.test/a?idxno=1", "한국은행이 기준금리를 동결했다", 1)])
    assert (s1.n_new, s2.n_new, s2.n_updated) == (1, 0, 1)
    body = (await client.get("/v1/headlines")).json()
    assert len(body["items"]) == 1
    art = body["items"][0]
    assert art["feeds"] == ["einfomax_all", "einfomax_stock"]
    assert art["summary"] == "한국은행이 기준금리를 동결했다"  # 뒤 소스의 요약으로 보강
    assert art["body_kind"] == "summary"
    assert "bond" in art["topics"]


async def test_items_older_than_retention_are_skipped(app_ctx):
    _, svc = app_ctx
    stats = await ingest(svc, "yonhap_sports", [item("옛 기사", "https://yna.test/old", hours_ago=24 * 400)])
    assert stats.n_new == 0 and stats.n_skipped == 1


async def test_symbol_query_tag_and_text_paths(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    body = (await client.get("/v1/headlines", params={"symbol": "5930", "market": "KRX", "hours": 48})).json()
    titles = {i["title"]: i["match"] for i in body["items"]}
    assert titles["삼성전자, HBM4 양산 돌입"] == "tag"  # 사전 태깅 (fixture aliases)
    assert titles["[삼성전자] 주요사항보고서"] == "tag"  # 공시 종목코드
    assert "삼성전자산업 신규 수주" not in titles
    # 종목 태그 없는 공시는 텍스트로 찾지 않는다 (filings=auto)
    assert "[삼성전자] 임원ㆍ주요주주특정증권등소유상황보고서" not in titles
    assert body["query"]["symbol"] == "005930" and "삼성전자" in body["query"]["terms"]

    body = (await client.get("/v1/headlines", params={"symbol": "005930", "market": "KR", "filings": "include",
                                                      "match": "text"})).json()
    assert "[삼성전자] 임원ㆍ주요주주특정증권등소유상황보고서" in {i["title"] for i in body["items"]}


async def test_symbol_query_exact_case_ticker(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    body = (await client.get("/v1/headlines", params={"symbol": "ALL", "market": "US", "name": "Allstate Corp"})).json()
    assert [i["title"] for i in body["items"]] == ["ALL shares slip after guidance cut"]


async def test_symbol_query_requires_market(app_ctx):
    client, _ = app_ctx
    assert (await client.get("/v1/headlines", params={"symbol": "NVDA"})).status_code == 422


async def test_topic_category_market_and_kind_filters(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    body = (await client.get("/v1/headlines", params={"topics": "fx,bond"})).json()
    assert [i["title"] for i in body["items"]] == ["코스피 외국인 순매수"]
    body = (await client.get("/v1/headlines", params={"category": "sports"})).json()
    assert [i["title"] for i in body["items"]] == ["축구 대표팀 승리"]
    body = (await client.get("/v1/headlines", params={"market": "US"})).json()
    assert all(i["market"] == "US" for i in body["items"]) and len(body["items"]) == 3
    body = (await client.get("/v1/headlines", params={"kind": "filing"})).json()
    assert len(body["items"]) == 2 and all(i["is_filing"] for i in body["items"])
    assert (await client.get("/v1/headlines", params={"topics": "nope"})).status_code == 422


async def test_fulltext_and_sources(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    body = (await client.get("/v1/headlines", params={"q": "HBM4"})).json()
    assert [i["title"] for i in body["items"]] == ["삼성전자, HBM4 양산 돌입"]
    body = (await client.get("/v1/headlines", params={"q": "코스"})).json()  # 3자 미만 → LIKE
    assert [i["title"] for i in body["items"]] == ["코스피 외국인 순매수"]
    body = (await client.get("/v1/headlines", params={"sources": "dart"})).json()
    assert len(body["items"]) == 2
    body = (await client.get("/v1/headlines", params={"exclude_sources": "dart,yonhap_sports"})).json()
    assert all(i["source"]["key"] not in ("dart", "yonhap_sports") for i in body["items"])


async def test_since_id_cursor(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    first = (await client.get("/v1/headlines", params={"since_id": 0, "limit": 3})).json()
    ids = [i["id"] for i in first["items"]]
    assert ids == sorted(ids) and first["next_since_id"] == ids[-1]
    rest = (await client.get("/v1/headlines", params={"since_id": first["next_since_id"], "limit": 100})).json()
    assert all(i["id"] > ids[-1] for i in rest["items"])
    empty = (await client.get("/v1/headlines", params={"since_id": 10_000})).json()
    assert empty["items"] == [] and empty["next_since_id"] == 10_000


async def test_by_symbols_and_article(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    resp = await client.post("/v1/headlines/by-symbols", json={
        "symbols": [{"symbol": "005930", "market": "KR"}, {"symbol": "NVDA", "market": "US"}], "limit_per_symbol": 5})
    results = resp.json()["results"]
    assert set(results) == {"KR:005930", "US:NVDA"}
    assert [i["title"] for i in results["US:NVDA"]] == ["Stocks making moves: Nvidia, Intel and more"]
    art_id = results["US:NVDA"][0]["id"]
    assert (await client.get(f"/v1/articles/{art_id}")).json()["id"] == art_id
    assert (await client.get("/v1/articles/999999")).status_code == 404


async def test_search_target_tag(app_ctx):
    client, svc = app_ctx
    await ingest(svc, "gnews_kr_symbol", [item("신제품 공개 행사", "https://news.google.test/1")],
                 target=SymbolTarget("KR", "005930", ["삼성전자"]))
    body = (await client.get("/v1/headlines", params={"symbol": "005930", "market": "KR"})).json()
    assert body["items"][0]["symbols"] == [{"market": "KR", "symbol": "005930", "method": "search"}]


async def test_topics_endpoints(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    topics = (await client.get("/v1/topics")).json()
    assert topics["version"] >= 1 and any(t["key"] == "bond" for t in topics["topics"])
    got = (await client.get("/v1/topics/for", params={"name": "TIGER 미국나스닥100"})).json()
    assert [t["key"] for t in got["topics"]] == ["nasdaq", "bigtech"]
    stats = (await client.get("/v1/topics/stats", params={"days": 7})).json()
    keys = {t["key"]: t for t in stats["topics"]}
    assert stats["total_articles"] == 9 and keys["semiconductor"]["count"] >= 1


async def test_sources_toggle_and_auth(app_ctx):
    client, svc = app_ctx
    sources = (await client.get("/v1/sources")).json()
    dart = next(s for s in sources if s["key"] == "dart")
    assert dart["configured"] is False and "DART_API_KEY" in dart["config_issue"]

    resp = await client.patch("/v1/sources/yonhap_sports", json={"enabled": False})
    assert resp.json()["enabled"] is False and resp.json()["enabled_override"] is False
    resp = await client.patch("/v1/sources/yonhap_sports", json={"enabled": None})
    assert resp.json()["enabled"] is True
    assert (await client.post("/v1/sources/dart/refresh")).status_code == 409
    assert (await client.patch("/v1/sources/none", json={"enabled": True})).status_code == 404

    svc.settings.api_token = "secret"
    assert (await client.patch("/v1/sources/yonhap_sports", json={"enabled": False})).status_code == 401
    ok = await client.patch("/v1/sources/yonhap_sports", json={"enabled": False},
                            headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


async def test_watchlists_and_aliases(app_ctx):
    client, svc = app_ctx
    resp = await client.put("/v1/watchlists/app1", json={"symbols": [
        {"symbol": "nvda", "market": "NASDAQ"}, {"symbol": "5930", "market": "KR", "name": "삼성전자"}]})
    assert [(s["market"], s["symbol"]) for s in resp.json()["symbols"]] == [("KR", "005930"), ("US", "NVDA")]
    await client.put("/v1/watchlists/app2", json={"symbols": [{"symbol": "NVDA", "market": "US"}]})
    summary = (await client.get("/v1/watchlists")).json()
    assert summary == {"clients": {"app1": 2, "app2": 1}, "union_size": 2}

    resp = await client.put("/v1/symbols/KR/005930/aliases", json={"aliases": ["삼전"]})
    assert resp.json()["aliases"] == ["삼전"]
    assert (await client.put("/v1/symbols/US/ZZZZ/aliases", json={"aliases": []})).status_code == 404
    created = await client.put("/v1/symbols/US/ZZZZ/aliases", json={"aliases": ["지지"], "name": "Zeta Corp"})
    assert created.json()["origin"] == "manual"
    found = (await client.get("/v1/symbols/search", params={"q": "삼성"})).json()
    assert found[0]["symbol"] == "005930"


async def test_health_and_stats(app_ctx):
    client, svc = app_ctx
    await seed(svc)
    health = (await client.get("/health")).json()
    assert health["status"] == "ok"
    stats = (await client.get("/v1/stats", headers={"X-Client": "tester"})).json()
    assert stats["articles"]["total"] == 9
    assert stats["storage"]["db_bytes"] > 0
    assert stats["clients"]["tester"] >= 1


async def test_console_page_and_health_auth_flag(app_ctx):
    client, svc = app_ctx
    page = await client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "NewsServer" in page.text and ".innerHTML" not in page.text  # 외부 텍스트는 DOM API 로만 렌더링
    assert (await client.get("/health")).json()["auth_required"] is False
    svc.settings.api_token = "t"
    assert (await client.get("/health")).json()["auth_required"] is True


async def test_tagging_preview(app_ctx):
    client, _ = app_ctx
    body = (await client.post("/v1/tagging/preview", json={
        "title": "삼성전자, 엔비디아에 HBM 공급", "summary": "$NVDA 주가와 원/달러 환율"})).json()
    got = {(s["symbol"], s["method"]) for s in body["symbols"]}
    assert ("005930", "dict") in got and ("NVDA", "ref") in got
    assert next(s for s in body["symbols"] if s["symbol"] == "005930")["name"] == "삼성전자"
    assert {"semiconductor", "fx"} <= {t["key"] for t in body["topics"]}


async def test_fetch_log_endpoint(app_ctx):
    client, svc = app_ctx
    async with svc.db.write() as conn:
        await conn.executemany(
            "INSERT INTO fetch_log(source_key, started_at, n_items, n_new, error) VALUES (?, ?, ?, ?, ?)",
            [("yonhap_market", "2026-09-24T00:00:00Z", 30, 5, None),
             ("yonhap_market", "2026-09-24T00:03:00Z", 3, 0, "HTTP 503"),
             ("dart", "2026-09-24T00:04:00Z", 10, 10, None)])
    rows = (await client.get("/v1/fetch-log", params={"source": "yonhap_market"})).json()
    assert [r["n_items"] for r in rows] == [3, 30]  # 최신순
    assert [r["maybe_missed"] for r in rows] == [False, False]  # 상한 도달했지만 신규 5건뿐
    async with svc.db.write() as conn:
        await conn.execute("INSERT INTO fetch_log(source_key, started_at, n_items, n_new) "
                           "VALUES ('yonhap_market', '2026-09-24T00:06:00Z', 30, 30)")
    rows = (await client.get("/v1/fetch-log", params={"source": "yonhap_market", "limit": 1})).json()
    assert rows[0]["maybe_missed"] is True  # 상한까지 전부 신규
    errs = (await client.get("/v1/fetch-log", params={"errors_only": True})).json()
    assert [r["error"] for r in errs] == ["HTTP 503"]


async def _set_collected(svc, days_ago: float, n: int, source: str) -> None:
    from newsserver.timeutil import to_iso
    ts = to_iso(utcnow() - dt.timedelta(days=days_ago))
    async with svc.db.write() as conn:
        for i in range(n):
            url = f"https://proj.test/{source}/{days_ago}/{i}"
            await conn.execute(
                "INSERT INTO articles(url, url_key, title, body_kind, source_key, market, lang, collected_at, ts, title_key) "
                "VALUES (?, ?, 't', 'summary', ?, 'KR', 'ko', ?, ?, 'k')", (url, url, source, ts, ts))


async def test_stats_projection(app_ctx):
    client, svc = app_ctx
    assert (await client.get("/v1/stats")).json()["projection"]["ready"] is False
    # 방금 첫 수집 — 과거 기사 일괄 수신 구간이라 추정하지 않는다
    await seed(svc)
    p = (await client.get("/v1/stats")).json()["projection"]
    assert p["ready"] is False and p["ready_at"]

    # 3일 전 첫 수집(과거분 500건, 워밍업이라 제외) 이후 하루 100건씩
    await _set_collected(svc, 3.0, 500, "yonhap_market")
    for d in (2.5, 1.5, 0.5):
        await _set_collected(svc, d, 100, "yonhap_market")
    p = (await client.get("/v1/stats", params={"days": 14})).json()["projection"]
    assert p["ready"] and p["bytes_basis"] == "reference"
    # 관측 구간(첫 수집+6시간 ~ 현재, 2.75일)에 300건 + 방금 seed 9건
    assert 105 <= p["articles_per_day"] <= 115
    assert abs(p["projected_db_bytes"] - p["projected_articles"] * p["bytes_per_article"]) <= p["bytes_per_article"]
