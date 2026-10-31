"""Меню «Подтверждение действий» — отдельная вкладка бота (/settings).

Тексты и клавиатура вынесены из app.py, чтобы их можно было тестировать
без Telegram: render_policy_text(policy) и policy_keyboard(policy).
"""
from __future__ import annotations

import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..policy import GROUPS, ConfirmationPolicy


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def render_policy_text(policy: ConfirmationPolicy) -> str:
    snap = policy.snapshot()
    master = "✅ ВКЛ" if snap["confirm_all"] else "⚡ ВЫКЛ"
    lines = [
        "⚙️ Подтверждение действий",
        "",
        f"Общий режим: {master}",
        ("«ВКЛ» — любое изменение в STOCRM выполняется только после нажатия "
         "кнопки «Применить»."),
        "«ВЫКЛ» — исключения по группам ниже.",
        "",
        "Группы действий:",
    ]
    for key, meta in GROUPS.items():
        state = snap["groups"].get(key)
        if state is True:
            mark, note = "✅ подтверждать", ""
        elif state is False:
            mark, note = "⚡ без подтверждения", ""
        else:
            mark, note = "◐ настройки разные", ""
        lines.append(f"• {meta['title']} ({meta['desc']}): {mark}{note}")
    lines += [
        "",
        ("Что делает «без подтверждения»: бот сразу вносит изменение в CRM "
         "и помечает ответ значком ⚡. Всё пишется в аудит-лог."),
        f"Последнее изменение: {_fmt_ts(snap['updated_at'])}"
        + (f", ID {snap['updated_by']}" if snap["updated_by"] else ""),
        "",
        "Менять настройки может только администратор (TG_ADMIN_USERS).",
    ]
    return "\n".join(lines)


SKIP_CONFIRM_TEXT = (
    "⚠️ Точно никого не назначать?\n\n"
    "Услуга/задача останется без исполнителя: её не будет в выработке сотрудника, "
    "а сделка может не закрыться в успешный статус, если в CRM включена проверка "
    "«запретить закрытие без исполнителей в работах»."
)


def skip_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    """Переспрос перед созданием без исполнителя — защита от случайного «пропустить»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, без исполнителя", callback_data=f"emp:{token}:skip_yes")],
        [InlineKeyboardButton(text="👥 Нет, выбрать сотрудника", callback_data=f"emp:{token}:back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"emp:{token}:cancel")],
    ])


def policy_keyboard(policy: ConfirmationPolicy) -> InlineKeyboardMarkup:
    snap = policy.snapshot()
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text=("🔒 Подтверждать всё: ВКЛ" if snap["confirm_all"] else "⚡ Подтверждать всё: ВЫКЛ"),
            callback_data="pc:master",
        )
    ]]
    for key, meta in GROUPS.items():
        state = snap["groups"].get(key)
        icon = "✅" if state is True else ("⚡" if state is False else "◐")
        rows.append([InlineKeyboardButton(text=f"{icon} {meta['title']}", callback_data=f"pc:g:{key}")])
    rows.append([
        InlineKeyboardButton(text="🔄 Сбросить (подтверждать всё)", callback_data="pc:reset"),
        InlineKeyboardButton(text="✖ Закрыть", callback_data="pc:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
