"""OpenDART 공시 목록 수집기 (``kind: dart``).

API: https://opendart.fss.or.kr/api/list.json — 당일 공시를 최신순으로 페이지 단위 조회.

접수일(``rcept_dt``)만 있고 시각이 없으므로 ``published_at`` 은 비워 두고(정렬·시간창은
수집 시각 기준) 접수일은 ``extra.rcept_dt`` 로 보존한다. 날짜 00:00 을 발행 시각으로 두면
장중 공시가 시간창 조회에서 전부 빠지고, "마지막 확인 시각 이후" 같은 필터에도 걸린다.

옵션: page_count (기본 100), max_pages (기본 10)
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from loguru import logger

from newsserver.collectors.base import Collector, CollectorError, FetchResult, RawItem, SymbolTag
from newsserver.markets import normalize_symbol
from newsserver.textutil import clean_text

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
KST = ZoneInfo("Asia/Seoul")

_CORP_CLS = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}


class DartCollector(Collector):
    kind = "dart"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_day: str = ""
        self._seen: set[str] = set()

    def missing_config(self) -> str | None:
        return None if self.settings.dart_api_key else "DART_API_KEY 미설정"

    async def fetch(self, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        today = dt.datetime.now(KST).strftime("%Y%m%d")
        if today != self._seen_day:
            self._seen_day, self._seen = today, set()

        page_count = int(self.spec.options.get("page_count", 100))
        max_pages = int(self.spec.options.get("max_pages", 10))
        items: list[RawItem] = []
        status = None

        for page in range(1, max_pages + 1):
            resp = await self._get(DART_LIST_URL, params={
                "crtfc_key": self.settings.dart_api_key,
                "bgn_de": today, "end_de": today,
                "page_no": page, "page_count": page_count,
                "sort": "date", "sort_mth": "desc",
            })
            status = resp.status_code
            data = resp.json()
            code = data.get("status")
            if code == "013":  # 조회된 데이터 없음
                break
            if code != "000":
                raise CollectorError(f"DART {code}: {data.get('message', '')}", status)

            new_on_page = 0
            for row in data.get("list") or []:
                rcept_no = str(row.get("rcept_no") or "")
                if not rcept_no or rcept_no in self._seen:
                    continue
                items.append(self._to_item(row))
                new_on_page += 1

            # 최신순이므로 한 페이지가 전부 이미 본 공시면 그 뒤도 본 것이다
            if new_on_page == 0 or page >= int(data.get("total_page") or 1):
                break
        else:
            logger.warning("DART: {}페이지 상한 도달 — 일부 공시를 놓쳤을 수 있습니다", max_pages)

        self._seen.update(item.extra["rcept_no"] for item in items)
        return FetchResult(items=items, http_status=status)

    def _to_item(self, row: dict) -> RawItem:
        corp_name = clean_text(row.get("corp_name"))
        report_nm = clean_text(row.get("report_nm"))
        rcept_no = str(row.get("rcept_no"))
        corp_cls = str(row.get("corp_cls") or "")
        stock_code = str(row.get("stock_code") or "").strip()
        filer = clean_text(row.get("flr_nm"))
        remark = clean_text(row.get("rm"))

        parts = [f"법인구분: {_CORP_CLS.get(corp_cls, corp_cls)}", f"제출인: {filer}"]
        if remark:
            parts.append(f"비고: {remark}")

        symbols = [SymbolTag("KR", normalize_symbol(stock_code, "KR"), "dart_code")] if stock_code else []
        return RawItem(
            title=f"[{corp_name}] {report_nm}",
            url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
            summary=" | ".join(parts),
            published_at=None,
            symbols=symbols,
            extra={
                "rcept_no": rcept_no,
                "rcept_dt": row.get("rcept_dt"),
                "corp_code": row.get("corp_code"),
                "corp_name": corp_name,
                "corp_cls": corp_cls,
                "stock_code": stock_code,
                "report_nm": report_nm,
                "filer": filer,
                "remark": remark,
            },
        )
