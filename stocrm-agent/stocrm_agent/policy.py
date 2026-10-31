"""Политика подтверждения изменений.

По умолчанию ЛЮБОЕ изменение в STOCRM требует подтверждения человеком.
Политика хранится в JSON и читается на каждом запросе, поэтому настройки из меню
бота применяются сразу, без перезапуска.

Семантика:
    requires_confirmation(tool) = overrides.get(tool, confirm_all)

  * confirm_all=True  — безопасный режим: подтверждаем всё;
  * override=False    — конкретное действие выполняется сразу (осознанное исключение);
  * override=True     — конкретное действие подтверждаем, даже если общий режим выключен.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------- группы
# Каждая группа — это набор инструментов, которые в меню переключаются вместе.
GROUPS: dict[str, dict[str, Any]] = {
    "deals": {
        "title": "Сделки / заказ-наряды",
        "desc": "создание сделки, смена статуса, правка полей",
        "tools": ("draft_create_offer", "draft_change_status", "draft_update_offer_field"),
    },
    "works": {
        "title": "Работы в сделке",
        "desc": "добавление услуг (исполнитель ставится в панели)",
        "tools": ("draft_add_offer_work",),
    },
    "parts": {
        "title": "Запчасти в сделке",
        "desc": "добавление запчастей, комплект ТО (фильтры по VIN + масло)",
        "tools": ("draft_add_offer_part", "draft_change_part_price",
                  "draft_remove_offer_part", "draft_add_maintenance"),
    },
    "contacts": {
        "title": "Клиенты",
        "desc": "создание контакта, правка ФИО и телефона, правка автомобиля (VIN, марка, модель, год)",
        "tools": ("draft_create_contact", "draft_update_contact", "draft_update_car"),
    },
    "tasks": {
        "title": "Задачи",
        "desc": "постановка задач и назначение исполнителей",
        "tools": ("draft_create_task", "draft_assign_task_responsible"),
    },
    "calendar": {
        "title": "Календарь",
        "desc": "записи на посты ТО, включая составную запись с новым клиентом/сделкой",
        "tools": ("draft_create_calendar_entry", "draft_edit_calendar_entry", "draft_book_new_offer", "draft_book_existing_offer"),
    },
    "notes": {
        "title": "Заметки",
        "desc": "заметки в сделке: новая и правка существующей",
        "tools": ("draft_add_offer_note", "draft_update_offer_note"),
    },
    "tags": {
        "title": "Теги",
        "desc": "пометка сделки и позиций тегами (и снятие тега)",
        "tools": ("draft_add_tag", "draft_remove_tag"),
    },
}

TOOL_GROUP: dict[str, str] = {tool: group for group, meta in GROUPS.items() for tool in meta["tools"]}


class ConfirmationPolicy:
    def __init__(self, path: Path | str, *, confirm_all: bool = True) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._confirm_all = confirm_all
        self._overrides: dict[str, bool] = {}
        self._updated_by: int | None = None
        self._updated_at: float | None = None
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._confirm_all = bool(data.get("confirm_all", True))
        self._overrides = {k: bool(v) for k, v in (data.get("overrides") or {}).items()}
        self._updated_by = data.get("updated_by")
        self._updated_at = data.get("updated_at")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "confirm_all": self._confirm_all,
            "overrides": self._overrides,
            "updated_by": self._updated_by,
            "updated_at": self._updated_at,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------- чтение
    @property
    def confirm_all(self) -> bool:
        with self._lock:
            return self._confirm_all

    def requires_confirmation(self, tool: str) -> bool:
        """Главный вопрос: спрашивать человека перед выполнением этого действия?"""
        with self._lock:
            if tool == "draft_book_existing_offer":
                return any(self._overrides.get(name, self._confirm_all) for name in (tool, "draft_change_status", "draft_create_calendar_entry"))
            if tool == "draft_book_new_offer":
                # Одна кнопка, но не обход запрета на создание клиента/сделки.
                return any(self._overrides.get(name, self._confirm_all) for name in (
                    tool, "draft_create_contact", "draft_create_offer", "draft_change_status", "draft_create_calendar_entry"))
            if tool not in TOOL_GROUP and tool.startswith("draft_"):
                # Новый инструмент забыли добавить в группы — безопаснее спросить человека,
                # чем молча записать в CRM при выключенном общем режиме.
                return True
            return self._overrides.get(tool, self._confirm_all)

    def group_state(self, group: str) -> bool | None:
        """True/False — единое состояние для группы, None — настройки разные."""
        tools = GROUPS[group]["tools"]
        states = {self.requires_confirmation(t) for t in tools}
        return states.pop() if len(states) == 1 else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "confirm_all": self._confirm_all,
                "overrides": dict(self._overrides),
                "updated_by": self._updated_by,
                "updated_at": self._updated_at,
                "groups": {g: self.group_state(g) for g in GROUPS},
            }

    # ------------------------------------------------------------------- запись
    def _touch(self, by: int | None) -> None:
        self._updated_by = by
        self._updated_at = time.time()

    def set_confirm_all(self, value: bool, *, by: int | None = None) -> None:
        with self._lock:
            self._confirm_all = bool(value)
            if value:
                # При включении безопасного режима сбрасываем исключения:
                # «подтверждать всё» должно означать именно всё.
                self._overrides.clear()
            self._touch(by)
            self._save()

    def set_group(self, group: str, confirm: bool, *, by: int | None = None) -> None:
        if group not in GROUPS:
            raise KeyError(f"Неизвестная группа действий: {group}")
        with self._lock:
            for tool in GROUPS[group]["tools"]:
                self._overrides[tool] = bool(confirm)
            self._touch(by)
            self._save()

    def toggle_group(self, group: str, *, by: int | None = None) -> bool:
        """Переключает группу и возвращает новое состояние."""
        current = self.group_state(group)
        new_state = not bool(current)  # None (смешанное) -> включить подтверждение
        self.set_group(group, new_state, by=by)
        return new_state

    def reset(self, *, by: int | None = None) -> None:
        """Сброс к безопасным настройкам: подтверждаем всё."""
        with self._lock:
            self._confirm_all = True
            self._overrides.clear()
            self._touch(by)
            self._save()
