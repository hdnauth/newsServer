# NewsServer API (v1)

- 기본 주소 `http://127.0.0.1:5200`, 대화형 문서 `/docs` (OpenAPI)
- 응답은 JSON, 시각은 UTC ISO-8601 (`2026-09-24T01:12:00Z`)
- `X-Client: <이름>` 헤더를 보내면 `/v1/stats` 에 클라이언트별 요청 수가 집계된다 (선택)
- `API_TOKEN` 이 설정돼 있으면 쓰기 요청(PATCH·POST·PUT·DELETE, 조회용 `POST /v1/headlines/by-symbols` 제외)에
  `Authorization: Bearer <토큰>` 또는 `X-API-Token: <토큰>` 이 필요하다
- `market` 은 `KR` | `US` | `GLOBAL`. `KRX`·`KOSPI`·`KOSDAQ`·`NYSE`·`NASDAQ` 등 별칭도 받는다
- 목록 파라미터는 콤마 구분(`topics=bond,fx`)과 반복(`topics=bond&topics=fx`) 모두 된다

---

## 헤드라인

### `GET /v1/headlines`

| 파라미터 | 기본 | 설명 |
|---|---|---|
| `symbol` | | 종목 코드/티커 (`005930`, `NVDA`). `market` 필요 |
| `market` | | 종목 조회 시 종목 시장. 종목 없이 주면 해당 시장 소스의 기사만 |
| `name`, `alias` | | 종목 회사명·별칭 (반복 가능). 사전에 없거나 보강할 이름 |
| `hours` | | 최근 N시간 |
| `since`, `until` | | 시각 범위 (ISO-8601) |
| `since_id` | | 증분 커서 — 이 id 초과를 id 오름차순으로 |
| `limit` | 50 | 1–500 |
| `topics` | | 주제 키 (`GET /v1/topics`) |
| `topics_mode` | `any` | `any` \| `all` |
| `sources` | | 이 피드들에 실린 기사만 |
| `exclude_sources` | | 이 피드들에**만** 실린 기사 제외 |
| `category` | | 소스 카테고리 (`economy`, `market`, `sports` …) |
| `lang` | | `ko` \| `en` |
| `kind` | `all` | `all` \| `news` \| `filing` |
| `body` | `any` | `with_summary` 면 요약이 있는 기사만 |
| `q` | | 전문 검색 (제목·요약 부분 일치, 2자 이상) |
| `match` | `any` | 종목 매칭 경로 `any` \| `tag` \| `text` |
| `match_summary` | `true` | 종목 텍스트 매칭에 요약 포함 |
| `filings` | `auto` | 종목 조회 시 공시: `auto`(태그로 특정된 공시만) \| `include` \| `exclude` |
| `market_filter` | `true` | 종목 텍스트 매칭을 같은 시장 소스로 제한 |
| `time_basis` | `ts` | 시간 조건·정렬 기준 `ts`(발행, 없으면 수집) \| `collected` |
| `dedup` | `none` | `title` 이면 제목이 같은 교차 소스 기사를 한 건으로 |
| `refresh` | `false` | 종목 조회 전에 종목별 소스를 즉시 수집 (소스·종목마다 `REFRESH_COOLDOWN_SEC` 쿨다운) |

응답

```json
{
  "items": [
    {
      "id": 184233,
      "title": "삼성전자, HBM4 12단 양산 돌입",
      "url": "https://…",
      "summary": "…",
      "body_kind": "summary",
      "source": {"key": "yonhap_market", "label": "연합뉴스 증권", "kind": "rss", "market": "KR",
                 "lang": "ko", "category": "market", "body_kind": "summary", "is_filing": false},
      "feeds": ["yonhap_market", "yonhap_economy"],
      "market": "KR", "lang": "ko", "category": "market", "is_filing": false,
      "published_at": "2026-09-24T01:12:00Z",
      "collected_at": "2026-09-24T01:15:31Z",
      "ts": "2026-09-24T01:12:00Z",
      "symbols": [{"market": "KR", "symbol": "005930", "method": "dict"}],
      "topics": ["semiconductor"],
      "extra": {},
      "match": "tag"
    }
  ],
  "next_since_id": 184233,
  "query": {"symbol": "005930", "market": "KR", "hours": 24.0, "limit": 20,
            "terms": ["005930", "삼성전자", "Samsung Electronics"]},
  "refresh": null
}
```

- `body_kind`: `summary`(요약 있음) · `title_only`(소스가 제목만 제공) · `metadata`(공시 메타데이터)
- `feeds`: 이 기사가 실린 모든 피드. `source` 는 처음 수집한 피드
- `symbols[].method`: `source_tag` · `dart_code` · `edgar_cik` · `search` · `ref` · `dict` ([DESIGN.md §3](DESIGN.md))
- `match`: 종목 조회에서 태그 경로(`tag`)로 찾았는지 텍스트 경로(`text`)로 찾았는지
- `extra`: 소스별 원자료 (DART: `rcept_no`, `rcept_dt`, `corp_code`, `stock_code`, `report_nm` … / EDGAR: `form`, `cik`, `company` / 검색 소스: `publisher`)
- `refresh`: `refresh=true` 일 때 소스별 결과 (`ok:<신규 건수>` · `cooldown` · `timeout` · `error`)

예시

```bash
# 종목 — 최근 24시간
curl 'localhost:5200/v1/headlines?symbol=005930&market=KR&hours=24&limit=20'
curl 'localhost:5200/v1/headlines?symbol=NVDA&market=US&name=NVIDIA%20Corporation&hours=16'
# 주제
curl 'localhost:5200/v1/headlines?topics=bond,fx&hours=72'
# 시장별 뉴스 풀 (공시 제외)
curl 'localhost:5200/v1/headlines?market=US&kind=news&hours=24&limit=250'
# 카테고리
curl 'localhost:5200/v1/headlines?category=sports&lang=ko&limit=10'
# 전문 검색
curl 'localhost:5200/v1/headlines?q=HBM&hours=48'
# 증분
curl 'localhost:5200/v1/headlines?since_id=184233&limit=500'
```

### `POST /v1/headlines/by-symbols`

여러 종목을 한 번에 조회한다 (최대 200개).

```json
{"symbols": [{"symbol": "NVDA", "market": "US", "names": ["NVIDIA Corporation"]},
             {"symbol": "005930", "market": "KR"}],
 "hours": 16, "limit_per_symbol": 8,
 "match": "any", "match_summary": true, "filings": "auto", "kind": "all", "time_basis": "ts", "dedup": "none"}
```

→ `{"results": {"US:NVDA": [Headline…], "KR:005930": [Headline…]}}`

### `GET /v1/articles/{id}`

기사 한 건 (Headline 형식). 없으면 404.

---

## 주제

| 엔드포인트 | 설명 |
|---|---|
| `GET /v1/topics` | `{"version": 1, "topics": [{"key": "bond", "label": "채권·금리"}, …]}` |
| `GET /v1/topics/for?name=&symbol=` | 종목·상품 이름에서 관련 주제 추론. `name=KODEX 미국나스닥100` → `nasdaq`, `bigtech` |
| `GET /v1/topics/stats?days=30&market=` | 기간 내 주제별 기사 수. `share_pct` = 전체 주제 태그 중 비중, `article_pct` = 기사 중 비중 |

---

## 소스

| 엔드포인트 | 설명 |
|---|---|
| `GET /v1/sources` | 소스 목록과 상태: `enabled`, `configured`/`config_issue`(자격 증명 누락 등), `last_success_at`, `last_new_at`, `last_error`, `fail_streak`, `next_poll_at`, `articles_24h`, `articles_total` |
| `PATCH /v1/sources/{key}` | `{"enabled": false}` 로 끄기, `true` 로 켜기, `null` 이면 `sources.yaml` 기본값으로 |
| `POST /v1/sources/{key}/refresh` | 즉시 수집 → `{"ok": true, "received": 30, "new": 4, "updated": 1}`. 수집 중이면 409, 미설정 소스면 409 |

---

## 관심종목 (종목별 수집 대상)

종목별 소스(`per_symbol: true`)는 모든 클라이언트 관심종목의 합집합을 주기적으로 수집한다.

| 엔드포인트 | 설명 |
|---|---|
| `PUT /v1/watchlists/{client}` | `{"symbols": [{"symbol": "AAPL", "market": "US", "name": "Apple", "aliases": []}]}` — 목록 통째로 교체 |
| `GET /v1/watchlists/{client}` | 클라이언트 관심종목 |
| `GET /v1/watchlists` | `{"clients": {"app": 12}, "union_size": 20}` |
| `DELETE /v1/watchlists/{client}` | 삭제 |

`client` 는 영숫자·`_.-` 64자 이내.

---

## 심볼 사전

| 엔드포인트 | 설명 |
|---|---|
| `GET /v1/symbols/search?q=&market=&limit=` | 코드·이름·별칭 부분 일치 |
| `GET /v1/symbols/{market}/{symbol}` | 사전 항목 (`name`, `name_en`, `aliases`, `cik`, `origin`) |
| `PUT /v1/symbols/{market}/{symbol}/aliases` | `{"aliases": ["삼전"]}` — 별칭 교체. 사전에 없는 종목은 `"name"` 과 함께 보내면 등록. 이후 수집 태깅과 모든 조회 매칭에 즉시 반영 |
| `POST /v1/symbols/refresh` | SEC·DART 원격 사전을 백그라운드로 재수신 |

---

## 상태·통계

### `GET /health`

```json
{"version": "0.1.0", "status": "ok", "problems": [], "sources_active": 23,
 "sources_failing": [], "sources_stale": []}
```

`status` 는 `ok` | `degraded`. HTTP 상태 코드는 항상 200.

### `GET /v1/stats?days=14`

기사 총수·가장 오래된/최신 시각, DB·WAL·백업 크기, 기사당 바이트, 일별·소스별 유입량, 클라이언트별 요청 수.
보관 기간 판단에 쓴다.
