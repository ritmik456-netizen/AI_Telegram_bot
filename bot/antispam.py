import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.account import PROFILE_BUTTON, TOPUP_BUTTON
from bot.calculator import looks_like_expression

COOLDOWN_SECONDS = 3
EXPENSIVE_COMMANDS = {"solve", "advice", "image", "img", "translate"}


def is_expensive(message: Message) -> bool:
    """Запросы, которые тратят нейросеть или процессор. Калькулятор, меню и оплата сюда не входят."""
    if message.photo or message.voice or message.video_note or message.audio or message.document:
        return True
    text = message.text
    if not text or text in (PROFILE_BUTTON, TOPUP_BUTTON) or looks_like_expression(text):
        return False
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0][1:].split("@")[0].lower()
        return command in EXPENSIVE_COMMANDS
    return True


class AntiSpamMiddleware(BaseMiddleware):
    """Один тяжёлый запрос за раз и пауза COOLDOWN_SECONDS после ответа."""

    def __init__(self) -> None:
        self._busy: set[int] = set()
        self._finished_at: dict[int, float] = {}
        self._warned: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.from_user is None or not is_expensive(event):
            return await handler(event, data)

        user_id = event.from_user.id
        wait = COOLDOWN_SECONDS - (time.monotonic() - self._finished_at.get(user_id, 0))
        if user_id in self._busy or wait > 0:
            if user_id not in self._warned:
                self._warned.add(user_id)
                if user_id in self._busy:
                    await event.answer("⏳ Я ещё отвечаю на предыдущее сообщение, подождите немного.")
                else:
                    await event.answer(f"⏳ Не так быстро! Следующий запрос можно через {max(1, round(wait))} сек.")
            return None

        self._warned.discard(user_id)
        self._busy.add(user_id)
        try:
            return await handler(event, data)
        finally:
            self._busy.discard(user_id)
            self._finished_at[user_id] = time.monotonic()
