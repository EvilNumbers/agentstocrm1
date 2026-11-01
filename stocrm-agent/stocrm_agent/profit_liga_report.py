"""Подробности ошибок Профит Лиги только в локальных файлах; не для отправки в чат."""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

from .envfile import find_env_file

REPORT_DIR = "profit_liga_local_reports"


@dataclass
class SearchTrace:
    article: str = ""
    status: int | None = None
    content_type: str = ""
    body: bytearray = field(default_factory=bytearray)
    body_complete: bool = False
    truncated: bool = False
    exception_type: str = ""
    exception_text: str = ""

    def exception(self, exc: BaseException) -> None:
        self.exception_type = type(exc).__name__
        self.exception_text = str(exc)[:16000]
        if not self.body_complete and (self.status is not None or self.body):
            self.truncated = True


def _mask_key(text: str, key: str) -> str:
    """Скрывает известный ключ и распространённые представления; не полный санитайзер."""
    variants = {key, quote(key, safe=""), quote_plus(key, safe=""),
                html.escape(key, quote=True), html.escape(key, quote=False),
                json.dumps(key, ensure_ascii=True)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1]}
    for value in sorted((v for v in variants if v), key=len, reverse=True):
        text = re.sub(re.escape(value), lambda _: "[СКРЫТ КЛЮЧ]", text, flags=re.IGNORECASE)
    # Исключение может содержать адрес запроса. Значение параметра secret не сохраняем.
    return re.sub(r"(?i)(secret(?:=|%3d))[^&\s'\"<>]+", r"\1[СКРЫТ КЛЮЧ]", text)


def _report_dir() -> Path:
    target = find_env_file()
    return (target.parent if target is not None else Path.cwd()) / REPORT_DIR


def write_report(trace: SearchTrace, error: str, key: str) -> Path:
    folder = _report_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with (folder / ".gitignore").open("x", encoding="ascii") as ignore:
            ignore.write("*\n")
    except OSError:
        pass
    body = bytes(trace.body).decode("utf-8-sig", errors="backslashreplace")
    report = (
        "ЛОКАЛЬНЫЙ ОТЧЁТ ПРОФИТ ЛИГИ — НЕ ОТПРАВЛЯТЬ БЕЗ ПРОВЕРКИ\n"
        "Может содержать личные данные и секреты, в том числе части ключа.\n"
        "Точный ключ и некоторые представления скрываются по возможности, но не гарантированно.\n"
        "Программа не добавляет запрос или .env, но сервер и текст ошибки могут содержать личные данные.\n\n"
        f"Время ПК: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        "Метод: GET /search/products (только поиск, не заказ)\n"
        f"Артикул: {trace.article}\n"
        f"Результат программы: {error}\n"
        f"HTTP статус: {trace.status if trace.status is not None else '[нет ответа]'}\n"
        f"Content-Type: {trace.content_type or '[не указан]'}\n"
        f"Ошибка: {trace.exception_type or '[нет]'}\n"
        f"Подробности ошибки: {trace.exception_text or '[нет]'}\n"
        f"Тело прочитано полностью: {'да' if trace.body_complete else 'нет'}\n"
        f"Тело ограничено/прервано: {'да' if trace.truncated else 'нет'}\n\n"
        "=== Ответ сервера (UTF-8, может содержать личные данные) ===\n"
        + (body if body else "[тело не получено или пустое]") + "\n"
    )
    report = _mask_key(report, key)
    fd, filename = tempfile.mkstemp(prefix="profit-liga-error-", suffix=".txt", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\n") as file:
            fd = -1
            file.write(report)
        return Path(filename)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(filename)
        except OSError:
            pass
        raise


def open_reports() -> int:
    """Открыть папку отчётов на Windows; запросы и изменение файлов не выполняются."""
    try:
        folder = _report_dir().resolve()
        if not folder.is_dir():
            print("Подробных отчётов Профит Лиги пока нет. При ошибке поиска они появятся здесь:")
            print(folder)
            return 1
        print("Локальная папка с отчётами:", folder)
        print("Открывайте файлы только на своём компьютере. Они могут содержать ключи и личные данные.")
        opener = getattr(os, "startfile", None)
        if opener is None:
            print("Откройте эту папку вручную.")
            return 1
        opener(str(folder))
        return 0
    except (OSError, ValueError):
        print("Не удалось открыть папку отчётов. Проверьте доступ к папке с настройками.")
        return 1


if __name__ == "__main__":
    raise SystemExit(open_reports())
