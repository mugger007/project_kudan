from __future__ import annotations

import logging

from telegram import Bot


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str, logger: logging.Logger):
        self.chat_id = chat_id
        self.logger = logger
        self._bot = Bot(token=token) if token and chat_id else None

    async def send(self, message: str) -> None:
        if not self._bot:
            self.logger.debug("Telegram disabled. Message: %s", message)
            return
        try:
            await self._bot.send_message(chat_id=self.chat_id, text=message)
        except Exception as exc:
            self.logger.warning("Telegram send failed: %s", exc)
