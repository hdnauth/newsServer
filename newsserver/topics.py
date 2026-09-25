"""주제 태깅 — ``config/topics.yaml`` 의 키워드 규칙을 기사 제목·요약에 적용한다.

키워드 작성 규칙
  * ``terms``: 대소문자 무시. 영숫자로 시작/끝나는 키워드는 영숫자 경계를 요구한다
    (한글 조사가 바로 붙어도 매칭: ``Apple은``).
  * ``cs_terms``: 대소문자 구분 (``AI`` 처럼 소문자일 때 다른 뜻인 약어).
  * 키워드 안의 공백은 0개 이상의 공백과 매칭 (``S&P 500`` ↔ ``S&P500``).

``version`` 을 올리면 서버가 보관 중인 전체 기사를 다시 태깅한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _term_pattern(term: str) -> str:
    body = r"\s*".join(re.escape(part) for part in term.split())
    if term[:1].isascii() and term[:1].isalnum():
        body = r"(?<![A-Za-z0-9])" + body
    if term[-1:].isascii() and term[-1:].isalnum():
        body = body + r"(?![A-Za-z0-9])"
    return body


def _compile(terms: list[str], cs_terms: list[str]) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    if terms:
        out.append(re.compile("|".join(_term_pattern(t) for t in terms), re.IGNORECASE))
    if cs_terms:
        out.append(re.compile("|".join(_term_pattern(t) for t in cs_terms)))
    return out


@dataclass
class Topic:
    key: str
    label: str
    patterns: list[re.Pattern] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


@dataclass
class NameHint:
    patterns: list[re.Pattern]
    topics: list[str]
    # 이름에 이것이 있으면 힌트를 적용하지 않는다 — 「인도」 ↔ 「인도네시아」처럼 한국어에는
    # 단어 경계가 없어 짧은 이름이 긴 이름의 앞부분과 겹친다.
    not_patterns: list[re.Pattern] = field(default_factory=list)

    def applies(self, text: str) -> bool:
        if any(p.search(text) for p in self.not_patterns):
            return False
        return any(p.search(text) for p in self.patterns)


class TopicRules:
    def __init__(self, version: int, topics: list[Topic], hints: list[NameHint]):
        self.version = version
        self.topics = topics
        self.hints = hints
        self.labels = {t.key: t.label for t in topics}

    @classmethod
    def load(cls, path: Path) -> "TopicRules":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        topics = [
            Topic(
                key=str(item["key"]),
                label=str(item.get("label") or item["key"]),
                patterns=_compile(list(item.get("terms") or []), list(item.get("cs_terms") or [])),
            )
            for item in data.get("topics") or []
        ]
        keys = {t.key for t in topics}
        hints: list[NameHint] = []
        for item in data.get("name_hints") or []:
            unknown = set(item.get("topics") or []) - keys
            if unknown:
                raise ValueError(f"topics.yaml name_hints 에 정의되지 않은 주제: {sorted(unknown)}")
            hints.append(NameHint(
                patterns=_compile(list(item.get("terms") or []), list(item.get("cs_terms") or [])),
                topics=list(item["topics"]),
                not_patterns=_compile(list(item.get("not_terms") or []), []),
            ))
        return cls(int(data.get("version") or 1), topics, hints)

    def extract(self, title: str, summary: str = "") -> list[str]:
        text = f"{title}\n{summary}"
        return [t.key for t in self.topics if t.matches(text)]

    def for_name(self, name: str, symbol: str = "") -> list[str]:
        """종목·상품 이름에서 관련 주제를 추론한다 (예: 지수 추종 상품명 → 지수 주제)."""
        text = f"{name} {symbol}".strip()
        out: list[str] = []
        for hint in self.hints:
            if hint.applies(text):
                out.extend(hint.topics)
        out.extend(self.extract(text))
        return list(dict.fromkeys(out))
