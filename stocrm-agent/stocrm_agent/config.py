"""Конфигурация MCP-сервера STOCRM."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .envfile import env_float, env_int, load_env_file


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    domain: str            # mycompany  или  https://mycompany.stocrm.ru
    sid: str               # ключ доступа из Настройки → API Ключи
    timeout: float = 30.0
    retries: int = 2
    page_size: int = 500
    max_pages: int = 20
    mock: bool = False
    draft_ttl_seconds: int = 3600
    state_dir: Path = Path(".state")
    free_form_type: str = "FREE_FORM"
    # Публичный метод правки существующей строки не подтверждён; по умолчанию выключено.
    price_method: str = ""
    # Поставщик только для явно выбранного supplier / старого offer/order/add.
    # Основной manual использует add_free_order: поставщик и склад не нужны.
    default_supplier_id: int | None = None
    # Сколько страниц склада/истории просматривать при поиске по названию
    # (серверного фильтра по подстроке в STOCRM нет). Меньше — быстрее ответ.
    text_pages: int = 3

    @classmethod
    def from_env(cls, *, require: bool = True) -> Config:
        load_env_file()  # подхватываем .env рядом с проектом, если ключи не экспортированы
        domain = os.getenv("STOCRM_DOMAIN", "").strip()
        sid = os.getenv("STOCRM_SID", "").strip()
        mock = _bool(os.getenv("STOCRM_MOCK"))
        if require and not mock and (not domain or not sid):
            raise SystemExit(
                "Не заданы STOCRM_DOMAIN и/или STOCRM_SID.\n"
                "Скопируйте .env.example в .env и заполните, либо запустите "
                "с STOCRM_MOCK=1 для демо-режима без реальных данных."
            )
        return cls(
            domain=domain or "demo",
            sid=sid or "0_demo",
            timeout=env_float("STOCRM_TIMEOUT", 30.0, minimum=1.0, maximum=600.0),
            retries=env_int("STOCRM_RETRIES", 2, minimum=0, maximum=5),
            page_size=env_int("STOCRM_PAGE_SIZE", 500, minimum=1, maximum=5000),
            max_pages=env_int("STOCRM_MAX_PAGES", 20, minimum=1, maximum=200),
            mock=mock,
            draft_ttl_seconds=env_int("STOCRM_DRAFT_TTL", 3600, minimum=60),
            state_dir=Path(os.getenv("STOCRM_STATE_DIR", ".state")),
            free_form_type=os.getenv("STOCRM_FREE_FORM_TYPE", "FREE_FORM"),
            # Старый список угаданных методов больше не включается автоматически.
            price_method=os.getenv("STOCRM_PRICE_METHOD", "") if os.getenv("STOCRM_ALLOW_UNDOCUMENTED_PRICE_EDITS") == "1" else "",
            default_supplier_id=env_int("STOCRM_DEFAULT_SUPPLIER_ID", 0, minimum=0) or None,
            text_pages=env_int("STOCRM_TEXT_PAGES", 3, minimum=1, maximum=20),
        )

    @property
    def drafts_path(self) -> Path:
        return self.state_dir / "drafts.json"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def capabilities_path(self) -> Path:
        return self.state_dir / "capabilities.json"
