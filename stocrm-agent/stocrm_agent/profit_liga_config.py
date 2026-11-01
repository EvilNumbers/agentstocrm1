"""Локальная настройка поиска Профит Лиги; без сетевых запросов."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Callable

from .envfile import _parse, find_env_file
from .rossko_config import ConfigurationError, _env_key

_PATTERN = re.compile(r"^\s*(?:export\s+)?(PROFIT_LIGA_ENABLED|PROFIT_LIGA_KEY)\s*=")


def configure_file(path: Path, key: str) -> None:
    values = {"PROFIT_LIGA_ENABLED": "1", "PROFIT_LIGA_KEY": _env_key(key)}
    if not path.is_file():
        raise ConfigurationError("Файл .env не найден. Настройки не изменены.")
    raw = path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    newline = "\r\n" if b"\r\n" in raw else "\n"
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = _PATTERN.match(line)
        if not match:
            out.append(line)
            continue
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            end = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "\r" if line.endswith("\r") else ""
            out.append(f"{name}={values[name]}{end}")
    result = "".join(out)
    missing = [name for name in values if name not in seen]
    if missing and result and not result.endswith(("\r", "\n")):
        result += newline
    result += "".join(f"{name}={values[name]}{newline}" for name in missing)
    encoded = result.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".profit-env-", dir=path.parent)
    try:
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != raw:
            raise ConfigurationError("Файл .env изменился во время настройки; ничего не перезаписано.")
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(*, path: Path | None = None, ask: Callable[[str], str] = input) -> int:
    print("Профит Лига: настройка только поиска брендов. Сеть и заказы не используются.")
    print("Остановите бота. Вводимый ключ ВИДЕН на экране: не показывайте его и не отправляйте в чат.")
    target = path or find_env_file()
    if target is None or not target.is_file():
        print("Файл .env не найден. Сначала перенесите свои настройки или завершите setup.bat.")
        return 1
    if any(name in os.environ for name in ("PROFIT_LIGA_KEY", "PROFIT_LIGA_ENABLED")):
        print("Настройки Профит Лиги заданы в окружении и имеют приоритет над .env. Ничего не изменено.")
        return 1
    try:
        previous = _parse(target.read_text(encoding="utf-8-sig")).get("PROFIT_LIGA_KEY", "").strip()
        key = ask("API-ключ (Enter — оставить сохранённый): " if previous else "API-ключ: ").strip() or previous
        if not key:
            print("Ключ не введён. Настройки не изменены.")
            return 1
        configure_file(target, key)
        print("Настройки сохранены. Запустите бота и проверьте /parts_test с нужным артикулом.")
        return 0
    except ConfigurationError as exc:
        print(str(exc))
    except (EOFError, KeyboardInterrupt):
        print("Ввод прерван. Настройки не изменены.")
    except (UnicodeError, OSError, ValueError):
        print("Не удалось сохранить настройки. Проверьте кодировку UTF-8, доступ и место на диске. Настройки не изменены.")
    print("Не отправляйте ключ или файл .env в чат.")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
