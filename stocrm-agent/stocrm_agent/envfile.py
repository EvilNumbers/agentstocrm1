"""Чтение файла .env без внешних зависимостей.

Раньше ключи надо было экспортировать вручную (`export STOCRM_SID=...`), из-за чего
`python -m stocrm_agent.bot` падал у всех, кто просто заполнил `.env`. Теперь `.env`
подхватывается автоматически при первом обращении к настройкам.

Правила:
* значения из настоящего окружения имеют приоритет — `.env` только дополняет;
* поддерживаются `KEY=VALUE`, `export KEY=VALUE`, кавычки, комментарии `#`;
* путь можно переопределить переменной `STOCRM_ENV_FILE`.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

log = logging.getLogger("stocrm.envfile")

DEFAULT_NAME = ".env"
_loaded: set[Path] = set()


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # inline-комментарий без кавычек
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def find_env_file(start: Path | None = None) -> Path | None:
    """Ищет .env: явный путь из STOCRM_ENV_FILE → текущая папка → вверх до корня."""
    explicit = os.getenv("STOCRM_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / DEFAULT_NAME
        if candidate.is_file():
            return candidate
    return None


def load_env_file(
    path: str | Path | None = None, *, override: bool = False
) -> Path | None:
    """Загружает .env в os.environ. Возвращает путь файла или None, если его нет."""
    target = Path(path).expanduser() if path else find_env_file()
    if target is None or not target.is_file():
        return None
    target = target.resolve()
    if target in _loaded and not override:
        return target
    try:
        values = _parse(target.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    _loaded.add(target)
    return target


def _clean_number(raw: str | None) -> str:
    """«0,1» → «0.1»: в русской раскладке запятая — обычное дело."""
    return (raw or "").strip().replace(",", ".")


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Число из окружения с понятным фолбэком.

    Раньше `float(os.getenv("LLM_TIMEOUT", "120"))` ронял бота трейсбеком, если
    в .env стояло `LLM_TIMEOUT=` (пусто), `0,1` или опечатка. Теперь берём значение
    по умолчанию и пишем предупреждение в лог.
    """
    raw = os.getenv(name)
    value = default
    if raw is not None and raw.strip() != "":
        try:
            value = float(_clean_number(raw))
        except ValueError:
            log.warning("%s=%r — не число, беру %s", name, raw, default)
            value = default
    if not math.isfinite(value):
        log.warning("%s — не конечное число, беру %s", name, default)
        value = default
    if minimum is not None and value < minimum:
        log.warning("%s=%s меньше минимума %s — беру %s", name, value, minimum, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning(
            "%s=%s больше максимума %s — беру %s", name, value, maximum, maximum
        )
        value = maximum
    return value


def env_int(
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Целое из окружения; при мусоре — значение по умолчанию, а не падение."""
    value = env_float(
        name,
        float(default),
        minimum=None if minimum is None else float(minimum),
        maximum=None if maximum is None else float(maximum),
    )
    return int(value)
