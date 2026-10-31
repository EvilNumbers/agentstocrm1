"""Небольшой буфер в RAM. Ошибки и изменяющие действия не сохраняются."""
from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Coroutine
from typing import Any


class AsyncBuffer:
    """Single-flight на точный ключ, необязательный TTL успешного результата.

    LLM использует TTL=0: ответ после завершения не переиспользуется.
    STT хранит только текст, не аудио. Данные никогда не пишутся на диск.
    """

    def __init__(self, capacity: int = 32) -> None:
        self.capacity = capacity
        self._values: dict[str, tuple[float, Any]] = {}
        self._pending: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task[Any]] = {}
        self.hits = 0
        self.joined = 0
        self.misses = 0

    async def run(self, key: str, factory: Callable[[], Coroutine[Any, Any, Any]], *, ttl: float = 0.0) -> Any:
        now = time.monotonic()
        for old in list(self._values):
            if self._values[old][0] <= now:
                self._values.pop(old, None)
        hit = self._values.get(key)
        if ttl > 0 and hit and hit[0] > now:
            self.hits += 1
            return copy.deepcopy(hit[1])
        ident = (asyncio.get_running_loop(), key)
        task = self._pending.get(ident)
        if task is not None:
            self.joined += 1
        else:
            self.misses += 1
            if len(self._pending) >= self.capacity:
                return await factory()
            task = asyncio.create_task(factory())
            self._pending[ident] = task

            def finished(done: asyncio.Task[Any]) -> None:
                self._pending.pop(ident, None)
                if done.cancelled():
                    return
                # Забираем исключение даже если все ожидавшие были отменены.
                if done.exception() is not None:
                    return
                value = done.result()
                if ttl > 0:
                    self._values[key] = (time.monotonic() + min(ttl, 300.0), copy.deepcopy(value))
                    while len(self._values) > self.capacity:
                        self._values.pop(next(iter(self._values)))

            task.add_done_callback(finished)
        return copy.deepcopy(await asyncio.shield(task))

    def stats(self) -> dict[str, int]:
        return {"hit": self.hits, "joined": self.joined, "miss": self.misses,
                "entries": len(self._values), "inflight": len(self._pending)}
