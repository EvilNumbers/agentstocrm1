"""Аудит-лог всех действий агента (JSONL). Нужен, чтобы понимать, что именно
делал ИИ в CRM: кто, когда, какой метод API, с какими аргументами и что ответил."""
from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

_SENSITIVE = {"SID", "PASSWORD", "TOKEN", "API_KEY"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        # ключи бывают числами (ответы панели с ID) — str() обязателен, иначе аудит падал
        return {k: ("***" if str(k).upper() in _SENSITIVE else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        from .diagnostics import TRACE_ID
        trace = TRACE_ID.get()
        if fields.get("token"):
            fields["draft_fingerprint"] = hashlib.sha256(str(fields["token"]).encode()).hexdigest()[:12]
        record = {"ts": time.time(), "event": event, **({"trace_id": trace} if trace else {}), **_redact(fields)}
        # default=str: аудит не должен ронять уже выполненное действие
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:          # диск заполнен/нет прав — но действие уже выполнено
            logging.getLogger("stocrm.audit").warning("не смог записать аудит: %s", exc)

    def _last_lines(self, n: int, block: int = 64 * 1024) -> list[str]:
        """Читает последние строки, не поднимая в память весь файл."""
        with self.path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return data.decode("utf-8", errors="replace").splitlines()[-n:]

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if n <= 0:
            return []
        lines = self._last_lines(n)
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
