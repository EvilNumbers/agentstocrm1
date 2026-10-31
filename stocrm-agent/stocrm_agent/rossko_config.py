"""Локальная настройка пробного поиска Росско без ID. Сетевых запросов нет."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Callable

from .envfile import _parse, find_env_file

_PATTERN = re.compile(r"^\s*(?:export\s+)?(ROSSKO_ENABLED|ROSSKO_KEY1|ROSSKO_KEY2|ROSSKO_DELIVERY_ID|ROSSKO_ADDRESS_ID)\s*=")


class ConfigurationError(ValueError):
    """Безопасная для показа ошибка, не содержащая ключей или данных .env."""


def _env_key(value: str) -> str:
    """Кодирование ключа с проверкой совместимости с существующим парсером .env."""
    if not value or len(value) > 2048 or not value.isprintable():
        raise ConfigurationError("Ключ пустой или содержит непечатаемые символы. Ничего не сохранено.")
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    if '"' not in value:
        encoded = '"' + value + '"'
    elif "'" not in value:
        encoded = "'" + value + "'"
    else:
        encoded = value
    if _parse("KEY=" + encoded).get("KEY") != value:
        raise ConfigurationError("Ключ не удаётся безопасно записать в .env. Ничего не сохранено.")
    return encoded


def configure_file(path: Path, key1: str, key2: str) -> None:
    """Атомарно обновить пять переменных; никогда не трогать .state."""
    values = {"ROSSKO_ENABLED": "1", "ROSSKO_KEY1": _env_key(key1), "ROSSKO_KEY2": _env_key(key2),
              "ROSSKO_DELIVERY_ID": "", "ROSSKO_ADDRESS_ID": ""}
    _update_fields(path, values)


def update_delivery_ids(path: Path, delivery: str, address: str, *, expected_raw: bytes) -> None:
    """Обновить только ID, если настройки не изменились с момента запроса."""
    if not _valid_id(delivery) or (address and not _valid_id(address)):
        raise ConfigurationError("Некорректный ID; настройки не изменены.")
    _update_fields(path, {"ROSSKO_DELIVERY_ID": delivery, "ROSSKO_ADDRESS_ID": address}, expected_raw=expected_raw)


def _valid_id(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit() and len(value) <= 32


def _update_fields(path: Path, values: dict[str, str], *, expected_raw: bytes | None = None) -> None:
    if not path.is_file():
        raise ConfigurationError("Файл .env не найден. Сначала завершите setup.bat и перенесите свои настройки.")
    raw = path.read_bytes()
    if expected_raw is not None and raw != expected_raw:
        raise ConfigurationError("Файл .env изменился во время настройки; ничего не перезаписано.")
    original = raw.decode("utf-8-sig")
    lines = original.splitlines(keepends=True)
    newline = "\r\n" if b"\r\n" in raw else "\n"
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _PATTERN.match(line)
        if match and match.group(1) in values:
            key = match.group(1)
            if key not in seen:
                seen.add(key)
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "\r" if line.endswith("\r") else ""
                out.append(f"{key}={values[key]}{ending}")
            # Повторяющиеся строки Росско заменяются одной; остальные не меняем.
        else:
            out.append(line)
    result = "".join(out)
    missing = [key for key in values if key not in seen]
    if missing and result and not result.endswith(("\r", "\n")):
        result += newline
    result += "".join(f"{key}={values[key]}{newline}" for key in missing)
    encoded = result.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    # Временный файл в той же папке, атомарная замена только после полной записи.
    fd, tmp = tempfile.mkstemp(prefix=".rossko-env-", dir=path.parent)
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
    print("Росско: пробный поиск БЕЗ ID доставки. Настройка не отправляет запросы в сеть и не создаёт заказы.")
    print("Остановите бота до настройки. При вводе ключи ВИДНЫ на экране: не показывайте экран и не отправляйте ключи в чат.")
    print("После ввода настройки будут сразу сохранены, поиск включён, оба ID доставки очищены.")
    target = path or find_env_file()
    if target is None or not target.is_file():
        print("Файл .env не найден. Скопируйте настройки из старой папки или сначала завершите setup.bat.")
        return 1
    try:
        old = _parse(target.read_text(encoding="utf-8-sig"))
        previous1, previous2 = old.get("ROSSKO_KEY1", "").strip(), old.get("ROSSKO_KEY2", "").strip()
        prompt1 = "KEY1 (Enter — оставить сохранённый): " if previous1 else "KEY1: "
        prompt2 = "KEY2 (Enter — оставить сохранённый): " if previous2 else "KEY2: "
        key1 = ask(prompt1).strip() or previous1
        key2 = ask(prompt2).strip() or previous2
        if not key1 or not key2:
            print("Не введены оба ключа. Настройки не изменены.")
            return 1
        configure_file(target, key1, key2)
        print("Настройки сохранены. Запустите бота и отправьте /parts_test 2DUF050N7.")
        print("Росско может отклонить поиск без ID: его документация требует ID доставки.")
        return 0
    except ConfigurationError as exc:
        print(str(exc))
    except UnicodeError:
        print("Не удалось прочитать .env: требуется текстовый файл в UTF-8. Настройки не изменены.")
    except PermissionError:
        print("Нет доступа к файлу .env или папке. Закройте бота и проверьте права записи. Настройки не изменены.")
    except FileNotFoundError:
        print("Файл .env или его папка не найдены. Проверьте папку с ботом. Настройки не изменены.")
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        suffix = f" Код ошибки системы: {code}." if isinstance(code, int) else ""
        print("Не удалось сохранить .env. Проверьте место на диске и доступ к папке." + suffix + " Настройки не изменены.")
    except (EOFError, KeyboardInterrupt):
        print("Ввод прерван. Настройки не изменены.")
    except ValueError:
        print("Не удалось обработать файл .env. Настройки не изменены.")
    print("Не отправляйте ключи и файл .env в чат.")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
