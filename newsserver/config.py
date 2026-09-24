"""환경 설정 (.env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    server_host: str = "127.0.0.1"
    server_port: int = 5200

    db_path: Path = ROOT / "data" / "news.db"
    log_dir: Path = ROOT / "data" / "logs"
    backup_dir: Path = ROOT / "data" / "backups"
    backup_keep: int = 7

    sources_file: Path = ROOT / "config" / "sources.yaml"
    topics_file: Path = ROOT / "config" / "topics.yaml"
    aliases_file: Path = ROOT / "config" / "symbols" / "aliases.yaml"
    stopnames_file: Path = ROOT / "config" / "symbols" / "stopnames.txt"
    # 영어 일반명사와 겹치는 회사명(Target, Apple 등)을 사전 태깅에서 제외할 때 쓰는 단어 목록.
    # 파일이 없으면 이 검사는 생략된다.
    wordlist_file: Path = Path("/usr/share/dict/words")

    # 보관 기간 기본값 (소스별 retention_days 가 우선)
    default_retention_days: int = 365
    fetch_log_retention_days: int = 7

    # 유지보수 작업(보관 기간 정리·백업·사전 갱신) 기준 시간대와 시각
    local_tz: str = "Asia/Seoul"
    maintenance_hour: int = 3
    symbol_refresh_days: int = 7

    http_user_agent: str = "Mozilla/5.0 (compatible; NewsServer/0.1; +local)"
    http_timeout_sec: float = 15.0
    max_concurrent_fetches: int = 4

    # 공시 소스 자격 증명 — 비어 있으면 해당 소스는 '미설정' 상태로 건너뛴다
    dart_api_key: str = ""
    # SEC 는 연락처가 포함된 User-Agent 를 요구한다. 예: "MyOrg admin@example.com"
    edgar_user_agent: str = ""

    # 쓰기 엔드포인트(PATCH/POST/PUT) 보호용. 비어 있으면 인증 없이 허용한다.
    api_token: str = ""

    # 상태 경보 (선택)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    scheduler_enabled: bool = True
    symbol_dict_tagging: bool = True
    refresh_cooldown_sec: int = 300
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
