from collections import defaultdict, deque


class DialogMemory:
    """Хранит последние сообщения каждого чата в оперативной памяти (сбрасывается при перезапуске)."""

    def __init__(self, limit: int) -> None:
        self._store: defaultdict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=limit))

    def get(self, chat_id: int) -> list[dict]:
        return list(self._store[chat_id])

    def add(self, chat_id: int, role: str, content: str) -> None:
        self._store[chat_id].append({"role": role, "content": content})

    def clear(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)
