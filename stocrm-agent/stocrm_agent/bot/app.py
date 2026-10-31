"""Telegram-бот: голосовые сообщения → расшифровка → действия в STOCRM.

Запуск:
    python -m stocrm_agent.bot

Логика подтверждения:
  1. Любая просьба что-то изменить превращается в черновик (ничего не пишется в CRM).
  2. Бот показывает список черновиков с кнопками «Применить» / «Отмена».
  3. Запись в STOCRM происходит только по нажатию кнопки человеком.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import secrets
import time
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from ..access import AccessList
from ..audit import AuditLog
from ..client import StocrmClient, StocrmError
from ..config import Config
from ..crmcheck import check_crm, format_checks, summary
from ..drafts import DraftStore
from ..draft_recovery import lock_refusal
from ..human import humanize_error, polish, pretty_method
from ..netproxy import Route, detect_proxy
from ..netproxy import probe as netprobe
from ..policy import ConfirmationPolicy
from ..report import format_apply_result
from ..tools import Toolset
from .llm import LLMClient
from .access_menu import Actor, add_user, run_action
from .menu import (
    SKIP_CONFIRM_TEXT,
    policy_keyboard,
    render_policy_text,
    skip_confirm_keyboard,
)
from .pipeline import BotPipeline
from .settings import BotSettings
from .stt import STTError, Transcriber, probe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stocrm.bot")

MAX_TEXT = 3500
PROBE_TIMEOUT = 6.0          # сколько ждём на проверку маршрута


# --------------------------------------------------------------------- helpers
def format_staff(staff: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    seen: set[Any] = set()
    for person in staff:
        name = str(person.get("name") or "").strip()
        ident = person.get("user_id") or name
        if not name or ident in seen or re.search(r"\bapi\b|[a-f0-9]{24,}", name, re.I):
            continue
        seen.add(ident)
        lines.append(f"{len(lines) + 1}. {name}")
        if len(lines) >= 40:
            break
    return "👥 Сотрудники в CRM:\n" + ("\n".join(lines) if lines else "Именованные сотрудники не найдены; технические API-учётки скрыты.")


def build_tools(app_config: Config) -> Toolset:
    session = None
    if app_config.mock:
        from ..mock import MockSession

        session = MockSession()
    client = StocrmClient(
        app_config.domain, app_config.sid,
        timeout=app_config.timeout, retries=app_config.retries, session=session,  # type: ignore[arg-type]
    )
    return Toolset(
        client,
        DraftStore(app_config.drafts_path, ttl_seconds=app_config.draft_ttl_seconds),
        AuditLog(app_config.audit_path),
        page_size=app_config.page_size,
        max_pages=app_config.max_pages,
        price_method=app_config.price_method,
        default_supplier_id=app_config.default_supplier_id,
        text_pages=app_config.text_pages,
    )


def note_after_choices(result: Any, prefix: str = "") -> str:
    """Что дописать после сообщений с кнопками.

    Вопрос уже ушёл вместе с кнопками, поэтому второй раз тем же текстом его не
    отправляем: иначе человек видит одно и то же сообщение дважды.
    """
    questions = {str(c.get("question") or "").strip() for c in (result.choices or [])}
    answer = str(getattr(result, "answer", "") or "").strip()
    for question in questions:
        if question:
            answer = answer.replace(question, "").strip()
    return "\n\n".join(part for part in (prefix, answer) if part and part.strip() not in questions)


#: Сколько позиций общего черновика показываем кнопками «убрать».
DROP_BUTTONS_LIMIT = 8


def _short_title(text: str, limit: int = 26) -> str:
    """Короткая подпись кнопки: «1. Колодки PC2031 PILENGA…» → «1. Колодки PC2031»."""
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def drafts_keyboard(drafts: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    rows = []
    for number, draft in enumerate(drafts, 1):
        token = draft.get("token")
        if not token:
            continue
        if draft.get("batch_preparing"):
            rows.append([InlineKeyboardButton(text="❌ Отменить незавершённую сборку", callback_data=f"dr:{token}")])
            continue
        if draft.get("status") == "partial":
            rows.extend([
                [InlineKeyboardButton(text=f"🔎 {number}. Проверить CRM", callback_data=f"rv:{token}"),
                 InlineKeyboardButton(text=f"✅ {number}. Подтвердить по CRM", callback_data=f"rc:{token}")],
                [InlineKeyboardButton(text=f"🗑 {number}. Убрать из списка", callback_data=f"ra:{token}")],
            ])
            if lock_refusal((draft.get("done_steps") or {}).get("error")):
                rows.insert(len(rows) - 1, [InlineKeyboardButton(text=f"🔧 {number}. Доделать после блокировки", callback_data=f"rp:{token}")])
            continue
        check = draft.get("check") or {}
        items = check.get("items") if check.get("kind") == "added_lines" else None
        label = "✅ Применить"
        if items and len(items) > 1:
            label += f" ({len(items)} поз.)"      # человеку — сколько строк запишется
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"ap:{token}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"dr:{token}"),
        ])
        # Несколько позиций в одном черновике: каждую можно убрать отдельно,
        # не отменяя всю просьбу (номер — как в списке выше).
        if not items or len(items) < 2:
            continue
        line: list[InlineKeyboardButton] = []
        for number, item in enumerate(items[:DROP_BUTTONS_LIMIT], 1):
            title = item.get("name") or item.get("sku") or "позиция"
            line.append(InlineKeyboardButton(
                text=f"✂️ {number}. {_short_title(title, 18)}",
                callback_data=f"dp:{token}:{number}",
            ))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


CHOICES_PER_PAGE = 6


def booking_keyboard(token: str, pending: dict[str, Any]) -> InlineKeyboardMarkup | None:
    options = pending.get("options") or []
    if not options:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(option["label"])[:100], callback_data=f"book:{token}:{index}")]
        for index, option in enumerate(options, 1)
    ])


def employee_keyboard(token: str, candidates: list[dict[str, Any]], *, page: int = 0,
                      allow_skip: bool = True) -> InlineKeyboardMarkup:
    """Кнопки выбора исполнителя из списка сотрудников CRM."""
    total = len(candidates)
    pages = max(1, -(-total // CHOICES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * CHOICES_PER_PAGE
    chunk = candidates[start:start + CHOICES_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for emp in chunk:
        rows.append([InlineKeyboardButton(text=f"👤 {emp['name']}", callback_data=f"emp:{token}:{emp['user_id']}")])

    nav: list[InlineKeyboardButton] = []
    if pages > 1:
        if page > 0:
            nav.append(InlineKeyboardButton(text="←", callback_data=f"emp:{token}:p:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"emp:{token}:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="→", callback_data=f"emp:{token}:p:{page + 1}"))
    if nav:
        rows.append(nav)

    tail: list[InlineKeyboardButton] = []
    if allow_skip:
        tail.append(InlineKeyboardButton(text="⏭ Без исполнителя", callback_data=f"emp:{token}:skip"))
    tail.append(InlineKeyboardButton(text="❌ Отмена", callback_data=f"emp:{token}:cancel"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


PART_CHOICES_PER_PAGE = 6


def _part_button(item: dict[str, Any], number: int) -> str:
    """Текст кнопки: бренд + артикул + название, сноска о наличии — в конце."""
    name = str(item.get("name") or "").strip()
    if item.get("supplier_id"):
        return f"🏭 {number}. {name} (ID {item['supplier_id']})"[:64]
    head = " ".join(str(x) for x in (item.get("brand"), item.get("sku")) if x) or name or "вариант"
    text = f"{head} — {name}" if name and name.lower() != head.lower() else head
    note = " · в наличии" if item.get("in_stock") or item.get("qty") else ""
    mark = "✅ " if item.get("index") == 0 else ""
    prefix = f"{mark}{number}. "
    room = 64 - len(prefix) - len(note)
    if len(text) > room:
        text = text[: max(0, room - 1)].rstrip(" ,—") + "…"
    return f"{prefix}{text}{note}"


def render_to_choice(choice: dict[str, Any]) -> str:
    """Вопрос сценария ТО: «стандартное или нет», бренд фильтров, выбор фильтра, масло."""
    lines = [f"🧰 {choice.get('question') or 'Уточните, пожалуйста.'}"]
    step = choice.get("step")
    if choice.get("why"):
        lines.append(f"   {choice['why']}")
    if step == "oil":
        lines.append("")
        lines.append("Напишите ответ сообщением — например «Motul 5W-30» или «Лукойл 5w40».")
        lines.append("Фильтры уже подобраны и ждут подтверждения — их можно подтвердить сразу.")
        return "\n".join(lines)
    if step == "brand":
        lines.append("")
        lines.append("Можно нажать бренд ниже или просто написать свой — например «Sakura».")
        return "\n".join(lines)
    candidates = choice.get("candidates") or []
    if step == "pick" and candidates:
        lines.append("")
        lines.extend(_part_line(i, c) for i, c in enumerate(candidates, 1))
        if any(c.get("stock_note") for c in candidates):
            lines.append("")
            lines.append("ℹ️ В наличии на складе — справочно: со склада в сделку не подставляем.")
        lines.append("")
        lines.append("Первый вариант — самый вероятный. Выберите кнопкой ниже.")
    return "\n".join(lines)


def to_keyboard(token: str, candidates: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in candidates:
        text = str(item.get("title") or item.get("value") or "вариант")
        if len(text) > 64:
            text = text[:63].rstrip(" ,—") + "…"
        rows.append([InlineKeyboardButton(text=text,
                                          callback_data=f"to:{token}:{item.get('index')}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"to:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _part_line(number: int, item: dict[str, Any]) -> str:
    title = item.get("title") or " ".join(str(item.get(k) or "") for k in ("brand", "sku", "name")).strip()
    return f"{number}. {title}" + (f" · {item['stock_note']}" if item.get("stock_note") else "")


def part_keyboard(token: str, candidates: list[dict[str, Any]], *, page: int = 0, text_only: bool = False) -> InlineKeyboardMarkup:
    pages = max(1, (len(candidates) + 5) // 6)
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(text=_part_button(c, page * 6 + n + 1),
             callback_data=f"prt:{token}:{c.get('index')}")]
            for n, c in enumerate(candidates[page * 6:(page + 1) * 6])]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"prt:{token}:p:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"prt:{token}:p:{page+1}"))
    if nav:
        rows.append(nav)
    if not candidates and not text_only:
        rows.extend([
            [InlineKeyboardButton(text="➕ Добавить как указано", callback_data=f"prt:{token}:manual")],
            [InlineKeyboardButton(text="🔎 Повторить поиск", callback_data=f"prt:{token}:retry"),
             InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"prt:{token}:skip")],
        ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"prt:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_part_choice(choice: dict[str, Any]) -> str:
    lines = [str(choice.get("question") or "Выберите запчасть:")]
    candidates = choice.get("candidates") or []
    if choice.get("role") == "supplier":
        lines.extend(f"{n}. {c['name']} (ID {c['supplier_id']})" for n, c in enumerate(candidates, 1))
        lines.append("Выберите поставщика кнопкой или напишите его название.")
        return "\n".join(lines)
    if candidates:
        first = candidates[0]
        label = " ".join(str(first.get(k) or "") for k in ("brand", "sku")).strip()
        lines.append("Самый вероятный: " + (label or str(first.get("title") or "первый вариант")))
        if first.get("why"):
            lines.append(str(first["why"]))
        sources = {str(c.get("source") or "") for c in candidates}
        if "интернет" in sources:
            lines.append("Варианты из интернета — не подтверждение наличия и применяемости:")
            for number, candidate in enumerate(candidates, 1):
                url = str(candidate.get("url") or "")
                if candidate.get("source") == "интернет" and url.startswith(("https://", "http://")):
                    label = " ".join(str(candidate.get(k) or "") for k in ("brand", "sku")).strip()
                    lines.append(f"{number}. {label}: {url}")
        known = choice.get("args") or {}
        lines.append("Выберите подходящую позицию кнопкой." if known.get("sku") else
                     "Выберите вариант кнопкой или уточните бренд и артикул.")
    elif not (choice.get("args") or {}).get("sku"):
        lines.append("Укажите бренд и артикул запчасти, если они известны.")
    # Диагностика доступна в /parts_test, а не дублируется в каждом вопросе.
    return "\n".join(lines)


def render_choice(choice: dict[str, Any]) -> str:
    lines = [f"❓ {choice['question']}"]
    if choice.get("reason") == "ambiguous":
        lines.append("Уточните, пожалуйста, — я не хочу назначить не того.")
    if choice.get("candidates"):
        lines.append("Выберите кнопкой ниже или назовите фамилию.")
    return "\n".join(lines)


def render_applied(applied: list[dict[str, Any]]) -> str:
    """Что уже внесено (режим без подтверждения) — по-русски, без имён методов панели."""
    lines = []
    for i, item in enumerate(applied, 1):
        summary = item.get("summary") or "изменение"
        state = "результат требует проверки" if item.get("warning") else "API принял запрос"
        lines.append(f"{i}. {summary}\n   → {pretty_method(item.get('method'))}, {state}")
        if item.get("warning"):
            # панель записала не то, что просили (наценка, чужое количество)
            lines.append(f"   ⚠️ {item['warning']}")
    return "\n".join(lines)


_BULLET = re.compile(r"^\s*(?:[•\-*—]|\d{1,2}[.)])\s+")


def without_repeated_list(answer: str, drafts: list[dict[str, Any]]) -> str:
    """Убирает из ответа модели перечисление тех же позиций, что уже есть в черновике.

    Реальный случай 30.10: бот присылал «А по остальным позициям я подготовил добавление:
    • MITSUBISHI MB664285 … • STELLOX … • ABCP …», а ниже — тот же список черновика.
    Два списка подряд человек читает как два разных набора.
    """
    text = str(answer or "")
    skus = [str(item.get("sku") or "").strip()
            for draft in drafts
            for item in ((draft.get("check") or {}).get("items") or [])]
    skus = [sku for sku in skus if len(sku) >= 3]
    if len(skus) < 2:
        return text
    lowered = text.lower()
    if sum(1 for sku in skus if sku.lower() in lowered) < 2:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        if _BULLET.match(line):
            break                        # началось перечисление — дальше список черновика
        kept.append(line)
    stripped = "\n".join(kept).strip()
    return stripped or "Подготовил изменения — проверьте список ниже."


def drafts_block(drafts: list[dict[str, Any]], answer: str = "") -> str:
    """Текст «что будет записано» без повтора одной и той же фразы дважды.

    Раньше сообщение выглядело так:

        Подготовил изменения — подтвердите, пожалуйста.
        Подготовлено к записи (пока ничего не изменено):
        Добавить в сделку 1313 — позиций 3 …

    Три строки об одном и том же. Если ответ уже говорит «подтвердите», заголовок
    блока не нужен — сразу список.
    """
    if not drafts:
        return ""
    body = render_drafts(drafts)
    if any(d.get("status") == "partial" for d in drafts):
        return "⚠️ Требуется проверка CRM; часть действий могла выполниться. Не повторяйте заявку:\n" + body
    if "подтвердите" in str(answer or "").lower():
        return body
    return "Подготовлено к записи (пока ничего не изменено):\n" + body


def render_drafts(drafts: list[dict[str, Any]]) -> str:
    lines = []
    for i, draft in enumerate(drafts, 1):
        # .get: черновики из прошлых версий файла могут не иметь части полей,
        # а падать на этом нельзя — иначе бот перестанет отвечать вовсе.
        summary = str(draft.get("summary") or "изменение")
        method = pretty_method(draft.get("method"))
        if "\n" in summary:
            # Список позиций: свой заголовок и своя нумерация уже внутри.
            # Раньше сверху добавлялось ещё «1.», и получалось «1. … 1. … 2. … 3. …».
            lines.append(f"{summary}\n   → {method}")
            continue
        lines.append(f"{i}. {summary}\n   → {method}")
    return "\n".join(lines)


def chunk(text: str, size: int = MAX_TEXT) -> list[str]:
    """Режет текст на сообщения, укладываясь в лимит Telegram (4096).

    Длинный абзац без переводов строки тоже режется — иначе Telegram вернёт ошибку.
    """
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    current = ""
    for para in text.split("\n"):
        for piece in [para[i:i + size] for i in range(0, len(para), size)] or [""]:
            if len(current) + len(piece) + 1 > size:
                if current:
                    parts.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts or [text[:size]]


def _hide_password(url: str) -> str:
    """В логах не должно быть пароля от прокси."""
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url)


class AllowedUsers(BaseMiddleware):
    """Белый список бота.

    Список читается из `AccessList` на КАЖДОМ сообщении: человек, которого только что
    добавили через меню `/users`, может писать сразу, без перезапуска бота. Пустой
    список = пускаем всех (как раньше — удобно для первого запуска и для тестов).
    """

    def __init__(self, access: AccessList) -> None:
        self.access = access

    async def __call__(self, handler, event, data):  # type: ignore[override]
        user: User | None = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        if not self.access.is_allowed(user_id):
            log.warning("отказ доступа user_id=%s", user_id)
            if isinstance(event, Message) or hasattr(event, "text"):
                await event.answer(
                    "⛔ Доступ к боту закрыт.\n\n"
                    f"Ваш Telegram ID: {user_id}\n\n"
                    "Передайте этот номер администратору — он добавит вас через меню "
                    "«👥 Доступ» (/users), и бот сразу начнёт отвечать."
                )
            return None
        if user_id is not None and user is not None:
            # имя нужно меню доступа: «Иван Петров (ID 512…)» читается лучше цифр
            with contextlib.suppress(Exception):
                self.access.touch(int(user_id), user.full_name)
        return await handler(event, data)


# ------------------------------------------------------------------------ бот
#: Телеграм отклоняет всплывающее окно кнопки (show_alert) длиннее 200 символов —
#: целиком, вместе с текстом. Из-за этого человек видел не объяснение, а
#: «необработанная ошибка обработчика».
ALERT_LIMIT = 190
#: Предел одного сообщения в Телеграме.
MESSAGE_LIMIT = 4000


def clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Обрезает текст до допустимой длины, сохраняя начало (там суть)."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def notify_problem(callback: CallbackQuery, text: str) -> None:
    """Показывает ошибку так, чтобы Телеграм её точно принял.

    Короткий текст — во всплывающем окне; длинный — короткая зацепка в окне,
    а подробности отдельным сообщением в чат, чтобы их можно было перечитать.
    """
    full = polish(str(text or "").strip() or "Не удалось")
    short = clip(full, ALERT_LIMIT)
    with contextlib.suppress(Exception):
        if len(short) >= len(full):
            await callback.answer(short, show_alert=True)
            return
        await callback.answer("Не удалось — подробности ниже", show_alert=True)
    if len(short) < len(full) and callback.message is not None:
        with contextlib.suppress(Exception):
            await callback.message.answer(clip("⚠️ " + full))


def create_app(bot_settings: BotSettings | None = None, app_config: Config | None = None):
    bs = bot_settings or BotSettings.from_env()
    cfg = app_config or Config.from_env()

    # Белый список: стартовые ID из .env + то, что добавлено через меню /users.
    access_path = os.getenv("TG_ACCESS_PATH") or str(cfg.state_dir / "bot_access.json")
    access = AccessList(access_path, env_allowed=bs.allowed_users, env_admins=bs.admin_users)
    if access.open_mode:
        logging.getLogger("stocrm.bot").warning(
            "Список доступа пуст — бот отвечает любому, кто его найдёт. "
            "Откройте /users и добавьте сотрудников (или задайте TG_ALLOWED_USERS)."
        )
    tools = build_tools(cfg)
    # если TG_POLICY_PATH не задан явно — держим политику рядом с остальным состоянием
    policy_path = bs.policy_path if os.getenv("TG_POLICY_PATH") else str(cfg.state_dir / "bot_policy.json")
    policy = ConfirmationPolicy(policy_path, confirm_all=bs.confirm_required)
    pipeline = BotPipeline(tools, LLMClient(bs), Transcriber(bs), bs, policy)

    # Маршрут до Telegram: сначала напрямую, прокси — только если напрямую не выходит.
    api = TelegramAPIServer.from_base(bs.api_server) if bs.api_server else TelegramAPIServer.from_base("https://api.telegram.org")
    session = None
    route = probe_telegram_route(bs)
    if route.proxy:
        try:
            session = AiohttpSession(proxy=route.proxy, api=api)
            log.info("Telegram: %s", _hide_password(route.note))
        except Exception as exc:  # noqa: BLE001 — прокси не должен мешать запуску
            log.warning("не удалось настроить прокси %s: %s", _hide_password(route.proxy), exc)
            session = None
    elif bs.api_server:
        session = AiohttpSession(api=api)
        log.info("Telegram через свой Bot API server: %s", bs.api_server)
    else:
        log.info("Telegram: напрямую (прокси не требуется)")

    bot = Bot(bs.token, session=session, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.message.middleware(AllowedUsers(access))
    dp.callback_query.middleware(AllowedUsers(access))
    router = Router()
    @dp.errors()
    async def on_error(event: Any) -> bool:
        """Любая необработанная ошибка — в лог и коротким сообщением в чат."""
        log.exception("необработанная ошибка обработчика")
        target = getattr(event, "message", None) or getattr(event, "update", None)
        chat = getattr(target, "chat", None)
        if chat is not None:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat.id,
                    "⚠️ Внутренняя ошибка при обработке сообщения. Подробности — "
                    "в файле .state/bot.log. Если повторяется — пришлите последние строки оттуда.",
                )
        return True

    dp.include_router(router)

    history: dict[int, list[dict[str, Any]]] = {}
    # ожидающие выбора исполнителя: token -> {choice, page, created_at}
    pending_choices: dict[str, dict[str, Any]] = {}

    def remember_choice(choice: dict[str, Any]) -> str:
        token = secrets.token_hex(4)
        pending_choices[token] = {"choice": choice, "created_at": time.time()}
        if len(pending_choices) > 200:  # простая защита от роста памяти
            oldest = sorted(pending_choices, key=lambda k: pending_choices[k]["created_at"])[:50]
            for key in oldest:
                pending_choices.pop(key, None)
        return token

    async def progress_while(message: Message) -> None:
        """Пока бот думает: индикатор «печатает» и одна честная строка ожидания.

        Раньше на время запроса в STOCRM бот просто молчал (а с синхронными вызовами
        замирал и сам). Теперь работа идёт в отдельном потоке, а человек видит, что
        происходит.
        """
        bot = message.bot
        if bot is None:  # без Bot нечем показывать «печатает» — просто ждём
            return
        with contextlib.suppress(Exception):
            await bot.send_chat_action(message.chat.id, "typing")
        await asyncio.sleep(2.0)
        with contextlib.suppress(Exception):
            await message.answer("⏳ Секунду — смотрю в STOCRM…")
        ticks = 0
        while True:
            await asyncio.sleep(4.5)
            ticks += 1
            with contextlib.suppress(Exception):
                await bot.send_chat_action(message.chat.id, "typing")
            if ticks % 5 == 0:
                with contextlib.suppress(Exception):
                    await message.answer("⏳ Обработка ещё идёт. Не отправляйте эту же заявку повторно — результат появится здесь.")

    async def run_and_reply(message: Message, text: str, prefix: str = "", actor_id: int | None = None) -> None:
        user_id = actor_id if actor_id is not None else (message.from_user.id if message.from_user else 0)
        hist = history.setdefault(user_id, [])
        progress = asyncio.create_task(progress_while(message))
        try:
            result = await pipeline.handle_text(text, hist, user_id=user_id)
        except Exception as exc:
            log.exception("Не удалось обработать сообщение")
            await message.answer("⚠️ " + humanize_error(exc) + "\nЗапрос не завершён. Не повторяйте запись вслепую; для чтения укажите номер сделки.")
            return
        finally:
            progress.cancel()

        if result.error:
            await message.answer("⚠️ " + polish(result.error))
            return

        if result.pending_booking and result.pending_booking.get("options"):
            pending = result.pending_booking
            token = remember_choice({"kind": "booking", "pending": pending, "owner": user_id})
            await message.answer(polish(result.answer or pending["question"]), reply_markup=booking_keyboard(token, pending))
            return

        if result.choices:
            # Спрашиваем исполнителя ДО создания черновика; кнопки подтверждения покажем после выбора.
            for choice in result.choices:
                token = remember_choice(choice)
                if choice.get("kind") == "part":
                    await message.answer(render_part_choice(choice),
                                         reply_markup=part_keyboard(token, choice["candidates"], text_only=choice.get("_group_step") in {"price", "offer"}))
                    continue
                if choice.get("kind") == "to":
                    await message.answer(render_to_choice(choice),
                                         reply_markup=to_keyboard(token, choice.get("candidates") or []))
                    continue
                keyboard = employee_keyboard(token, choice["candidates"], allow_skip=True)
                await message.answer(render_choice(choice), reply_markup=keyboard)
            note = note_after_choices(result, prefix)
            if note.strip():
                await message.answer(note)
            return

        body = without_repeated_list(result.answer or "Готово.", result.drafts)
        parts = [p for p in (prefix, body) if p]
        if result.applied:
            parts.append("⚡ Внесено без подтверждения (режим из /settings):\n" + render_applied(result.applied))
        if result.drafts:
            parts.append(drafts_block(result.drafts, body))
            warnings = sorted({str(d["warning"]) for d in result.drafts if d.get("warning")})
            if warnings:
                parts.append("⚠️ " + "\n⚠️ ".join(warnings))

        text_out = polish("\n\n".join(parts).strip() or "Готово.")
        await message.answer(chunk(text_out)[0], reply_markup=drafts_keyboard(result.drafts))
        for extra in chunk(text_out)[1:]:
            await message.answer(extra)

    def is_admin(user: User | None) -> bool:
        """Администратор — из .env или назначенный через меню /users."""
        return access.is_admin(getattr(user, "id", None))

    def actor_of(user: User | None) -> Actor:
        return Actor(user_id=getattr(user, "id", None),
                     name=getattr(user, "full_name", "") or "",
                     is_admin=is_admin(user))

    # ------------------------------------------------------ меню подтверждений
    @router.message(F.text.startswith("/settings"))
    async def cmd_settings(message: Message) -> None:
        if not is_admin(message.from_user):
            await message.answer("⚙️ Настройки подтверждений доступны только администратору.")
            return
        await message.answer(render_policy_text(policy), reply_markup=policy_keyboard(policy))

    @router.callback_query(F.data.startswith("pc:"))
    async def on_policy_callback(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not is_admin(user):
            await callback.answer("Менять настройки может только администратор", show_alert=True)
            return
        action = (callback.data or "").split(":", 2)
        actor_id = user.id if user else None

        if action[1] == "close":
            await safe_edit(_cb_msg(callback), "⚙️ Настройки закрыты. Открыть снова — /settings")
            await callback.answer()
            return

        if action[1] == "master":
            policy.set_confirm_all(not policy.confirm_all, by=actor_id)
        elif action[1] == "reset":
            policy.reset(by=actor_id)
        elif action[1] == "g" and len(action) == 3:
            try:
                policy.toggle_group(action[2], by=actor_id)
            except KeyError as exc:
                await notify_problem(callback, humanize_error(exc))
                return
        else:
            await callback.answer("Неизвестная команда")
            return

        tools.audit.write("bot.policy", by=actor_id, action=":".join(action[1:]),
                          snapshot=policy.snapshot()["groups"], confirm_all=policy.confirm_all)
        await safe_edit(_cb_msg(callback), render_policy_text(policy), reply_markup=policy_keyboard(policy))
        await callback.answer("Сохранено")

    # ------------------------------------------------- меню доступа (/users)
    #: Кто сейчас в режиме «пришлите ID»: user_id админа → время начала.
    pending_access: dict[int, float] = {}
    ACCESS_PENDING_TTL = 600.0

    def _access_reply(reply: Any) -> str:
        return clip(polish(reply.text or ""))

    async def send_reply(message: Message, reply: Any) -> None:
        """Отправляет ответ меню: текст + кнопки, и (если нужно) пишет новому человеку."""
        if reply.notify_user:
            with contextlib.suppress(Exception):
                await bot.send_message(reply.notify_user, reply.notify_text)
        await message.answer(_access_reply(reply), reply_markup=reply.keyboard)

    @router.message(F.text.startswith("/users"))
    async def cmd_users(message: Message) -> None:
        actor = actor_of(message.from_user)
        if not actor.is_admin:
            await message.answer(
                "⛔ Управлять доступом может только администратор бота.\n"
                f"Ваш Telegram ID: {actor.user_id}\n"
                "Передайте его администратору — он добавит вас через /users."
            )
            return
        reply = run_action(access, "open", None, actor)
        tools.audit.write("bot.access_view", by=actor.user_id)
        await send_reply(message, reply)

    @router.callback_query(F.data.startswith("us:"))
    async def on_access_callback(callback: CallbackQuery) -> None:
        raw = (callback.data or "").split(":")
        action = raw[1] if len(raw) > 1 else ""
        arg = raw[2] if len(raw) > 2 else None
        actor = actor_of(callback.from_user)
        reply = run_action(access, action, arg, actor)
        if not reply.text and not reply.keyboard:
            await callback.answer(reply.alert or "Готово")
            return
        if action in ("add",):
            pending_access[actor.user_id or 0] = time.time()
        elif action in ("cancel", "close"):
            pending_access.pop(actor.user_id or 0, None)
        if action not in ("open", "page", "refresh", "close", "info", "noop", ""):
            # каждое изменение списка — в аудит: видно, кто кого добавил
            tools.audit.write("bot.access", by=actor.user_id, action=action, target=arg,
                              admins=access.admin_ids(), users=[e.user_id for e in access.entries()])
        message = _cb_msg(callback)
        if message is not None and reply.keyboard is not None:
            await safe_edit(message, _access_reply(reply), reply_markup=reply.keyboard)
        elif message is not None:
            await safe_edit(message, _access_reply(reply))
        if reply.notify_user:
            with contextlib.suppress(Exception):
                await bot.send_message(reply.notify_user, reply.notify_text)
        await callback.answer(reply.alert or "Готово")

    async def _pending_access_add(message: Message, actor: Actor) -> bool:
        """Режим «пришлите ID»: число, пересланное сообщение или контакт.

        Возвращает True, если сообщение было про добавление пользователя и его не надо
        разбирать как просьбу к CRM.
        """
        if actor.user_id is None or actor.user_id not in pending_access:
            return False
        if time.time() - pending_access[actor.user_id] > ACCESS_PENDING_TTL:
            pending_access.pop(actor.user_id, None)     # забыли, о чём просили
            return False
        user_id: int | None = None
        name = ""
        forward = getattr(message, "forward_from", None) or getattr(
            getattr(message, "forward_origin", None), "sender_user", None)
        contact = getattr(message, "contact", None)
        if forward is not None:
            user_id = getattr(forward, "id", None)
            name = getattr(forward, "full_name", "") or ""
        elif contact is not None and getattr(contact, "user_id", None):
            user_id = int(contact.user_id)
            name = " ".join(filter(None, [getattr(contact, "first_name", ""),
                                          getattr(contact, "last_name", "")]))
        text = (message.text or "").strip()
        if user_id is None and text and not text.startswith("/"):
            reply = add_user(access, actor, raw=text)
        elif user_id is not None:
            reply = add_user(access, actor, user_id=int(user_id), name=name)
        else:
            return False
        pending_access.pop(actor.user_id, None)
        tools.audit.write("bot.access", by=actor.user_id, action="add",
                          target=user_id, admins=access.admin_ids())
        await send_reply(message, reply)
        return True

    # ---------------------------------------------------------- команды
    @router.message(F.text.startswith("/start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "Здравствуйте! Это ассистент STOCRM.\n\n"
            "🎙 Надиктуйте или напишите, что нужно сделать, например:\n"
            "• «Что по заказ-наряду 1654, сколько оплачено?»\n"
            "• «Закрой 1654 как успешный, работы выполнены»\n"
            "• «Собери ТО» — фильтры по VIN из карточки сделки и вопрос про масло\n"
            "• «Есть ли на складе фильтр W914/2?»\n"
            "• «Запиши Иванова на замену масла завтра в 10»\n"
            "• «Поставь тег срочно на сделку 1654» или на позицию W914/2\n"
            "• «Добавь заметку в 1654: клиент ждёт звонка после обеда»\n"
            "• «Покажи календарь на завтра» — уточню подразделение\n"
            "• «Запиши сделку 1654 на пост диагностики 2 ноября с 10 до 11»\n\n"
            "Любые изменения я сначала показываю как черновик — в CRM они попадут "
            "только после нажатия «Применить».\n\n"
            "У задач предложу выбрать исполнителя. Услуги добавляю без назначения исполнителя.\n\n"
            "Команды: /settings (подтверждения), /users (доступ к боту), "
            "/staff (сотрудники), /drafts, /voice_test, "
            "/parts_test (поиск запчасти по шагам), /reset, /help"
        )

    @router.message(F.text.startswith("/help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "Как пользоваться:\n"
            "1. Голосовое или текст — свободная формулировка, как для коллеги.\n"
            "2. Я нахожу нужные объекты в CRM и показываю, что понял.\n"
            "3. Изменения приходят черновиком с кнопками «Применить» / «Отмена».\n\n"
            "/drafts — черновики, ожидающие решения\n"
            "/settings — окно подтверждений: включить/отключить по группам действий\n"
            "/users — доступ к боту: добавить сотрудника, дать права администратора\n"
            "/staff — список сотрудников, из которых выбирается исполнитель\n"
            "/voice_test — проверить, работает ли распознавание голосовых\n"
            "/crm_test — проверить связь с панелью STOCRM (только чтение)\n"
            "/reset — очистить контекст диалога"
        )

    @router.message(F.text.startswith("/reset"))
    async def cmd_reset(message: Message) -> None:
        if message.from_user:
            history.pop(message.from_user.id, None)
            pipeline.reset_pending(message.from_user.id)     # и незакрытые вопросы тоже
        await message.answer("Контекст очищен. Незакрытые вопросы (запчасть, цена, ТО) забыты.")

    @router.message(F.text.startswith("/staff"))
    async def cmd_staff(message: Message) -> None:
        try:
            staff = await asyncio.to_thread(tools.users)
        except StocrmError as exc:
            await message.answer("⚠️ " + humanize_error(exc, context="Не удалось получить сотрудников"))
            return
        if not staff:
            await message.answer("Список сотрудников пуст или у ключа нет прав.")
            return
        await message.answer(format_staff(staff))

    @router.message(F.text.startswith("/drafts"))
    async def cmd_drafts(message: Message) -> None:
        pending = tools.drafts.list("pending")
        partial = tools.drafts.list("partial")
        archived = [d for d in tools.drafts.list("all") if d.get("archived_partial") or (
            d.get("tool") == "add_offer_part" and d.get("status") in {"applied", "discarded"}
            and not (d.get("review") or {}).get("verified") and not d.get("merged_into")
            and (d.get("check_warning") or (d.get("status") == "discarded" and d.get("done_steps"))))]
        if not pending and not partial and not archived:
            await message.answer("Черновиков нет.")
            return
        if pending:
            await message.answer(
                "Ожидают подтверждения:\n" + render_drafts(pending),
                reply_markup=drafts_keyboard(pending),
            )
        if partial:
            lines = [f"{number}. ⚠️ {d['summary']} — требует проверки; целиком повторять нельзя" for number, d in enumerate(partial, 1)]
            await message.answer("Требуют проверки в STOCRM:\n" + "\n".join(lines), reply_markup=drafts_keyboard(partial))
        if archived:
            visible = [{**d, "status": "partial"} for d in archived]
            lines = [f"{number}. ⚠️ {d['summary']}" for number, d in enumerate(visible, 1)]
            await message.answer("Убранные или старые непроверенные попытки блокируют повтор до проверки CRM:\n" + "\n".join(lines), reply_markup=drafts_keyboard(visible))

    # ---------------------------------------------------------- голос/аудио
    async def handle_audio(message: Message, file_id: str, suffix: str,
                           file_size: int | None = None) -> None:
        if file_size and file_size > 20 * 1024 * 1024:
            await message.answer("⚠️ Файл больше 20 МБ — Telegram не даст его скачать. "
                                 "Разбейте запись на части.")
            return
        if message.bot is None:
            await message.answer("⚠️ Внутренняя ошибка: нет доступа к Bot API.")
            return
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(message.chat.id, "typing")
        buffer = io.BytesIO()
        try:
            await message.bot.download(file_id, destination=buffer)
        except Exception as exc:  # noqa: BLE001 — сеть, лимиты Telegram, права
            log.warning("не удалось скачать голосовое: %s", exc)
            await message.answer(
                "⚠️ Не удалось скачать голосовое из Telegram.\n\n"
                "Что сделать: запишите его ещё раз (лучше короче, до 20 МБ) "
                "или пришлите текстом."
            )
            return
        audio = buffer.getvalue()
        if not audio:
            await message.answer("⚠️ Telegram прислал пустой файл — запишите голосовое заново.")
            return
        try:
            text = await pipeline.transcribe(audio, filename=f"voice{suffix}")
        except STTError as exc:
            log.warning("распознавание не удалось: %s", exc)
            await message.answer(
                "⚠️ Не удалось распознать голосовое.\n\n"
                "Что сделать: повторите запись ближе к микрофону, говорите чуть медленнее "
                "или просто напишите просьбу текстом."
            )
            return
        if not text.strip():
            await message.answer("Пустая расшифровка — попробуйте ещё раз.")
            return
        await run_and_reply(message, text, prefix=f"🎙 Распознано: «{text.strip()}»")

    @router.message(F.text.startswith("/parts_test"))
    async def on_parts_test(message: Message) -> None:
        """Почему бот не нашёл запчасть: по шагам, что ответила панель (только чтение)."""
        query = (message.text or "").split(maxsplit=1)
        if len(query) < 2 or not query[1].strip():
            await message.answer(
                "Напишите так: /parts_test W914/2 или /parts_test фильтр масляный манн\n"
                "Я пройду по складу и номенклатуре и покажу, что именно ответила панель."
            )
            return
        text_in = query[1].strip()
        await message.answer(f"🔎 Проверяю подбор «{text_in}» (только чтение)…")
        from ..partcheck import format_checks, run_partcheck, summary

        checks = await asyncio.to_thread(run_partcheck, tools, text_in)
        body = "\n".join(format_checks(checks)) + "\n\n" + summary(checks)
        for piece in chunk(body):
            await message.answer(piece)

    @router.message(F.text.startswith("/mode"))
    async def on_mode(message: Message) -> None:
        """Прямой ответ на вопрос «в каком режиме работаю и вижу ли настоящие сделки»."""
        from .. import CODE_STAMP, CODE_WAVE
        version = f"\nВерсия кода: волна {CODE_WAVE} от {CODE_STAMP}."
        if cfg.mock:
            await message.answer(
                "🔧 Режим: ДЕМО (STOCRM_MOCK=1).\n"
                "Бот работает на выдуманных данных: настоящих сделок он НЕ видит, "
                "в панель ничего не записывается.\n\n"
                "Чтобы работать по-настоящему: в файле .env поставьте STOCRM_MOCK=0, "
                "заполните STOCRM_DOMAIN и STOCRM_SID и перезапустите бота." + version
            )
            return
        await message.answer(
            f"🔧 Режим: БОЕВОЙ (STOCRM_MOCK=0).\n"
            f"Панель: {cfg.domain}.stocrm.ru\n"
            "Сделки и остатки читаются из настоящей панели. Подтверждение записи "
            "зависит от текущих настроек в /settings; отдельные действия могут выполняться сразу. "
            "Проверить связь: /crm_test." + version
        )

    @router.message(F.text.startswith("/crm_test"))
    async def on_crm_test(message: Message) -> None:
        """Проверка интеграции с панелью: только чтение, ничего не меняется."""
        await message.answer("🔎 Проверяю связь с STOCRM (только чтение)…")
        checks = await asyncio.to_thread(check_crm, tools, mock=cfg.mock)
        lines = format_checks(checks)
        await message.answer("Проверка STOCRM:\n" + "\n".join(lines) + "\n\n" + summary(checks))
        if cfg.mock:
            return
        failed = [c for c in checks if c.status == "fail"]
        if failed:
            return
        await message.answer(
            "Дальше можно проверить «на живом» — просто спросите меня:\n"
            "• «Покажи воронки» — чтение списка\n"
            "• «Что по заявке 123?» — карточка сделки\n"
            "• «Есть ли на складе фильтр W914/2?» — остатки\n"
            "• «Создай заявку тестовая проверка» — появится черновик с кнопкой «Отмена»: "
            "нажмите её, чтобы ничего не записалось в панель"
        )

    @router.message(F.text.startswith("/voice_test"))
    async def on_voice_test(message: Message) -> None:
        await message.answer("🔎 Проверяю распознавание речи…")
        ok, details = await probe(pipeline.s)
        if ok:
            await message.answer(
                f"✅ Распознавание работает: {details}\n\n"
                "Если голосовое всё равно не расшифровывается — пришлите его ещё раз; "
                "лог лежит в .state/bot.log"
            )
            return
        await message.answer(
            f"⚠️ Распознавание не работает: {details}\n\n"
            "Что делать: откройте .env и проверьте строки STT_PROVIDER, STT_API_KEY, STT_MODEL "
            "(ключ Groq — console.groq.com/keys). Голосовые принимаются и без ключа, "
            "но расшифровываться не будут."
        )

    @router.message(F.voice)
    async def on_voice(message: Message) -> None:
        if message.voice is None:
            return
        await handle_audio(message, message.voice.file_id, ".ogg", message.voice.file_size)

    @router.message(F.audio)
    async def on_audio(message: Message) -> None:
        if message.audio is None:
            return
        await handle_audio(message, message.audio.file_id, ".mp3", message.audio.file_size)

    @router.message(F.video_note)
    async def on_video_note(message: Message) -> None:
        await message.answer("Кружочки пока не поддерживаю — пришлите голосовое или текст.")

    # ---------------------------------------------------------- текст
    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        # «пришлите ID нового сотрудника» — это не просьба к CRM
        if await _pending_access_add(message, actor_of(message.from_user)):
            return
        if message.bot is not None:
            await message.bot.send_chat_action(message.chat.id, "typing")
        await run_and_reply(message, message.text or "")

    @router.message(F.contact)
    async def on_contact(message: Message) -> None:
        if await _pending_access_add(message, actor_of(message.from_user)):
            return
        await message.answer("Контакт получен. Чтобы открыть доступ, откройте /users → «➕ Добавить».")

    @router.message()
    async def on_other(message: Message) -> None:
        if await _pending_access_add(message, actor_of(message.from_user)):
            return
        await message.answer("Пришлите голосовое сообщение, аудиофайл или текст.")

    @router.callback_query(F.data.startswith("book:"))
    async def on_booking_choice(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        entry = pending_choices.get(parts[1]) if len(parts) == 3 else None
        choice = (entry or {}).get("choice", {})
        user_id = callback.from_user.id
        pending = pipeline._bookings.get(user_id)
        if (not entry or pending is None or choice.get("kind") != "booking" or choice.get("owner") != user_id
                or pending != choice.get("pending")):
            await callback.answer("Этот выбор уже неактуален или принадлежит другому сотруднику.", show_alert=True)
            return
        if not parts[2].isdigit() or not 1 <= int(parts[2]) <= len(pending.get("options") or []):
            await callback.answer("Некорректный вариант", show_alert=True)
            return
        # Другие кнопки этого же старого вопроса также более не действуют.
        for old_token, old_entry in list(pending_choices.items()):
            old_choice = old_entry.get("choice", {})
            if old_choice.get("kind") == "booking" and old_choice.get("owner") == user_id:
                pending_choices.pop(old_token, None)
        await callback.answer()
        if isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await run_and_reply(callback.message, parts[2], actor_id=user_id)

    # ------------------------------------------------ выбор исполнителя
    @router.callback_query(F.data.startswith("emp:"))
    async def on_employee_choice(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
            if parts[1:2] == ["noop"] or "noop" in parts:
                await callback.answer()          # кнопка-счётчик страниц
            else:
                await callback.answer("Некорректная кнопка")
            return
        _, token, value = parts[0], parts[1], parts[2]
        entry = pending_choices.get(token)

        if value == "noop":
            await callback.answer()
            return
        if entry is None:
            await callback.answer("Этот выбор уже неактуален — повторите просьбу.", show_alert=True)
            return

        # перелистывание списка
        if value == "p" and len(parts) == 4:
            try:
                page = int(parts[3])
            except ValueError:
                await callback.answer("Некорректная кнопка")
                return
            await safe_edit_markup(
                _cb_msg(callback),
                employee_keyboard(token, entry["choice"]["candidates"], page=page),
            )
            await callback.answer()
            return

        if value == "cancel":
            pending_choices.pop(token, None)
            await safe_edit(_cb_msg(callback), "❌ Отменено, ничего не создано.")
            await callback.answer()
            return

        # «Без исполнителя» — переспрашиваем, чтобы не проскочило случайно
        if value == "skip":
            await safe_edit(_cb_msg(callback), SKIP_CONFIRM_TEXT, reply_markup=skip_confirm_keyboard(token))
            await callback.answer()
            return

        # вернуться к списку сотрудников
        if value == "back":
            await safe_edit(_cb_msg(callback), 
                render_choice(entry["choice"]),
                reply_markup=employee_keyboard(token, entry["choice"]["candidates"]),
            )
            await callback.answer()
            return

        user_id: int | None = None
        if value not in ("skip_yes",):
            if not value.isdigit():
                await callback.answer("Некорректный сотрудник")
                return
            user_id = int(value)

        pending_choices.pop(token, None)
        try:
            result = await asyncio.to_thread(pipeline.resume_choice, entry["choice"], user_id)
        except (StocrmError, ValueError, KeyError) as exc:
            # Раньше ошибка показывалась ДВАЖДЫ (всплывающее окно + текст с тем же
            # содержанием) — на скриншоте 30.10 так и вышло: две одинаковые «Что сделать».
            text = humanize_error(exc, context="Не удалось выполнить")
            await safe_edit_markup(_cb_msg(callback), None)   # кнопки не должны врать
            if callback.message is not None:
                await notify_problem(callback, text)
            else:
                await callback.answer(clip(text), show_alert=True)
            return
        if user_id is None:
            await safe_edit(_cb_msg(callback), "⏭ Хорошо, создаю без исполнителя.")
        else:
            who = next(
                (c["name"] for c in entry["choice"]["candidates"] if c["user_id"] == user_id),
                f"user_id={user_id}",
            )
            await safe_edit(_cb_msg(callback), f"👤 Выбран исполнитель: {who}")

        text_out = without_repeated_list(result.answer or "Готово.", result.drafts)
        if result.applied:
            text_out += "\n\n⚡ Внесено без подтверждения:\n" + render_applied(result.applied)
        if result.drafts:
            text_out += "\n\n" + drafts_block(result.drafts, text_out)
            warnings = sorted({str(d["warning"]) for d in result.drafts if d.get("warning")})
            if warnings:
                text_out += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        markup = drafts_keyboard(result.drafts)
        if callback.message is None:
            if callback.bot is not None:
                with contextlib.suppress(Exception):
                    await callback.bot.send_message(
                        callback.from_user.id, text_out, reply_markup=markup
                    )
        else:
            await callback.message.answer(text_out, reply_markup=markup)
        await callback.answer("Готово")

    # ------------------------------------------------ вопросы сценария ТО
    @router.callback_query(F.data.startswith("to:"))
    async def on_to_choice(callback: CallbackQuery) -> None:
        parts_data = (callback.data or "").split(":")
        if len(parts_data) < 3:
            await callback.answer("Некорректная кнопка")
            return
        token, value = parts_data[1], parts_data[2]
        entry = pending_choices.get(token)
        if value == "noop":
            await callback.answer()
            return
        if entry is not None and entry["choice"].get("_group"):
            key = callback.from_user.id if callback.from_user else 0
            if pipeline._part_pending.get(key, {}).get("choice") is not entry["choice"]:
                await callback.answer("Этот вопрос устарел; используйте последние кнопки.", show_alert=True)
                return
        if value == "cancel":
            pending_choices.pop(token, None)
            await safe_edit(_cb_msg(callback), "❌ Отменено, по ТО ничего не добавлено.")
            await callback.answer()
            return
        if entry is None:
            await callback.answer("Этот выбор уже неактуален — повторите просьбу.", show_alert=True)
            return
        if not value.isdigit():
            await callback.answer("Некорректная кнопка")
            return
        pending_choices.pop(token, None)
        try:
            actor_id = callback.from_user.id if callback.from_user else None
            result = await asyncio.to_thread(
                pipeline.resume_choice, entry["choice"], int(value), user_key=actor_id)
        except (StocrmError, ValueError, KeyError) as exc:
            await safe_edit(_cb_msg(callback), "⚠️ " + humanize_error(exc, context="Не удалось собрать ТО"))
            await callback.answer("Не удалось", show_alert=True)
            return
        await callback.answer("Готово")
        target = _cb_msg(callback)
        if target is None:
            return
        if result.choices:
            for choice in result.choices:
                new_token = remember_choice(choice)
                if choice.get("kind") == "to":
                    await target.answer(render_to_choice(choice),
                                        reply_markup=to_keyboard(new_token, choice.get("candidates") or []))
                elif choice.get("kind") == "part":
                    await target.answer(render_part_choice(choice),
                                        reply_markup=part_keyboard(new_token, choice["candidates"], text_only=choice.get("_group_step") in {"price", "offer"}))
        # Черновики показываем ВСЕГДА: иначе после «Стандартное ТО» человек видел только
        # вопрос про масло, а подобранные фильтры висели в черновиках без кнопок.
        text_out = result.answer or "Готово."
        if result.applied:
            text_out += "\n\n⚡ Внесено без подтверждения:\n" + render_applied(result.applied)
        if result.drafts:
            text_out += "\n\n" + drafts_block(result.drafts, text_out)
            warnings = sorted({str(d["warning"]) for d in result.drafts if d.get("warning")})
            if warnings:
                text_out += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        for piece in chunk(text_out):
            await target.answer(piece, reply_markup=drafts_keyboard(result.drafts))

    # ------------------------------------------------ выбор запчасти
    @router.callback_query(F.data.startswith("prt:"))
    async def on_part_choice(callback: CallbackQuery) -> None:
        parts_data = (callback.data or "").split(":")
        if len(parts_data) < 3:
            await callback.answer("Некорректная кнопка")
            return
        _, token, value = parts_data[0], parts_data[1], parts_data[2]
        entry = pending_choices.get(token)

        if value == "noop":
            await callback.answer()
            return
        if value == "cancel":
            pending_choices.pop(token, None)
            if entry is not None:
                pipeline.forget_part_choice(callback.from_user.id, entry["choice"])
            await safe_edit(_cb_msg(callback), "❌ Отменено, запчасть не добавлена.")
            await callback.answer()
            return
        if entry is None:
            await callback.answer("Этот выбор уже неактуален — повторите просьбу.", show_alert=True)
            return
        if value == "p" and len(parts_data) == 4:
            try:
                page = int(parts_data[3])
            except ValueError:
                await callback.answer("Некорректная кнопка")
                return
            await safe_edit_markup(
                _cb_msg(callback),
                part_keyboard(token, entry["choice"]["candidates"], page=page),
            )
            await callback.answer()
            return

        if value in {"manual", "retry", "skip"}:
            key = callback.from_user.id if callback.from_user else 0
            # Старая кнопка не должна заменять более новый ожидающий вопрос.
            active = pipeline._part_pending.get(key, {}).get("choice")
            if active is not entry["choice"]:
                await callback.answer("Этот вопрос устарел; используйте последние кнопки.", show_alert=True)
                return
            pending_choices.pop(token, None)
            command = {"manual": "добавь как указано", "retry": "повтори поиск", "skip": "пропусти"}[value]
            await callback.answer()
            try:
                result = await pipeline.handle_text(command, [], user_id=key)
            except (StocrmError, ValueError, KeyError) as exc:
                pending_choices[token] = entry
                pipeline._part_pending[key] = {"choice": entry["choice"], "at": time.time()}
                target = _cb_msg(callback)
                if target is not None:
                    await target.answer(humanize_error(exc, context="Не удалось подготовить позицию") + " Данные сохранены; можно повторить действие.")
                return
            await safe_edit(_cb_msg(callback), "Выбрано: " + command)
            target = _cb_msg(callback)
            if target is not None:
                for choice in result.choices:
                    new_token = remember_choice(choice)
                    await target.answer(render_part_choice(choice), reply_markup=part_keyboard(new_token, choice.get("candidates") or [], text_only=choice.get("_group_step") in {"price", "offer"}))
                text_out = result.answer or "Продолжаем."
                if result.applied:
                    text_out += "\n\n⚡ Внесено без подтверждения:\n" + render_applied(result.applied)
                if result.drafts:
                    text_out += "\n\n" + drafts_block(result.drafts, text_out)
                if not result.choices:
                    for piece in chunk(text_out):
                        await target.answer(piece, reply_markup=drafts_keyboard(result.drafts))
            return
        pending_choices.pop(token, None)
        chosen = next(
            (c for c in entry["choice"]["candidates"] if str(c.get("index")) == value),
            None,
        )
        try:
            result = await asyncio.to_thread(
                pipeline.resume_choice, entry["choice"], int(value),
                user_key=callback.from_user.id if callback.from_user else None,
            )
        except (StocrmError, ValueError, KeyError) as exc:
            await safe_edit(_cb_msg(callback), "⚠️ " + humanize_error(exc, context="Не удалось добавить запчасть"))
            await callback.answer("Не удалось", show_alert=True)
            return

        if chosen and (result.drafts or result.choices or result.pending_price or result.applied):
            label = "Поставщик" if entry["choice"].get("role") == "supplier" else "Выбрано"
            await safe_edit(_cb_msg(callback), f"🔧 {label}: {chosen.get('title') or chosen.get('name')}")
        text_out = result.answer or "Готово."
        if result.applied:
            text_out += "\n\n⚡ Внесено без подтверждения:\n" + render_applied(result.applied)
        if result.drafts:
            text_out += "\n\n" + drafts_block(result.drafts, text_out)
            warnings = sorted({str(d["warning"]) for d in result.drafts if d.get("warning")})
            if warnings:
                text_out += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        target = _cb_msg(callback)
        if result.choices and target is not None:
            for choice in result.choices:
                new_token = remember_choice(choice)
                if choice.get("kind") == "part":
                    await target.answer(
                        render_part_choice(choice),
                        reply_markup=part_keyboard(new_token, choice["candidates"], text_only=choice.get("_group_step") in {"price", "offer"}),
                    )
                    continue
                if choice.get("kind") == "to":
                    await target.answer(
                        render_to_choice(choice),
                        reply_markup=to_keyboard(new_token, choice.get("candidates") or []),
                    )
                    continue
                await target.answer(
                    render_choice(choice),
                    reply_markup=employee_keyboard(new_token, choice["candidates"], allow_skip=True),
                )
        if callback.message is not None and not (result.choices and entry["choice"].get("_group")):
            await callback.message.answer(text_out, reply_markup=drafts_keyboard(result.drafts))
        await callback.answer("Продолжаем" if result.choices else "Готово")

    # ---------------------------------------------------------- подтверждение
    @router.callback_query(F.data.startswith(("ap:", "dr:", "dp:", "rv:", "rc:", "rp:", "ra:", "rx:")))
    async def on_callback(callback: CallbackQuery) -> None:
        action, _, token = (callback.data or "").partition(":")
        actor = callback.from_user
        try:
            if action in ("ra", "rx") or (action == "dr" and tools.drafts.get(token).get("status") == "partial"):
                if action != "rx":
                    await safe_edit(_cb_msg(callback), "Убрать черновик из списка? Это НЕ удалит и НЕ отменит уже созданные позиции в CRM.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Да, убрать из списка", callback_data=f"rx:{token}"),
                        InlineKeyboardButton(text="Проверить вместо удаления", callback_data=f"rv:{token}")]]))
                else:
                    tools.drafts.archive(token)
                    await safe_edit(_cb_msg(callback), "Черновик убран из списка. Данные в CRM не изменены; уже выполненные шаги не отменены.")
            elif action == "rv":
                review = await asyncio.to_thread(tools.review_draft, token)
                lines = ["Проверка CRM (без изменений):"]
                for item in review["details"]:
                    lines.append(f"Сделка №{item['offer_id']} · {item['sku']}: {item.get('execution', 'этап неизвестен')}; строк найдено {item['matches']}; " + (f"фактически {item['actual']}" if item['actual'] else "цена/количество не подтверждены"))
                lines.append("Цена и количество совпадают — можно подтвердить результат." if review["verified"] else "Выполнение не подтверждено. Не повторяйте всё добавление; используйте продолжение только после явного отказа из-за блокировки.")
                await safe_edit(_cb_msg(callback), "\n".join(lines), reply_markup=drafts_keyboard([{**tools.drafts.get(token), "status": "partial"}]))
            elif action == "rc":
                await asyncio.to_thread(tools.confirm_draft_result, token)
                await safe_edit(_cb_msg(callback), "✅ Цена и количество проверены в CRM. Черновик закрыт как выполненный. Повторных изменений не отправлял.")
            elif action == "rp":
                draft = await asyncio.to_thread(tools.prepare_draft_recovery, token)
                await safe_edit(_cb_msg(callback), "Подготовлен только отклонённый шаг. Проверьте и подтвердите:\n" + render_drafts([draft]), reply_markup=drafts_keyboard([draft]))
            elif action == "dr":
                draft = tools.drafts.archive(token)
                await safe_edit(_cb_msg(callback), f"❌ Отменено: {draft['summary']}")
                tools.audit.write("bot.discard", token=token, user_id=actor.id if actor else None)
            elif action == "dp":
                # «убрать одну позицию»: token:n — тот же черновик, минус одна строка
                token, _, number_text = token.partition(":")
                outcome = await asyncio.to_thread(
                    tools.drop_draft_position, token, number=int(number_text or 0))
                tools.audit.write("bot.drop_position", token=token, number=number_text,
                                  user_id=actor.id if actor else None,
                                  result=outcome.get("status"))
                if outcome.get("status") == "discarded":
                    await safe_edit(_cb_msg(callback),
                                    "❌ Убрал последнюю позицию — черновик отменён. "
                                    "В сделке ничего не изменилось.")
                    return
                updated = dict(outcome.get("draft") or {})
                text_out = (f"✂️ Убрал: {outcome.get('removed')}. "
                            f"Осталось позиций: {outcome.get('left')}.\n\n"
                            + render_drafts([updated]))
                await safe_edit(_cb_msg(callback), text_out,
                                reply_markup=drafts_keyboard([updated]))
            else:
                result = await asyncio.to_thread(tools.apply_draft, token)
                tools.audit.write(
                    "bot.apply", token=token, user_id=actor.id if actor else None,
                    method=result["method"], result=result["result"],
                )
                if result.get("queue_completed") and result.get("verified") and not result.get("warning"):
                    text = f"✅ Заявка выполнена: проверено позиций в CRM — {result['queue_completed']}."
                else:
                    text = format_apply_result(result["method"], result["result"],
                                               warning=str(result.get("warning") or (
                                                   "API принял запрос, но результат в карточке не перечитан. Проверьте его в STOCRM."
                                                   if result.get("verified") is False else "")))
                await safe_edit(_cb_msg(callback), text)
        except (StocrmError, ValueError, KeyError) as exc:
            await notify_problem(callback, humanize_error(exc, context="Не удалось выполнить действие с черновиком"))
            with contextlib.suppress(KeyError, ValueError):
                failed = tools.drafts.get(token)
                if failed.get("status") == "partial" and callback.message:
                    await callback.message.answer("Черновик требует проверки. Повтор всего добавления запрещён; доступны безопасные действия:", reply_markup=drafts_keyboard([failed]))
            return
        await callback.answer("Готово")

    def _cb_msg(callback: CallbackQuery) -> Message | None:
        """callback.message бывает InaccessibleMessage/None — тогда редактировать нечего."""
        message = callback.message
        return message if isinstance(message, Message) else None

    async def safe_edit(message: Message | None, text: str, **kwargs: Any) -> None:
        """edit_text падает, если сообщение старше 48ч, удалено или недоступно — шлём новое."""
        if message is None:
            return
        text = clip(polish(text))
        try:
            await message.edit_text(text, **kwargs)
        except Exception:  # noqa: BLE001 — телеграм-специфика, не бизнес-логика
            if isinstance(message, Message):
                with contextlib.suppress(Exception):
                    await message.answer(text, **kwargs)

    async def safe_edit_markup(message: Message | None, markup: Any) -> None:
        if message is None:
            return
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except Exception:  # noqa: BLE001
            if isinstance(message, Message):
                with contextlib.suppress(Exception):
                    await message.answer("Выберите вариант:", reply_markup=markup)

    async def set_commands() -> None:
        await bot.set_my_commands([
            BotCommand(command="start", description="Как пользоваться"),
            BotCommand(command="settings", description="Подтверждение действий"),
            BotCommand(command="users", description="Кто может пользоваться ботом"),
            BotCommand(command="staff", description="Сотрудники (исполнители)"),
            BotCommand(command="drafts", description="Черновики, ждущие решения"),
            BotCommand(command="voice_test", description="Проверить распознавание речи"),
            BotCommand(command="mode", description="Демо или боевой режим"),
            BotCommand(command="parts_test", description="Почему не нашёл запчасть"),
            BotCommand(command="crm_test", description="Проверить связь с STOCRM"),
            BotCommand(command="reset", description="Сбросить контекст"),
            BotCommand(command="help", description="Помощь"),
        ])

    return bot, dp, set_commands, tools


NETWORK_MARKERS = (
    "telegramnetworkerror", "clientconnectorerror", "proxyconnectionerror",
    "cannot connect", "connect call failed", "connection refused", "connection reset",
    "semaphore", "timed out", "timeout", "network is unreachable", "getaddrinfo",
    "ssl", "certificate",
)


def _is_network_error(exc: BaseException) -> bool:
    """Отличает сетевые сбои от настоящих ошибок кода."""
    if isinstance(exc, (TelegramNetworkError, OSError)):
        return True
    text = f"{type(exc).__module__} {type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in NETWORK_MARKERS)


def probe_telegram_route(bs: BotSettings) -> Route:
    """Быстро проверяет, как достучаться до Telegram: напрямую, через прокси из .env, через системный.

    Раньше прокси навязывался всем подряд, и это ломало то, что работало напрямую.
    """
    url = (bs.api_server or "https://api.telegram.org").rstrip("/")
    # 1) напрямую (если явный прокси не задан)
    if not bs.proxy_url:
        route = netprobe(url, timeout=PROBE_TIMEOUT)
        if route.ok:
            return route
    # 2) прокси из .env / окружения
    if bs.proxy_url:
        route = netprobe(url, explicit=bs.proxy_url, timeout=PROBE_TIMEOUT)
        if route.ok:
            return route
    # 3) системный прокси Windows — как последний вариант
    system, source = detect_proxy()
    if system:
        route = netprobe(url, explicit=system, timeout=PROBE_TIMEOUT)
        if route.ok:
            return Route(proxy=system, note=f"через прокси {_hide_password(system)} ({source})")
    return Route(proxy="", note="маршрут не найден: ни напрямую, ни через прокси", ok=False)


def _state_dir() -> str:
    """Папка состояния рядом с проектом, а не рядом с текущей папкой запуска."""
    configured = os.getenv("STOCRM_STATE_DIR")
    if configured:
        return configured
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), ".state")


def _write_network_log(exc: BaseException) -> None:
    """Короткая запись в .state, чтобы было что приложить к вопросу."""
    try:
        state = _state_dir()
        path = os.path.join(state, "network.log")
        os.makedirs(state, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} telegram: {type(exc).__name__}: {exc}\n")
        print(f"   Запись об ошибке: {path}")
    except OSError:
        pass


def _attach_file_log() -> None:
    """Дублирует лог в .state/bot.log — по нему видно, почему не сработало распознавание."""
    try:
        state = _state_dir()
        os.makedirs(state, exist_ok=True)
        handler = logging.FileHandler(os.path.join(state, "bot.log"), encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)


async def warm_up(tools: Any) -> None:
    """Прогрев сразу после запуска: соединение с панелью и справочники.

    Первая просьба человека не должна платить за TLS-рукопожатие и чтение
    воронок/сотрудников — это делается в фоне, пока бот ещё «просыпается».
    """
    def work() -> None:
        try:
            from .. import websearch
            websearch.prewarm()
        except Exception as exc:
            log.debug("прогрев поисковиков не удался: %s", exc)
        try:
            tools.boards()
            tools.users()
            log.debug("прогрев панели выполнен")
        except Exception as exc:      # noqa: BLE001 — прогрев не должен мешать запуску
            log.debug("прогрев панели не удался: %s", exc)

    try:
        await asyncio.to_thread(work)
    except Exception as exc:          # noqa: BLE001
        log.debug("прогрев не выполнен: %s", exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    _attach_file_log()
    try:
        bot, dp, set_commands, _tools = create_app()
    except SystemExit as exc:                      # нет ключей — сообщение уже понятное
        print(exc)
        print("Инструкция: docs/SETUP_FREE.md · проверка: python -m stocrm_agent.cli doctor")
        raise SystemExit(2) from exc
    try:
        await set_commands()
    except Exception as exc:  # noqa: BLE001 — сеть/токен: не мешаем запуску
        log.warning("не удалось зарегистрировать команды бота: %s", exc)
    # Режим виден сразу при запуске — чтобы не гадать, «видит ли бот настоящие сделки».
    app_cfg = Config.from_env(require=False)
    if app_cfg.mock:
        print()
        print("🔧 РЕЖИМ: ДЕМО (STOCRM_MOCK=1) — выдуманные данные.")
        print("   Настоящих сделок бот НЕ видит, в панель ничего не записывается.")
        print("   Для боевого режима: в .env поставьте STOCRM_MOCK=0, заполните")
        print("   STOCRM_DOMAIN и STOCRM_SID и запустите бота заново.")
        print()
    else:
        log.info("Режим: боевой (STOCRM_MOCK=0), панель %s.stocrm.ru", app_cfg.domain)
    from .. import CODE_STAMP, CODE_WAVE
    log.info("Код: волна %s от %s (если номер старее ожидаемого — файлы не обновились)",
             CODE_WAVE, CODE_STAMP)
    import hashlib
    from pathlib import Path
    from ..diagnostics import write_runtime_snapshot
    boot_record = {"event": "bot.lifecycle.start", "wave": CODE_WAVE, "pid": os.getpid(), "created_at": time.time(), "code_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_runtime_snapshot(Path(app_cfg.state_dir), boot_record)
    _tools.audit.write("bot.lifecycle.start", **{k: v for k, v in boot_record.items() if k != "event"})
    # прогрев панели — в фоне, запуску не мешает
    warm = asyncio.create_task(warm_up(_tools))
    log.info("Бот запущен (опрос Telegram). Остановить: Ctrl+C")
    try:
        await dp.start_polling(bot)
    except Exception as exc:
        if not _is_network_error(exc):
            raise
        print()
        print("⛔ Нет соединения с api.telegram.org — Telegram недоступен из этой сети.")
        print(f"   Техническая причина: {exc}")
        print()
        print("   Это не ошибка бота. Обычно так бывает, если Telegram блокируется")
        print("   провайдером, включён файрвол или нужен прокси. Что делать:")
        print("     1. Откройте в браузере https://api.telegram.org — если не открывается,")
        print("        значит доступ закрыт на уровне сети.")
        print("     2. Укажите прокси в .env (любой SOCKS5 или HTTP):")
        print("        TG_PROXY_URL=socks5://ЛОГИН:ПАРОЛЬ@ХОСТ:1080")
        print("        TG_PROXY_URL=http://ХОСТ:8080")
        print("     3. Либо запустите свой Bot API server и укажите:")
        print("        TG_API_SERVER=http://localhost:8081")
        print("     4. Проверка: python -m stocrm_agent.cli doctor")
        print()
        _write_network_log(exc)
        raise SystemExit(2) from None
    except TelegramUnauthorizedError:
        print("\n⛔ Telegram не принял токен TG_BOT_TOKEN.\n"
              "   Получите новый у @BotFather (/mybots → API Token) и впишите в .env.\n"
              "   Проверка: python -m stocrm_agent.cli doctor")
        raise SystemExit(2) from None
    except asyncio.CancelledError:
        pass
    finally:
        boot_record.update({"event": "bot.lifecycle.stop", "ts": time.time()})
        write_runtime_snapshot(Path(app_cfg.state_dir), boot_record)
        _tools.audit.write("bot.lifecycle.stop", wave=CODE_WAVE, pid=os.getpid())
        warm.cancel()
        try:
            _tools.client.close()          # закрываем соединения с панелью
        except Exception as exc:           # noqa: BLE001
            log.debug("соединения с панелью не закрылись: %s", exc)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
