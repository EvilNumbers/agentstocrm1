"""Кто может пользоваться ботом: белый список и администраторы.

Зачем отдельный файл: раньше список жил только в `.env` (`TG_ALLOWED_USERS`), и чтобы
пустить нового сотрудника, заказчику приходилось править файл и перезапускать бота.
Теперь список ведётся из меню в самом боте (`/users`): добавление, удаление, права
администратора. Изменения применяются сразу — перезапуск не нужен.

Два источника, и это важно:

* **`.env`** (`TG_ALLOWED_USERS`, `TG_ADMIN_USERS`) — «стартовый» доступ. Его нельзя
  удалить из меню: иначе, потеряв доступ к аккаунту администратора, из бота уже не
  выбраться. В списке такие люди помечены «из .env».
* **`bot_access.json`** (`TG_ACCESS_PATH`, по умолчанию рядом с остальным состоянием) —
  то, что добавлено через меню.

Защита от запирания снаружи: последнего администратора нельзя снять или удалить,
а себя удалить можно только если остаётся другой администратор.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("stocrm.access")


def _as_ids(values: Iterable[Any]) -> frozenset[int]:
    out = set()
    for value in values or ():
        try:
            out.add(int(str(value).strip().lstrip("+")))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


@dataclass
class AccessEntry:
    """Один человек в списке доступа."""

    user_id: int
    name: str = ""
    admin: bool = False
    source: str = "меню"                 # «меню» | «.env»
    added_by: int | None = None
    added_at: float | None = None

    @property
    def title(self) -> str:
        name = " ".join(str(self.name or "").split()).strip()
        return name or f"ID {self.user_id}"

    def to_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "name": self.name, "admin": self.admin,
                "added_by": self.added_by, "added_at": self.added_at}


@dataclass
class AccessList:
    """Белый список бота + права администратора. Потокобезопасно, живёт на диске."""

    path: Path | str
    env_allowed: frozenset[int] = frozenset()
    env_admins: frozenset[int] = frozenset()
    _entries: dict[int, AccessEntry] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    #: Путь к файлу уже как Path (в path можно передать строку из настроек).
    file: Path = field(default=Path("."), init=False, repr=False)

    def __post_init__(self) -> None:
        # принимаем и строку: AccessList(".state/x.json"); дальше работаем с Path
        self.file = Path(str(self.path))
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("список доступа не прочитан (%s): %s", self.file, exc)
            return
        rows = data.get("users") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                user_id = int(str(row.get("user_id")))
            except (TypeError, ValueError):
                continue
            self._entries[user_id] = AccessEntry(
                user_id=user_id,
                name=str(row.get("name") or ""),
                admin=bool(row.get("admin")),
                source="меню",
                added_by=row.get("added_by"),
                added_at=row.get("added_at"),
            )

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1,
                   "users": [entry.to_dict() for entry in
                             sorted(self._entries.values(), key=lambda e: e.user_id)]}
        tmp = self.file.with_suffix(self.file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    # -------------------------------------------------------------------- чтение
    @property
    def open_mode(self) -> bool:
        """Список пуст и в `.env` никого нет — бот пускает всех (как раньше)."""
        return not self.env_allowed and not self._entries

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        with self._lock:
            if self.open_mode:
                return True
            return int(user_id) in self.env_allowed or int(user_id) in self._entries

    def is_admin(self, user_id: int | None) -> bool:
        """Администратор — тот, кто может менять настройки и вести список доступа.

        Если администраторы не заданы нигде, а список доступа заполнен, администраторами
        считаются все, кто в списке: иначе ботом было бы некому управлять.
        """
        if user_id is None:
            return False
        uid = int(user_id)
        with self._lock:
            if uid in self.env_admins:
                return True
            entry = self._entries.get(uid)
            if entry is not None and entry.admin:
                return True
            if self.open_mode:
                return True
            if not self.env_admins and self.env_allowed:
                # Старый режим: TG_ALLOWED_USERS заполнен, TG_ADMIN_USERS — нет.
                # Тогда права администратора у всех, кого пустили (так было и раньше).
                return self.is_allowed(uid)
            return False

    def admin_ids(self) -> list[int]:
        with self._lock:
            ids = set(self.env_admins)
            ids |= {uid for uid, entry in self._entries.items() if entry.admin}
            return sorted(ids)

    def entries(self) -> list[AccessEntry]:
        """Полный список для меню: сначала администраторы, потом по имени."""
        with self._lock:
            rows: dict[int, AccessEntry] = {}
            for uid in self.env_allowed:
                rows[uid] = AccessEntry(user_id=uid, admin=uid in self.env_admins,
                                        source=".env")
            for uid, entry in self._entries.items():
                rows[uid] = AccessEntry(**{**entry.__dict__,
                                           "admin": entry.admin or uid in self.env_admins,
                                           "source": ".env" if uid in self.env_allowed
                                           else "меню"})
            return sorted(rows.values(), key=lambda e: (not e.admin, e.title.lower(), e.user_id))

    def get(self, user_id: int) -> AccessEntry | None:
        with self._lock:
            entry = self._entries.get(int(user_id))
            if entry is not None:
                return entry
            if int(user_id) in self.env_allowed:
                return AccessEntry(user_id=int(user_id), admin=int(user_id) in self.env_admins,
                                   source=".env")
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "open_mode": self.open_mode,
                "env_allowed": sorted(self.env_allowed),
                "env_admins": sorted(self.env_admins),
                "admins": self.admin_ids(),
                "users": [e.to_dict() for e in self.entries()],
            }

    # -------------------------------------------------------------------- запись
    def touch(self, user_id: int, name: str = "") -> bool:
        """Запоминает ФИО человека, который пользуется ботом (для списка в меню).

        Нужно, чтобы администратор видел «Иван Петров (ID 512…)», а не голые цифры.
        Пишем только когда имя правда изменилось — не дёргаем диск на каждое сообщение.
        """
        clean = " ".join(str(name or "").split()).strip()
        if not clean:
            return False
        with self._lock:
            entry = self._entries.get(int(user_id))
            if entry is None:
                if int(user_id) not in self.env_allowed:
                    return False          # незнакомого не заводим: его ещё не добавили
                return False              # из .env — имя не храним, чтобы не плодить записи
            if entry.name == clean:
                return False
            entry.name = clean
            self._save()
            return True

    def add(self, user_id: int, *, name: str = "", by: int | None = None,
            admin: bool = False) -> AccessEntry:
        """Добавляет человека в список. Уже добавленного не дублирует."""
        try:
            uid = int(str(user_id).strip().lstrip("+"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Telegram ID — это число, например 512345678") from exc
        if uid <= 0:
            raise ValueError("Telegram ID не может быть отрицательным или нулём")
        with self._lock:
            entry = self._entries.get(uid)
            if entry is not None:
                changed = False
                if name and entry.name != name:
                    entry.name = name
                    changed = True
                if admin and not entry.admin:
                    entry.admin = True
                    changed = True
                if changed:
                    self._save()
                return entry
            if uid in self.env_allowed:
                raise ValueError(
                    "Этот ID уже есть в списке из файла .env (TG_ALLOWED_USERS) — "
                    "он и так работает с ботом"
                )
            # Первый человек в списке становится администратором: иначе меню доступа
            # окажется без хозяина, и добавить кого-то будет уже некому.
            first_admin = (not admin and not self.env_admins and not self.env_allowed
                           and not any(e.admin for e in self._entries.values()))
            entry = AccessEntry(user_id=uid, name=" ".join(str(name or "").split()).strip(),
                                admin=bool(admin) or first_admin, source="меню", added_by=by,
                                added_at=time.time())
            self._entries[uid] = entry
            self._save()
            return entry

    def remove(self, user_id: int, *, by: int | None = None) -> AccessEntry:
        uid = int(user_id)
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                if uid in self.env_allowed:
                    raise ValueError(
                        "Этот человек задан в файле .env — из меню его не убрать. "
                        "Уберите ID из TG_ALLOWED_USERS и перезапустите бота"
                    )
                raise ValueError("Такого человека нет в списке доступа")
            was_admin = entry.admin or uid in self.env_admins
            if was_admin and by is not None and int(by) == uid and self._only_admin(uid):
                raise ValueError(
                    "Нельзя убрать себя: вы единственный администратор. "
                    "Сначала добавьте другого администратора"
                )
            if was_admin and len(self.admin_ids()) <= 1:
                raise ValueError(
                    "Это последний администратор — если его убрать, управлять ботом "
                    "будет некому. Сначала назначьте другого администратора"
                )
            del self._entries[uid]
            self._save()
            return entry

    def set_admin(self, user_id: int, admin: bool, *, by: int | None = None) -> AccessEntry:
        """Назначает или снимает администратора."""
        uid = int(user_id)
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                if uid in self.env_allowed:
                    raise ValueError(
                        "Этот человек задан в .env (TG_ALLOWED_USERS). Права администратора "
                        "для него задаются там же — в TG_ADMIN_USERS"
                    )
                raise ValueError("Такого человека нет в списке доступа — сначала добавьте его")
            if not admin and entry.admin and len(self.admin_ids()) <= 1:
                raise ValueError(
                    "Это последний администратор — снять права нельзя, иначе управлять "
                    "ботом будет некому"
                )
            if entry.admin == bool(admin):
                return entry
            entry.admin = bool(admin)
            self._save()
            return entry

    def _only_admin(self, user_id: int) -> bool:
        admins = self.admin_ids()
        return len(admins) == 1 and admins[0] == int(user_id)

    # ------------------------------------------------------------------ подсказки
    @staticmethod
    def new_invite() -> str:
        """Короткая метка для логов/аудита (не секрет, просто читаемый маркер)."""
        return secrets.token_hex(4)
