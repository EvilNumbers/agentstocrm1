"""Локальная настройка пробного поиска Росско без ID. Сетевых запросов нет."""
from __future__ import annotations

import getpass
import os
import re
import stat
import tempfile
import warnings
from pathlib import Path
from typing import Callable

from .envfile import _parse, find_env_file

_FIELDS = ("ROSSKO_ENABLED", "ROSSKO_KEY1", "ROSSKO_KEY2", "ROSSKO_DELIVERY_ID", "ROSSKO_ADDRESS_ID")
_PATTERN = re.compile(r"^\s*(?:export\s+)?(ROSSKO_ENABLED|ROSSKO_KEY1|ROSSKO_KEY2|ROSSKO_DELIVERY_ID|ROSSKO_ADDRESS_ID)\s*=")


def configure_file(path: Path, key1: str, key2: str) -> None:
    """Атомарно обновить только эти пять переменных; никогда не трогать .state."""
    if not path.is_file():
        raise ValueError("Файл .env не найден. Сначала завершите setup.bat и перенесите свои настройки.")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{4,128}", key) for key in (key1, key2)):
        raise ValueError("Формат ключей не распознан. Ничего не сохранено.")
    raw = path.read_bytes()
    original = raw.decode("utf-8-sig")
    values = {"ROSSKO_ENABLED": "1", "ROSSKO_KEY1": key1, "ROSSKO_KEY2": key2,
              "ROSSKO_DELIVERY_ID": "", "ROSSKO_ADDRESS_ID": ""}
    lines = original.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _PATTERN.match(line)
        if match:
            key = match.group(1)
            if key in seen:
                raise ValueError("В .env есть повторяющиеся строки настройки Росско. Ничего не сохранено.")
            seen.add(key)
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)
    for key in _FIELDS:
        if key not in seen:
            out.append(f"{key}={values[key]}")
    result = "\n".join(out) + "\n"
    # Скрытый временный файл в той же папке для атомарной замены, режим как у .env.
    fd, tmp = tempfile.mkstemp(prefix=".rossko-env-", dir=path.parent)
    try:
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            fd = -1
            file.write(result)
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != raw:
            raise ValueError("Файл .env изменился во время настройки; ничего не перезаписано.")
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(*, path: Path | None = None, ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass) -> int:
    print("Росско: пробный поиск БЕЗ ID доставки. Никаких сетевых запросов и заказов в этой настройке нет.")
    print("Остановите бота до настройки. Ключи будут записаны локально в .env и передаваться только Росско при поиске; не отправляйте их в чат.")
    target = path or find_env_file()
    if target is None or not target.is_file():
        print("Файл .env не найден. Скопируйте прежние настройки или сначала завершите setup.bat.")
        return 1
    try:
        old = _parse(target.read_text(encoding="utf-8-sig"))
        key1, key2 = old.get("ROSSKO_KEY1", "").strip(), old.get("ROSSKO_KEY2", "").strip()
        if key1 and key2:
            print("Оба ключа уже записаны в .env; их значения не показываются.")
        else:
            print("Введите существующие ключи из кабинета Росско. При вводе символы не отображаются.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key1 = secret("KEY1: ").strip()
                key2 = secret("KEY2: ").strip()
        if not key1 or not key2:
            print("Ключи не введены. Настройки не изменены.")
            return 1
        print("Будут сохранены ключи, включён поиск и очищены оба ID доставки. Запросы к Росско не отправляются.")
        if ask("Продолжить? Введите ДА: ").strip().casefold() not in {"да", "yes"}:
            print("Отменено. Настройки не изменены.")
            return 0
        configure_file(target, key1, key2)
        print("Готово. Запустите бота и отправьте /parts_test 2DUF050N7.")
        print("Росско может отклонить поиск без ID: его документация требует ID доставки.")
        return 0
    except (OSError, UnicodeError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        # Не печатаем исключения ОС и введённые значения: там могут быть ключи.
        print("Не удалось сохранить настройки или ввод был прерван. Не отправляйте ключи в чат.")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
