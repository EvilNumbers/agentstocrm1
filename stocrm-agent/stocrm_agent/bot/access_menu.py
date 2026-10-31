"""Меню «Кто может пользоваться ботом» (/users) — отдельная вкладка.

Тексты и клавиатура вынесены из app.py, чтобы их можно было проверять без Telegram:
`render_access_text(access)` и `access_keyboard(access)` — чистые функции от списка
доступа, а `run_action(...)` — единственное место, где меняются права. Обработчики в
app.py только передают сюда нажатие кнопки и отправляют готовый ответ.

Важно: менять список может только администратор. Проверка прав — тоже здесь
(`actor.is_admin`), чтобы её нельзя было забыть в каком-то из обработчиков.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..access import AccessList

#: Сколько человек показываем на одной странице меню.
USERS_PER_PAGE = 5


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


@dataclass
class Actor:
    """Кто нажал кнопку. Отдельным классом — чтобы меню не зависело от aiogram."""

    user_id: int | None
    name: str = ""
    is_admin: bool = False


@dataclass
class Reply:
    """Что отправить в чат и что показать всплывающим окном."""

    text: str = ""
    keyboard: InlineKeyboardMarkup | None = None
    alert: str = ""
    #: Кому сообщить радостную новость (когда человека добавили) — app.py отправит сам.
    notify_user: int | None = None
    notify_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _entry_line(entry: Any, icon: str) -> str:
    """Строка списка: «👑 Иван Петров · ID 512» — без «ID 512 · ID 512»."""
    title = entry.title
    line = f"  {icon} {title}"
    if str(entry.user_id) not in title:
        line += f" · ID {entry.user_id}"
    if entry.source == ".env":
        line += " · из .env"
    return line


def render_access_text(access: AccessList, *, note: str = "") -> str:
    """Текст меню: кто есть, какие права, откуда взят и как добавить нового."""
    snap = access.snapshot()
    users = access.entries()
    lines = ["👥 Доступ к боту", ""]
    if snap["open_mode"]:
        lines += [
            "Список пуст — бот сейчас отвечает ЛЮБОМУ, кто его найдёт.",
            "Добавьте хотя бы одного человека кнопкой ниже, и доступ закроется:",
            "остальные получат отказ и свой ID для передачи администратору.",
            "",
        ]
    admins = [e for e in users if e.admin]
    regular = [e for e in users if not e.admin]
    lines.append(f"Администраторы ({len(admins)}): могут менять настройки и этот список")
    if admins:
        lines.extend(_entry_line(entry, "👑") for entry in admins)
    else:
        lines.append("  — нет: пока админы не назначены, права есть у всех из списка")
    lines.append("")
    lines.append(f"Пользователи ({len(regular)}): работают с ботом, менять настройки не могут")
    if regular:
        lines.extend(_entry_line(entry, "👤") for entry in regular)
    else:
        lines.append("  — пока никого, кроме администраторов")
    lines += [
        "",
        "Как добавить: кнопка «➕ Добавить», затем пришлите Telegram ID нового "
        "сотрудника, перешлите его сообщение или его контакт.",
        "Изменения действуют сразу — перезапускать бота не нужно.",
    ]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def _short(name: str, limit: int = 16) -> str:
    text = " ".join(str(name or "").split())
    if len(text) <= limit:
        return text
    head = text.split()[0]
    return head if len(head) <= limit else head[: limit - 1] + "…"


def access_keyboard(access: AccessList, page: int = 0) -> InlineKeyboardMarkup:
    """Кнопки меню: добавить, по каждому человеку — права и удаление, страницы."""
    entries = access.entries()
    pages = max(1, (len(entries) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(max(0, page), pages - 1)
    chunk = entries[page * USERS_PER_PAGE: (page + 1) * USERS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="us:add")
    ]]
    for entry in chunk:
        role = "👑" if entry.admin else "👤"
        # имя — не кнопка-действие, но и не «пустая»: нажатие показывает подсказку
        rows.append([
            InlineKeyboardButton(text=f"{role} {_short(entry.title)}",
                                 callback_data=f"us:info:{entry.user_id}"),
            InlineKeyboardButton(text=("👤 Снять админа" if entry.admin else "👑 Админ"),
                                 callback_data=f"us:adm:{entry.user_id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"us:del:{entry.user_id}"),
        ])
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"us:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="us:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Вперёд ›", callback_data=f"us:page:{page + 1}"))
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"us:page:{page}"),
        InlineKeyboardButton(text="✖ Закрыть", callback_data="us:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖ Отмена", callback_data="us:cancel")],
    ])


def delete_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, убрать доступ", callback_data=f"us:delok:{user_id}")],
        [InlineKeyboardButton(text="‹ Назад к списку", callback_data="us:page:0")],
    ])


ADD_PROMPT = (
    "Кого добавить?\n\n"
    "Пришлите одно из трёх:\n"
    "1) Telegram ID числом — например 512345678;\n"
    "2) перешлите любое сообщение этого человека — я возьму ID и имя оттуда;\n"
    "3) его контакт (скрепка → «Контакт»).\n\n"
    "ID человек может узнать сам: пусть напишет боту — в ответе на отказ будет его ID."
)


def run_action(access: AccessList, action: str, arg: str | None,
               actor: Actor, *, page: int = 0) -> Reply:
    """Одно место, где меняются права. Возвращает готовый ответ для чата.

    `action` — то, что пришло в callback_data после «us:»:
    open | page | add | cancel | info | adm | del | delok | close.
    """
    if not actor.is_admin:
        return Reply(
            alert="Менять доступ может только администратор",
            text="⛔ Управлять доступом может только администратор бота.",
        )

    if action in ("open", "page", "refresh"):
        try:
            page_number = int(arg) if arg not in (None, "") else page
        except (TypeError, ValueError):
            page_number = 0
        return Reply(text=render_access_text(access), keyboard=access_keyboard(access, page_number))

    if action == "add":
        return Reply(text=ADD_PROMPT, keyboard=add_prompt_keyboard())

    if action == "cancel":
        return Reply(text=render_access_text(access, note="Добавление отменено."),
                     keyboard=access_keyboard(access))

    if action == "close":
        return Reply(text="👥 Меню доступа закрыто. Открыть снова — /users")

    if action == "info":
        entry = access.get(int(str(arg))) if str(arg or "").isdigit() else None
        if entry is None:
            return Reply(text=render_access_text(access, note="Этот человек уже не в списке."),
                         keyboard=access_keyboard(access))
        lines = [f"{'👑 Администратор' if entry.admin else '👤 Пользователь'}: {entry.title}"]
        if entry.name and str(entry.user_id) not in entry.name:
            lines.append(f"ID: {entry.user_id}")
        if entry.source == ".env":
            lines.append("Источник: файл .env — из меню не удаляется")
        elif entry.added_at:
            lines.append(f"Добавлен: {_fmt_ts(entry.added_at)}"
                         + (f" (ID {entry.added_by})" if entry.added_by else ""))
        lines.append("")
        lines.append("Может: спрашивать CRM и готовить изменения (каждое — с подтверждением)."
                     if not entry.admin else
                     "Может: всё то же + настройки подтверждений и этот список доступа.")
        return Reply(text="\n".join(lines), keyboard=access_keyboard(access), alert="")

    try:
        user_id = int(str(arg or ""))
    except (TypeError, ValueError):
        return Reply(text=render_access_text(access, note="Некорректный номер в кнопке."),
                     keyboard=access_keyboard(access))

    if action == "adm":
        entry = access.get(user_id)
        want_admin = not (entry.admin if entry else False)
        try:
            changed = access.set_admin(user_id, want_admin, by=actor.user_id)
        except ValueError as exc:
            return Reply(text=render_access_text(access, note=f"⚠️ {exc}"),
                         keyboard=access_keyboard(access), alert=str(exc)[:180])
        access.touch(user_id, "")           # имя не трогаем, только фиксируем факт
        what = "теперь администратор" if changed.admin else "больше не администратор"
        return Reply(text=render_access_text(access, note=f"✅ {changed.title} — {what}."),
                     keyboard=access_keyboard(access), alert="Сохранено")

    if action == "del":
        entry = access.get(user_id)
        if entry is None:
            return Reply(text=render_access_text(access, note="Этого человека уже нет в списке."),
                         keyboard=access_keyboard(access))
        return Reply(
            text=(f"Убрать доступ для «{entry.title}»"
                  + ("" if str(user_id) in entry.title else f" (ID {user_id})") + "?\n"
                  "Человек больше не сможет пользоваться ботом, пока его снова не добавят."),
            keyboard=delete_confirm_keyboard(user_id),
        )

    if action == "delok":
        try:
            removed = access.remove(user_id, by=actor.user_id)
        except ValueError as exc:
            return Reply(text=render_access_text(access, note=f"⚠️ {exc}"),
                         keyboard=access_keyboard(access), alert=str(exc)[:180])
        return Reply(text=render_access_text(
            access, note=f"🗑 Доступ убран: {removed.title} (ID {user_id})."),
            keyboard=access_keyboard(access), alert="Убрано")

    return Reply(text=render_access_text(access), keyboard=access_keyboard(access),
                 alert="Неизвестная команда")


def add_user(access: AccessList, actor: Actor, *, user_id: int | None = None,
             name: str = "", raw: str = "", admin: bool = False) -> Reply:
    """Добавляет человека по ID (числом, из пересланного сообщения или из контакта).

    Возвращает готовый ответ; `notify_user` заполняется, чтобы app.py мог написать
    новому сотруднику «вам открыли доступ».
    """
    if not actor.is_admin:
        return Reply(alert="Добавлять может только администратор",
                     text="⛔ Добавлять пользователей может только администратор бота.")

    if user_id is None:
        digits = re.sub(r"[^\d]", "", str(raw or ""))
        if not digits:
            return Reply(
                text=("Не вижу Telegram ID. Пришлите число (например 512345678), "
                      "перешлите сообщение этого человека или его контакт.\n\n" + ADD_PROMPT),
                keyboard=add_prompt_keyboard(),
            )
        user_id = int(digits)

    try:
        entry = access.add(user_id, name=name, by=actor.user_id, admin=admin)
    except ValueError as exc:
        return Reply(text=render_access_text(access, note=f"⚠️ {exc}"),
                     keyboard=access_keyboard(access), alert=str(exc)[:180])

    role = "администратор" if entry.admin else "пользователь"
    who = (entry.title if str(entry.user_id) in entry.title
           else f"{entry.title} (ID {entry.user_id})")
    note = (f"✅ Добавлен: {who} — {role}.\n"
            "Изменения уже действуют: перезапускать бота не нужно.")
    return Reply(
        text=render_access_text(access, note=note),
        keyboard=access_keyboard(access),
        alert="Добавлено",
        notify_user=entry.user_id,
        notify_text=("\n✅ Вам открыли доступ к боту STOCRM.\n"
                     "Напишите, что нужно сделать, например «покажи сделку 1313».\n"
                     "Любое изменение в CRM сначала приходит черновиком с кнопкой «Применить»."),
    )
