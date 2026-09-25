# NewsServer

뉴스·공시를 **한 번만 수집**해 저장하고 HTTP로 제공하는 로컬 서버.
여러 애플리케이션이 같은 피드를 각자 폴링하는 대신 이 서버 하나를 조회한다.

- 수집: RSS/Atom, OpenDART 공시, SEC EDGAR, 관심종목별 검색 소스 — `config/sources.yaml` 로 관리
- 조회: 종목·주제·카테고리·시장·전문 검색·증분 커서
- 종목 연결: 소스 태그·공시 종목코드·CIK·사전(회사명·별칭) 태깅 + 조회 시 텍스트 매칭
- 저장: SQLite(WAL) + FTS5, 기본 1년 보관, 일일 gzip 백업
- 재무제표: KR(DART)·US(SEC) 재무제표를 저장하지 않고 요청 시 읽어 정규화된 항목으로 제공 (메모리 캐시만)
- 운영 콘솔: `http://<서버>:5200/` — 소스 상태·조회 테스터·수집량·태깅/별칭 (빌드 없는 단일 페이지)
- 스택: Python 3.12, FastAPI, aiosqlite, httpx, feedparser

문서: [설계](docs/DESIGN.md) · [API](docs/API.md)

---

## 빠른 시작

```bash
cp .env.example .env          # 공시 소스를 쓰려면 DART_API_KEY, EDGAR_USER_AGENT 설정
./run_newsServer.sh           # .venv 생성·설치·기동, http://127.0.0.1:5200
curl localhost:5200/health
curl 'localhost:5200/v1/headlines?symbol=005930&market=KR&hours=24'
./stop_newsServer.sh
```

개발용 직접 실행

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]" -e client
.venv/bin/uvicorn --factory newsserver.main:app_factory --port 5200 --reload
.venv/bin/pytest
```

처음 기동하면 모든 소스를 즉시 한 번 수집하고, 자격 증명이 있으면 SEC·DART 심볼 사전을 내려받는다(수 초).

### LAN 공개

기본은 이 머신에서만 접근(`127.0.0.1`)이다. 다른 기기에서 API·콘솔을 쓰려면 `.env` 에서

```bash
SERVER_HOST=0.0.0.0
API_TOKEN=<무작위 문자열>     # 쓰기 요청 보호. 조회는 토큰 없이 가능
```

으로 바꾸고 재시작한다. 인증은 공유 토큰 하나뿐이므로 신뢰할 수 있는 내부망에서만 공개한다.

---

## 설정

### `.env`

| 변수 | 기본 | 설명 |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `5200` | 바인딩 주소 |
| `DEFAULT_RETENTION_DAYS` | `365` | 소스별 `retention_days` 가 없을 때 보관 기간 |
| `BACKUP_KEEP` | `3` | 일일 gzip 백업 보관 개수 (0 이면 백업 안 함) |
| `DART_API_KEY` | | OpenDART 인증키. 없으면 DART 소스와 한국 사전 갱신을 건너뜀 |
| `EDGAR_USER_AGENT` | | SEC 요구 User-Agent (`"이름 email@example.com"`). 없으면 EDGAR 소스와 미국 사전 갱신을 건너뜀 |
| `API_TOKEN` | | 설정하면 쓰기 요청에 토큰 필요 (LAN 공개 시 권장) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | 상태 이상·복구 경보 |
| `MAX_CONCURRENT_FETCHES` | `4` | 동시 수집 수 |
| `REFRESH_COOLDOWN_SEC` | `300` | 종목별 즉시 수집(`refresh=true`) 쿨다운 |
| `FINANCIALS_CACHE_TTL_SEC` / `FINANCIALS_CACHE_MAX_ENTRIES` | `43200` / `512` | 재무제표 원천 응답의 메모리 캐시 (디스크에 저장하지 않음) |
| `MAINTENANCE_HOUR` / `LOCAL_TZ` | `3` / `Asia/Seoul` | 일일 유지보수 시각 |
| `LOG_LEVEL` | `INFO` | |

전체 목록은 [`newsserver/config.py`](newsserver/config.py).

### 소스 — `config/sources.yaml`

```yaml
- {key: yonhap_economy, kind: rss, url: "https://www.yna.co.kr/rss/economy.xml",
   label: 연합뉴스 경제, market: KR, lang: ko, category: economy, body_kind: summary}
```

| 필드 | 설명 |
|---|---|
| `kind` | `rss` · `rss_search`(종목별 검색 피드, url 에 `{query}`) · `dart` · `edgar` · `yahoo_symbol`(종목 뉴스 스트림, 요약 포함) |
| `market` | `KR` · `US` · `GLOBAL` — 종목 텍스트 매칭 시 같은 시장 소스로 제한할 때 쓴다 |
| `body_kind` | `summary` · `title_only` · `metadata` |
| `schedule` | `fixed`(`interval_sec`, 생략 시 기본) · `market_aware`(시장 시간에 따라 180s/30m/60m, 종목별 소스는 대상 종목 시장 기준·`market_intervals` 필수) |
| `naive_tz` | 타임존 표기 없는 발행 시각의 시간대 (예: `Asia/Seoul`) |
| `retention_days` | 소스별 보관 기간 — 여러 피드에 실린 기사는 그중 가장 긴 기간 |
| `per_symbol` / `symbol_markets` | 관심종목별 수집 여부와 대상 시장 |
| `enabled` | 기본 활성 여부 (런타임에는 `PATCH /v1/sources/{key}`) |
| `options` | 수집기별 옵션 — 각 `collectors/*.py` 상단 설명 참고 |

편집 후 서버를 재시작하면 반영된다. 기본 구성: 한국·미국 금융·기술·국제 RSS 19종, 일반 뉴스 6종(전체·정치·사회·세계·연예·스포츠), DART, EDGAR 8-K,
Yahoo 종목 뉴스(미국, 요약 포함), Google 뉴스 종목 검색(한국, 기본 비활성).

### 주제 — `config/topics.yaml`

키워드 규칙으로 주제를 붙인다. 규칙을 바꾸면 `version` 을 올린다 — 재시작 시 보관 중인 기사를 재태깅한다.
`name_hints` 는 상품 이름 → 주제 추론(`/v1/topics/for`)용이며, `not_terms` 로 겹치는 이름을 제외한다(예: 「인도」 힌트에서 「인도네시아」 제외).

### 심볼 — `config/symbols/`

- `aliases.yaml`: 수동 별칭 (한글 표기, 약칭). 사전에 없는 종목은 `name` 과 함께 등록
- `stopnames.txt`: 일반 명사와 겹쳐 사전 자동 태깅에서 뺄 회사명

---

## API 요약

| | |
|---|---|
| `GET /v1/headlines` | 헤드라인 조회 (`symbol`+`market`, `topics`, `category`, `q`, `hours`, `since_id` …) |
| `POST /v1/headlines/by-symbols` | 여러 종목 한 번에 |
| `GET /v1/articles/{id}` | 기사 한 건 |
| `GET /v1/topics` · `/v1/topics/for` · `/v1/topics/stats` | 주제 목록 · 이름→주제 · 주제 분포 |
| `GET/PATCH /v1/sources` · `POST /v1/sources/{key}/refresh` | 소스 상태 · 토글 · 즉시 수집 |
| `PUT/GET/DELETE /v1/watchlists/{client}` | 종목별 수집 대상 |
| `GET /v1/symbols/search` · `PUT /v1/symbols/{m}/{s}/aliases` | 심볼 사전 |
| `GET /v1/financials/{market}/{symbol}` · `/v1/financials/items` | 재무제표 (분기·연간, 정규화 항목) · 항목 정의 |
| `POST /v1/tagging/preview` | 텍스트에 태깅 규칙 적용 결과 (저장 안 함) |
| `GET /v1/fetch-log` | 수집 이력 |
| `GET /health` · `GET /v1/stats` | 상태 · 수집량·용량 통계·용량 예측 |

상세: [docs/API.md](docs/API.md), 실행 중 `http://127.0.0.1:5200/docs`.

---

## 운영 콘솔

브라우저로 `http://<서버>:5200/` 을 연다. 조회는 누구나, 켜기/끄기·즉시 수집·별칭 저장은 API 토큰이 필요하다
(우측 상단 🔑 — 브라우저에만 저장).

| 탭 | 내용 |
|---|---|
| 소스 상태 | 소스별 상태(정상·지연·재시도·실패·미설정·꺼짐), 마지막 성공·신규, 24시간·누적 건수, 다음 수집, 켜기/끄기, 즉시 수집, 최근 수집 이력과 오류 |
| 조회 테스터 | `/v1/headlines` 조건 폼. 결과마다 매칭 경로(태그/텍스트)·종목 태그 근거·주제·실린 피드, 실제 검색 키워드, 요청 URL 복사 |
| 수집량·용량 | 보관 기사 수, DB·백업 크기, 일평균 신규, 보관 기간이 찼을 때 예상 용량, 일별·소스별 유입 |
| 태깅·별칭 | 텍스트를 넣어 종목·주제 태깅 결과 미리보기, 심볼 사전 검색, 별칭 편집 |

---

## 파이썬 클라이언트

```bash
pip install -e /path/to/newsServer/client     # 의존성: httpx
```

```python
from newsclient import NewsClient, SyncNewsClient, build_news_body

async with NewsClient(client="myapp") as nc:            # 기본 http://127.0.0.1:5200
    items = await nc.symbol_headlines("NVDA", "US", hours=24, names=["NVIDIA Corporation"])
    macro = await nc.topic_headlines(["bond", "fx"], hours=72)
    cursor = await nc.latest_id()                       # 증분 소비 시작점
    new, cursor = await nc.since(cursor)                # 이후 새 기사만
    await nc.set_watchlist("myapp", [("AAPL", "US"), ("005930", "KR")])
    prompt_block = build_news_body(items, max_items=5)  # LLM 입력용 텍스트
    fin = await nc.financials("KR", "005930", period="quarter", limit=8)  # 재무제표 (실패 시 None)

with SyncNewsClient(client="script") as nc:
    items = nc.headlines(category="sports", limit=10)
```

기본은 fail-soft: 서버 장애 시 예외 대신 빈 결과(`[]`, 커서 유지)와 경고 로그. 예외가 필요하면 `raise_errors=True`.
`financials()` 는 실패하면 `None` 을 돌려준다 — 데이터가 없는 것(`periods: []`)과 구분하기 위해서다.

---

## 운영

- 상시 실행: `run_newsServer.sh` / `stop_newsServer.sh` (PID 파일 `.pids/server.pid`, 포트 폴백 종료).
  서비스 매니저 예시:
  ```json
  {"name": "NewsServer", "run_script": "~/work/newsServer/run_newsServer.sh",
   "stop_script": "~/work/newsServer/stop_newsServer.sh", "workdir": "~/work/newsServer",
   "pid_file": "~/work/newsServer/.pids/server.pid",
   "log_files": ["~/work/newsServer/data/logs/server.log"], "autostart": true}
  ```
- 데이터: `data/news.db`, 백업 `data/backups/news-YYYY-MM-DD.db.gz`, 로그 `data/logs/`
- 복구: `gunzip -c data/backups/news-….db.gz > data/news.db` (서버 정지 후)
- 용량: 1년 110만 건 기준 DB 약 1.8 GB + 백업 3세대 약 2 GB ([DESIGN.md §8](docs/DESIGN.md#8-저장-용량과-보관-기간))

---

## 디렉터리

```
newsserver/          서버 패키지 (collectors/, api/, storage/, web/, pipeline, query, scheduler, symbols, topics …)
client/newsclient/   클라이언트 패키지
config/              sources.yaml, topics.yaml, symbols/
docs/                DESIGN.md, API.md
tests/               pytest
```
