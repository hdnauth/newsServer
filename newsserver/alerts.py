"""상태 경보 (선택). 텔레그램 설정이 없으면 로그만 남긴다."""
from __future__ import annotations

import httpx
from loguru import logger

from newsserver.config import Settings


class Notifier:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    async def send(self, text: str) -> None:
        logger.warning("[경보] {}", text)
        if not self.enabled:
            return
        try:
            await self.http.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={"chat_id": self.settings.telegram_chat_id, "text": f"[NewsServer] {text}"},
                timeout=10,
            )
        except httpx.HTTPError as e:
            logger.warning("텔레그램 전송 실패: {}", e)
