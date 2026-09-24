"""LLM 프롬프트·메시지용 텍스트 포맷 헬퍼."""
from __future__ import annotations

import datetime as dt
from typing import Sequence

from newsclient.models import Headline


def format_age(h: Headline, now: dt.datetime | None = None) -> str:
    """``[3h전]`` 형태의 경과 시간 (발행 시각, 없으면 수집 시각 기준)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    hours = max(0.0, (now - h.ts).total_seconds() / 3600)
    if hours < 1:
        return f"[{int(hours * 60)}분전]"
    if hours < 48:
        return f"[{hours:.0f}h전]"
    return f"[{hours / 24:.0f}일전]"


def build_news_body(headlines: Sequence[Headline], *, max_items: int = 5, with_age: bool = True,
                    now: dt.datetime | None = None) -> str:
    """여러 기사를 LLM 입력용 블록으로 합친다.

    본문이 없는 소스는 그 사실을 명시한다 — 모델이 "본문 부재"를 판단 불가 사유로
    오인하지 않게 하기 위해서다.
    """
    parts: list[str] = []
    for i, h in enumerate(headlines[:max_items], start=1):
        age = f"{format_age(h, now)} " if with_age else ""
        if h.body_kind == "metadata":
            body = f"(공시 메타데이터) {h.summary}".strip()
        elif h.summary:
            body = h.summary
        else:
            body = "(이 소스는 제목만 제공 — 본문 없음)"
        parts.append(f"[{i}] {age}{h.title}\n출처: {h.source_label or h.source}\n{body}")
    return "\n\n---\n\n".join(parts)
