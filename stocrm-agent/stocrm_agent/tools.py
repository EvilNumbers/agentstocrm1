"""Инструменты агента над STOCRM.

Разделены на две группы:
  * чтение (READ_*) — выполняются сразу;
  * изменения (draft_*) — создают черновик, который применяется только после
    подтверждения человеком через apply_draft().

Эндпоинты и поля сверены с официальным разделом Public API в wiki STOCRM
(снимки документации лежат в каталоге api_docs/).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Any

from contextvars import copy_context

from .client import read_budget
from . import parts, profit_liga, rossko
from .booking import BookingMixin
from .calendar import CalendarMixin
from .audit import AuditLog
from .client import StocrmClient, StocrmError, _as_items, _forbidden_supplier_order
from .contact_search import detail_phones, name_key, phone_key, phone_matches, title_of, unique_phones
from .draft_recovery import DraftRecoveryMixin, lock_refusal
from .drafts import DraftStore
from .envfile import env_float
from .human import METHOD_TITLES, clean_html

log = logging.getLogger("stocrm.tools")

#: Один пул потоков на процесс: потоки и их соединения к панели переиспользуются.
_PARALLEL_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="stocrm-read")
_IN_PARALLEL = threading.local()


def _same_id(a: Any, b: Any) -> bool:
    """Сравнение ID, пришедших строками и числами."""
    return str(a).strip() == str(b).strip()

# --------------------------------------------------------------------- helpers


# STOCRM отдаёт даты в часовом поясе сервера (+03:00). Наивные даты из речи
# («с 1 октября») тоже трактуем в этом поясе, иначе границы дня уезжают на 3 часа.
CRM_TZ = dt.timezone(dt.timedelta(hours=env_float("STOCRM_TZ_OFFSET", 3.0,
                                                 minimum=-12.0, maximum=14.0)))


def to_ts(value: Any) -> int | None:
    """Приводит дату к unix-timestamp. Принимает int, '2026-10-01', ISO-строку."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CRM_TZ)
        return int(parsed.timestamp())
    raise ValueError(f"Не удалось разобрать дату: {value!r} (ожидается 2026-10-01 или unix-timestamp)")


def date_range(date_from: Any, date_to: Any) -> list[int]:
    start = to_ts(date_from) or 0
    end = to_ts(date_to) or int(time.time())
    return [start, end]


def _extract_id(value: Any) -> Any:
    """Достаёт числовой ID из ответа предыдущего шага API.

    Понимает плоские ответы (19661), словари с известными ключами
    ({"REQUIRE_ID": 3715}), вложенные ({"3715": {"REQUIRE_ID": 3715}},
    {"WORK_ID": 402944, "OFFER_ID": 1}, {"WORKS": {...}}) и списки.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(value) if value.isdigit() else value

    known = ("REQUIRE_ID", "ORDER_ID", "WORK_ID", "TASK_ID", "OFFER_ID", "CONTACT_ID",
             "TASK_MEMBER_ID", "ID", "RESPONSE")
    if isinstance(value, dict):
        for key in known:
            if key in value:
                found = _extract_id(value[key])
                if isinstance(found, int):
                    return found
        for key in ("WORKS", "WORK", "DATA"):
            if key in value:
                found = _extract_id(value[key])
                if isinstance(found, int):
                    return found
        # вложенные словари: {"3715": {"REQUIRE_ID": 3715}}
        for item in value.values():
            found = _extract_id(item)
            if isinstance(found, int):
                return found
        return value

    if isinstance(value, list) and value:
        return _extract_id(value[-1])
    return value


#: Как называть виды оплаты по-русски (ключ ответа `payment/get_balance`).
PAYMENT_KINDS = {
    "CASH": "наличные",
    "BANK_CARD": "карта",
    "BLACK_CASH": "наличные (НФ)",
    "BLACK_BANK_CARD": "карта (НФ)",
    "CASHLESS": "безнал",
    "TRANSFER": "перевод",
}


def _payments_total(balances: dict[str, Any]) -> dict[str, Any]:
    """Сумма оплат по сделке из ответа `payment/get_balance` + разбивка по видам.

    Панель отдаёт баланс по каждой валюте и виду оплаты (`RUB_CASH`, `RUB_CASHLESS`…).
    Раньше бот эти цифры вообще не показывал — человек видел только «Оплата: Нет
    оплаты» и не мог узнать, сколько уже заплатили и сколько осталось.
    """
    total = 0.0
    kinds: dict[str, float] = {}
    for key, raw in (balances or {}).items():
        value = _money(raw)
        if not value:
            continue
        total += value
        # Ключ баланса — «RUB_CASHLESS», «RUB_BLACK_CASH», «RUB_CASH». Вид оплаты берём
        # из второй части ключа и сравниваем ТОЧНО: иначе «RUB_CASHLESS» попадал под
        # проверку «CASH» и безнал показывался как наличные.
        kind = str(key)
        short = kind.split("_", 1)[1] if "_" in kind else kind
        if short in PAYMENT_KINDS:
            kind = PAYMENT_KINDS[short]
        else:
            for known, title in PAYMENT_KINDS.items():
                if kind.endswith(f"_{known}") or known == short:
                    kind = title
                    break
        kinds[kind] = round(kinds.get(kind, 0.0) + value, 2)
    return {"total": round(total, 2), "by_kind": kinds}


def _reasons(raw: Any) -> list[dict[str, Any]]:
    """Причины перевода в статус из ответа `offer/all_statuses`.

    Панель отдаёт их по-разному: `false` (причин нет), список словарей или словарь
    «ID → название». Приводим к одному виду.
    """
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                reason_id = value.get("REASON_ID") or value.get("ID") or key
                title = value.get("TITLE") or value.get("NAME") or value.get("REASON")
            else:
                reason_id, title = key, value
            if reason_id not in (None, "", False):
                out.append({"reason_id": reason_id, "title": title})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                reason_id = item.get("REASON_ID") or item.get("ID")
                if reason_id not in (None, "", False):
                    out.append({"reason_id": reason_id,
                                "title": item.get("TITLE") or item.get("NAME")})
            elif item not in (None, "", False):
                out.append({"reason_id": item, "title": None})
    return out


def _clone_value(value: Any) -> Any:
    """Независимая копия данных для кэша — быстрее `copy.deepcopy` в разы.

    `deepcopy` пятисот строк склада стоит ~2.7 мс на КАЖДОЕ чтение из кэша, а
    рекурсивная копия тех же данных — ~1 мс. Копируем только контейнеры (dict/list),
    остальное (строки, числа) неизменяемо.
    """
    if isinstance(value, dict):
        return {key: _clone_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_value(item) for item in value]
    if isinstance(value, (set, bytearray)):
        return value.copy()
    return value


def _note_matches(text: str, needle: str) -> bool:
    """Похоже ли слово человека на текст заметки.

    Человек говорит «про доставку», а в заметке «Доставка после 18:00»: точного
    совпадения нет, поэтому сравниваем ещё и по основе слова (без хвоста -у/-а/-и).
    """
    haystack = parts.normalize_text(text)
    if not needle:
        return False
    if needle in haystack:
        return True
    stem = needle[:-1] if len(needle) > 5 else needle
    stem = stem[:-1] if len(stem) > 5 else stem
    return bool(stem) and stem in haystack


def _human_write_error(exc: Exception, method: str, tool: str = "") -> str:
    """Переводит отказ панели на человеческий язык.

    Самая частая причина — «Заблокировано: <сотрудник>»: карточку в STOCRM держит
    открытой другой человек (или ваша же вкладка в браузере), и панель не даёт в неё
    писать. Раньше бот показывал это как «шаг 1 из 2 не выполнен» и навсегда
    блокировал черновик — человек не понимал ни что случилось, ни что делать.
    """
    text = " ".join(clean_html(str(exc)).split()) or "без пояснений от STOCRM"
    lowered = text.lower()
    if "заблокирован" in lowered or "blocked" in lowered or "locked" in lowered:
        who = ""
        match = re.search(r"заблокирован\w*\s*:?\s*(.*)", text, re.IGNORECASE)
        if match:
            who = match.group(1).strip(" .")
        holder = f" Её держит открытой: {who}." if who else ""
        return (
            "STOCRM не дал записать: карточка сейчас заблокирована." + holder + "\n"
            "Что сделать: попросите этого сотрудника закрыть карточку сделки (если это вы — "
            "закройте её в браузере) и нажмите «Применить» ещё раз.\n"
            "Ничего не записано, черновик сохранён — дубликатов не будет."
        )
    if _no_such_method(exc):
        if tool == "remove_offer_part":
            return DELETE_MANUAL_HINT
        if tool == "update_offer_note":
            return (
                "STOCRM не даёт изменить уже созданную заметку через API — в Public API "
                "описан только метод «Создать заметку».\n"
                "Что сделать: откройте сделку в STOCRM → блок «Заметки и задачи» → "
                "поправьте текст там; либо скажите «добавь заметку …» — я создам новую.\n"
                "Старый текст не тронут, ничего не изменено."
            )
        if tool == "remove_tag":
            return TAG_MANUAL_UNLINK_HINT
        if tool == "change_part_price":
            return (
                "STOCRM не даёт менять цену уже добавленной в сделку позиции через API "
                f"(метод {method} не поддерживается).\n"
                "Что сделать: откройте сделку в STOCRM → блок «Реализация» → «Товары» → "
                "в строке нужной позиции поставьте новую цену продажи.\n"
                "Ничего не записано, вторая такая же позиция в сделку не добавлялась."
            )
        return (
            f"STOCRM не знает метод {method} — это действие в вашей версии API недоступно.\n"
            "Ничего не записано. Сообщите об этом администратору бота."
        )
    if "причин" in lowered:
        return (
            "STOCRM не дал сменить статус: панель требует причину перевода.\n"
            "Что сделать: назовите причину («утилизация», «клиент отказался») — "
            "или смените статус в самой панели, там причина выбирается из списка.\n"
            "Ничего не записано, черновик сохранён."
        )
    reason = text[:300].rstrip(" .")
    return (
        f"STOCRM не принял изменение ({method}): {reason}.\n"
        "Ничего не записано — черновик сохранён, попытку можно повторить."
    )


#: Как панель STOCRM отвечает, когда такого метода API у неё нет.
_NO_METHOD_RE = re.compile(
    r"wrong\s*uri|no\s*api\s*method|api\s*method\s*available|не\s*найден|not\s*found|"
    r"unknown\s*method|не\s*поддерж\w*|не\s*существу\w*",
    re.IGNORECASE,
)


def _no_such_method(exc: Exception) -> bool:
    """Панель ответила «такого метода нет» (а не «неверные данные»)."""
    return bool(_NO_METHOD_RE.search(str(exc)))


#: Признаки того, что связь с панелью оборвалась и ответа мы не получили.
_BREAK_RE = re.compile(
    r"сетевая ошибка|connection|aborted|reset|timed out|timeout|не ответила|"
    r"unreachable|remotedisconnected|ssl|proxy",
    re.IGNORECASE,
)


def _is_transport_error(exc: Exception) -> bool:
    """Связь оборвалась на середине: записалось действие или нет — неизвестно.

    Панель могла успеть записать (так и было 29.10: бот написал «шаг 1 из 2 не
    выполнен», а позиция в сделке появилась). Поэтому такие ошибки нельзя ни
    выдавать за отказ панели, ни приписывать «карточку держит другой сотрудник».
    """
    if isinstance(exc, StocrmError) and exc.code is not None:
        return False                     # панель явно ответила кодом — это не обрыв
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return bool(_BREAK_RE.search(str(exc)))


def _human_break_error(method: str, tool: str = "") -> str:
    """Что сказать человеку, когда связь оборвалась во время записи."""
    what = f" ({METHOD_TITLES.get(method, method)})" if method else ""
    return (
        f"Связь с STOCRM оборвалась ровно в момент записи{what}, и панель не успела "
        "ответить.\n"
        "Что сделать: 1) откройте сделку в панели и посмотрите, появилось ли изменение; "
        "2) если изменения нет — нажмите «Применить» ещё раз (дубликата не будет: "
        "я перечитал сделку и записи нет); 3) если изменение есть — нажмите «Отмена», "
        "второй раз применять не нужно.\n"
        "Это не ваша ошибка и не блокировка карточки — просто не дошёл ответ."
    )


def _resolve_refs(payload: Any, results: list[Any]) -> Any:
    """Подставляет {"$from_step": N} реальным ID из результата шага N.

    Если предыдущий шаг не вернул ID (или такого шага нет), бросаем понятную ошибку:
    раньше заглушка `{"$from_step": 0}` уходила в панель как значение поля.
    """
    if isinstance(payload, dict):
        if "$from_step" in payload and len(payload) == 1:
            index = int(payload["$from_step"])
            if index >= len(results):
                raise ValueError(
                    f"шаг {index + 1} не выполнялся — ссылаться на его результат нельзя"
                )
            found = _extract_id(results[index])
            if found in (None, "", 0, "0"):
                raise ValueError(
                    f"шаг {index + 1} не вернул ID — продолжить нельзя, проверьте сделку в панели"
                )
            return found
        return {k: _resolve_refs(v, results) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_resolve_refs(v, results) for v in payload]
    return payload


# Какие фильтры входят в стандартное ТО и каким брендом собираем по умолчанию.
MAINTENANCE_KINDS: tuple[tuple[str, str], ...] = (
    ("воздушный", "Фильтр воздушный"),
    ("масляный", "Фильтр масляный"),
)
DEFAULT_MAINTENANCE_BRAND = "ZEKKERT"


#: Как узнать тип фильтра по названию («Фильтр воздушный C 25 114»).
KIND_STEMS: dict[str, tuple[str, ...]] = {
    "воздушный": ("воздушн",),
    "масляный": ("маслян", "масл"),
}


def _is_filter_of_kind(name: Any, kind: str) -> bool:
    """Позиция — фильтр нужного типа? «масло моторное» фильтром не считается."""
    low = str(name or "").lower().replace("ё", "е")
    if "фильтр" not in low:
        return False
    return any(stem in low for stem in KIND_STEMS.get(kind, (kind,)))


def _norm_vin(value: Any) -> str:
    """VIN без пробелов в верхнем регистре; пустое → ""."""
    return "".join(str(value or "").split()).upper()


def _num(value: Any) -> str:
    """Число для текста черновика: 6000, а не 6000.0; 5200.5 — как есть."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else f"{number:.2f}".rstrip("0").rstrip(".")


def _thousands(value: Any) -> str:
    """Цена для списка: 6900 → «6 900», 520.5 → «520,50»."""
    number = _money(value)
    text = f"{number:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return text.replace(".", ",")


def _clean_part_title(name: str, brand: str = "", sku: str = "") -> str:
    """Название позиции без повторного бренда и артикула.

    Панель часто отдаёт NAME вида «NGN V182575156», а бренд и артикул отдельно:
    в списке получалось «NGN V182575156 ABCP V182575156 × 1». Убираем дубли.
    """
    text = str(name or "").strip()
    for token in (sku, brand):
        token = str(token or "").strip()
        if not token:
            continue
        text = re.sub(rf"(?<![\w-]){re.escape(token)}(?![\w-])", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,.-—·")


def _pretty_title(text: str, limit: int = 62) -> str:
    """Название для списка: с заглавной буквы, без «|» и лишних знаков."""
    clean = re.sub(r"\s*[|/]\s*", " · ", str(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" .,—–-·")
    clean = re.sub(r"([!?])\1+", r"\1", clean)
    if clean:
        clean = clean[0].upper() + clean[1:]
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _part_line(check: dict[str, Any]) -> str:
    """Одна читаемая строка позиции: «Сальник · MITSUBISHI MB664285 × 1 — 1 000 ₽»."""
    brand = str(check.get("brand") or "").strip()
    sku = str(check.get("sku") or "").strip()
    title = _pretty_title(_clean_part_title(str(check.get("name") or ""), brand, sku))
    code = " ".join(part for part in (brand, sku) if part)
    head = title or code or "позиция"
    if code and code.lower() not in head.lower():
        head = f"{head} · {code}"
    qty = check.get("qty") or 1
    line = f"{head} × {_num(qty)}"
    price = _money(check.get("price") or 0)
    if price > 0:
        line += f" — {_thousands(price)} ₽"
        if int(qty or 1) > 1:
            line += f" (итого {_thousands(price * int(qty or 1))} ₽)"
    return line


def _named_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if value.get(key) not in (None, "", False):
            return value[key]
        for child in value.values():
            found = _named_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _named_value(child, key)
            if found is not None:
                return found
    return None


def _money(value: Any) -> float:
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _known_work_sum(value: Any) -> float | None:
    """Неизвестную/испорченную сумму услуги не подменять нулём."""
    try:
        number = float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) else None


# --------------------------------------------------------------------- toolset


#: Подтверждённого метода изменения цены строки сделки в Public API не найдено.
#: По умолчанию нет перебора угаданных endpoints. Явная настройка оставлена
#: только для совместимости с отдельно проверенными интеграциями.
DEFAULT_PRICE_METHOD = ""
# Существующий резервный путь УДАЛЕНИЯ (ACTIVE=N), не изменение цены.
# Отделён от настройки правки стоимости, чтобы не сломать удаление позиций.
DEFAULT_ARCHIVE_METHODS = ("offer/order/update", "offer/order/edit", "offer/order/change", "offer/order/update_price")

# --------------------------------------------------------------------- заметки
#: Сущности заметок STOCRM. Сделку панель отдаёт и как OFFER_NOTE (так её и создаём
#: по документации note/create), и как OFFER — читаем обе, иначе «заметок нет» на
#: пустом месте.
NOTE_ENTITIES: tuple[str, ...] = ("OFFER_NOTE", "OFFER")

#: Методы правки уже созданной заметки. В Public API описан только note/create,
#: поэтому перебираем известные написания, а после записи перечитываем заметки.
NOTE_UPDATE_METHODS: tuple[str, ...] = ("note/update", "notes/update", "note/edit", "note/save")

# ------------------------------------------------------------------------ теги
#: Сущности тегов: карточка сделки и её позиция (строка товара). Позиция в документации
#: не описана — проверяем известные названия по очереди и подтверждаем перечитыванием.
TAG_ENTITIES_OFFER: tuple[str, ...] = ("OFFER",)
TAG_ENTITIES_ORDER: tuple[str, ...] = ("OFFER_ORDER", "ORDER", "PRODUCTS_ORDER")

#: Что бот говорит, если панель не дала пометить позицию тегом.
TAG_MANUAL_UNLINK_HINT = (
    "STOCRM не дал снять тег через API: публичный метод отвязки не принимает объект "
    "(снимает все связи тега сразу), поэтому я не стал рисковать.\n"
    "Что сделать: откройте карточку в STOCRM → блок с тегами → «×» у ненужного тега.\n"
    "Ничего не изменено."
)
TAG_ORDER_MANUAL_HINT = (
    "STOCRM не принял тег у позиции в сделке: в Public API пометка строки не описана.\n"
    "Что сделать (20 секунд): STOCRM → сделка → блок «Реализация» → «Товары» → "
    "в строке позиции поставьте тег.\n"
    "Позиция при этом не изменилась — добавлена не была."
)

#: Методы удаления позиции из сделки. В Public API v1 их нет (проверено по разделу
#: «Сделка → Товары» 30.10.2026: там только добавление и два чтения), поэтому пробуем
#: по очереди все известные написания и запоминаем рабочий — как с правкой цены.
DELETE_METHODS: tuple[str, ...] = (
    "offer/order/delete",
    "offer/order/remove",
    "offer/order/del",
    "offer/order/delete_order",
    "offer/order/remove_order",
    "offer/order/delete_free_order",
)

#: Если метода удаления нет, пробуем «убрать» строку пометкой «не активна» —
#: в панели такая позиция не участвует в сумме сделки. Поле называется ACTIVE.
DELETE_ARCHIVE_FIELD = "ACTIVE"
DELETE_ARCHIVE_VALUE = "N"

#: Что бот говорит, когда удалить позицию через API не удалось ни одним методом.
DELETE_MANUAL_HINT = (
    "Позиция осталась в сделке: в Public API STOCRM нет метода удаления товара "
    "(есть только добавление и чтение), и «не активна» панель тоже не приняла.\n"
    "Что сделать (30 секунд): STOCRM → сделка → блок «Реализация» → «Товары» → "
    "в строке позиции нажмите корзину.\n"
    "Других записей бот не делал — в сделке осталось ровно то, что было."
)


class SupplierNeeds(ValueError):
    """Подготовка требует выбора, не ошибка поиска товара/сделки."""

    def __init__(self, message: str, suppliers: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.suppliers = suppliers


class Toolset(BookingMixin, CalendarMixin, DraftRecoveryMixin):
    def __init__(
        self,
        client: StocrmClient,
        drafts: DraftStore,
        audit: AuditLog,
        *,
        page_size: int = 200,
        max_pages: int = 20,
        free_form_type: str = "FREE_FORM",
        price_method: str = DEFAULT_PRICE_METHOD,
        text_pages: int = 3,
        capabilities_path: Path | None = None,
        default_supplier_id: int | None = None,
        manual_order_method: str = "offer/order/add_free_order",
    ):
        if manual_order_method not in {"offer/order/add_free_order", "offer/order/add"}:
            raise ValueError("Неподдерживаемый метод свободного товара")
        self.manual_order_method = manual_order_method
        self.client = client
        self.drafts = drafts
        self.audit = audit
        self.page_size = page_size
        self.max_pages = max_pages
        self.free_form_type = free_form_type
        # Правка уже добавленной позиции: имён может быть несколько (через запятую) —
        # в документации метода нет, поэтому пробуем по очереди, пока панель не ответит
        # «Wrong URI. No api method available».
        self.price_methods: tuple[str, ...] = tuple(
            m.strip() for m in str(price_method or "").split(",") if m.strip()
        )
        self.price_method = self.price_methods[0] if self.price_methods else ""
        self.price_rounding_step = env_float("STOCRM_PRICE_ROUNDING_STEP", 50.0, minimum=0, maximum=1000)
        self.default_supplier_id = default_supplier_id
        self.capabilities_path = capabilities_path

        # кэши справочников: поля контакта и бренды авто не меняются в течение сеанса
        self._last_step_method: str | None = None
        self._flat_fields: list[str] | None = None
        self._car_brands: list[dict[str, Any]] | None = None
        # короткий кэш списочных чтений (склад, номенклатура, история, авто клиента):
        # в одном диалоге бот спрашивает одно и то же по 2–3 раза, каждый раз платя
        # за это сетевым запросом к панели
        self._cache: dict[str, tuple[float, Any]] = {}
        self.cache_ttl = 120.0
        self._cache_lock = threading.RLock()
        self._cache_flights: dict[tuple[int, str], tuple[int, Future[Any]]] = {}
        self._cache_stats = {"hit": 0, "miss": 0, "joined": 0}
        # сколько страниц просматривать при поиске по названию/артикулу (серверного
        # фильтра по подстроке в STOCRM нет). Меньше страниц — быстрее ответ;
        # если позиции не нашлось, бот честно об этом скажет.
        self.text_pages = text_pages
        # «поколение» кэша: любая запись в CRM его увеличивает. Чтение, начатое ДО
        # записи, не станет кэшировать устаревший ответ, даже если вернулось позже.
        self._epoch = 0

    # ------------------------------------------------------------- служебное
    def _cached(self, key: str, compute: Callable[[], Any], ttl: float | None = None) -> Any:
        """Короткий кэш чтений: по умолчанию 2 минуты или до первой записи в CRM.

        ttl задаётся там, где данные заведомо стабильны (воронки, статусы,
        сотрудники) — они не меняются, пока кто-то не поправит настройки панели.
        """
        limit = self.cache_ttl if ttl is None else ttl
        owner = threading.get_ident()
        reentrant = False
        with self._cache_lock:
            epoch = self._epoch
            flight_key = (epoch, key)
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < limit:
                self._cache_stats["hit"] += 1
                return _clone_value(hit[1])
            active = self._cache_flights.get(flight_key)
            if active and active[0] != owner:
                self._cache_stats["joined"] += 1
                future = active[1]
                leader = False
            elif active:
                # Рекурсивный вызов того же ключа не должен ждать сам себя.
                reentrant = True
                leader = False
                future = active[1]
            else:
                self._cache_stats["miss"] += 1
                future = Future()
                self._cache_flights[flight_key] = (owner, future)
                leader = True
        if reentrant:
            return compute()
        if not leader:
            return _clone_value(future.result())
        try:
            value = compute()
            stored = _clone_value(value)
            with self._cache_lock:
                if epoch == self._epoch and limit > 0 and not (isinstance(value, dict) and value.get("error")):
                    # TTL начинается после получения ответа, не до медленного HTTP.
                    self._cache[key] = (time.monotonic(), stored)
                    if len(self._cache) > 200:
                        for old in sorted(self._cache, key=lambda k: self._cache[k][0])[:50]:
                            self._cache.pop(old, None)
                future.set_result(stored)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._cache_lock:
                self._cache_flights.pop(flight_key, None)

    def cache_stats(self) -> dict[str, int]:
        """Счётчики без телефонов, ключей API и содержимого запросов."""
        with self._cache_lock:
            return {**self._cache_stats, "entries": len(self._cache), "inflight": len(self._cache_flights)}

    def _run_parallel(self, tasks: list[Callable[[], None]]) -> None:
        """Выполняет независимые чтения одновременно.

        Пул потоков — общий на процесс и постоянный: потоки (а вместе с ними и
        соединения до панели) переиспользуются, поэтому повторный набор чтений
        не платит за новый TLS-хендшейк.
        """
        if len(tasks) <= 1 or getattr(_IN_PARALLEL, "active", False):
            # вложенный вызов внутри рабочего потока выполняем подряд,
            # иначе при шести задачах пул может залипнуть в ожидании самих себя
            for task in tasks:
                task()
            return
        def guarded(task: Callable[[], None]) -> None:
            # помечаем РАБОЧИЙ поток: если внутри задачи снова попросят параллельность,
            # она выполнится подряд и не займёт место в пуле (иначе — взаимная блокировка)
            _IN_PARALLEL.active = True
            try:
                task()
            finally:
                _IN_PARALLEL.active = False

        for future in [_PARALLEL_POOL.submit(copy_context().run, guarded, task) for task in tasks]:
            try:
                future.result()
            except Exception as exc:          # одна неудача не должна ломать подбор
                log.warning("параллельное чтение не удалось: %s", exc)

    def _invalidate_cache(self) -> None:
        """После записи в CRM кэш чтений больше не актуален."""
        with self._cache_lock:
            self._epoch += 1
            self._cache.clear()

    def _invalidate_quick_identity_cache(self) -> None:
        """Для точной проверки контакта/статуса обновляем справочники, не стирая поиск деталей.

        Новое поколение не даст вернуть старый in-flight результат как свежий;
        независимые завершённые кэши остаются действительными до их TTL или записи.
        """
        with self._cache_lock:
            self._epoch += 1
            prefixes = ("boards:", "statuses:", "wire:boards/get_all:", "wire:offer/all_statuses:")
            for cache_key in list(self._cache):
                if cache_key.startswith(prefixes):
                    self._cache.pop(cache_key, None)

    def _read(self, tool: str, method: str, payload: dict[str, Any] | None = None) -> Any:
        if method == 'offers/get_from_filter':
            # WIKI-A-593/595: запрашиваем поля, нужные для идентификации и карточки,
            # явно; панель может не включать некоторые поля по умолчанию.
            payload = dict(payload or {})
            required = payload.get('REQUIRED_FIELDS') or []
            if not isinstance(required, list):
                raise ValueError('Неизвестный формат REQUIRED_FIELDS')
            fields = ('OFFER_ID', 'CONTACT_ID', 'BOARD_ID', 'STATUS_ID', 'CONTACT_TITLE',
                      'BOARD_NAME', 'STATUS_NAME', 'OFFER_SUM', 'WORKS_SUM', 'ORDERS_SUM', 'PAYMENT_STATUS')
            payload['REQUIRED_FIELDS'] = list(dict.fromkeys([*required, *fields]))
        # Динамические данные объединяются только пока HTTP в полёте (TTL=0).
        # Небольшой буфер завершённых ответов — лишь для справочников.
        stable = {"boards/get_all", "offer/all_statuses", "users/get_from_filter",
                  "customers/get_filtered", "calendar/post/get"}
        key = "wire:" + method + ":" + json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
        return self._cached(key, lambda: self._read_uncached(tool, method, payload),
                            ttl=15.0 if method in stable else 0.0)

    def _read_uncached(self, tool: str, method: str, payload: dict[str, Any] | None = None, *, get: bool = False) -> Any:
        started = time.monotonic()
        try:
            # чтение: сетевую ошибку можно безопасно повторить, запись — нет
            call = self.client.call_get if get else self.client.call
            result = call(method, payload, idempotent=True)
        except StocrmError as exc:
            self.audit.write("read.error", tool=tool, method=method, payload=payload, error=str(exc))
            raise
        rows = len(_as_items(result)) if isinstance(result, (list, dict)) else 1
        self.audit.write(
            "read", tool=tool, method=method,
            payload=None if not rows else payload,      # пустое чтение не мусорим фильтрами
            ms=round((time.monotonic() - started) * 1000),
            rows=rows,
        )
        return result

    def _page(self, tool: str, method: str, *, filters=None, sort=None, page=1, limit=50, extra=None) -> list[Any]:
        payload: dict[str, Any] = {"PAGE": page, "LIMIT": limit}
        if filters:
            payload["FILTER"] = filters
        if sort:
            payload["SORT"] = sort
        if extra:
            payload.update(extra)
        return _as_items(self._read(tool, method, payload))

    # ------------------------------------------------------------------ READ
    def boards(self, only_active: bool = True) -> list[dict[str, Any]]:
        # Воронки правят раз в месяц — держим их в кэше 10 минут.
        return self._cached(f"boards:{only_active}", lambda: self._boards(only_active), ttl=600.0)

    def _boards(self, only_active: bool) -> list[dict[str, Any]]:
        raw = self._read("boards", "boards/get_all", {"ONLY_ACTIVE": "Y" if only_active else "N"})
        return [
            {"board_id": b.get("BOARD_ID"), "name": b.get("BOARD_NAME"), "active": b.get("ACTIVE")}
            for b in _as_items(raw)
        ]

    def statuses(self, board_id: int) -> list[dict[str, Any]]:
        return self._cached(f"statuses:{int(board_id)}",
                            lambda: self._statuses(int(board_id)), ttl=600.0)

    def _statuses(self, board_id: int) -> list[dict[str, Any]]:
        raw = self._read("statuses", "offer/all_statuses", {"BOARD_ID": int(board_id)})
        out = []
        for s in _as_items(raw):
            out.append({
                "status_id": s.get("STATUS_ID"),
                "board_id": s.get("BOARD_ID"),
                "title": s.get("TITLE"),
                "front_color": s.get("FRONT_COLOR"),
                "is_final": s.get("IS_FINAL"),
                "is_new": s.get("IS_NEW"),
                "active": s.get("ACTIVE"),
                "good_next": s.get("GOOD_STATUSES"),
                # причины перевода: панель требует REASON_ID и без него отвечает
                # «Не указана причина перевода» (так и упала смена статуса 29.10)
                "reasons": _reasons(s.get("REASONS")),
            })
        return out

    def find_offers(
        self,
        *,
        board_id: int | None = None,
        status_id: int | None = None,
        responsible_id: int | None = None,
        contact_id: int | None = None,
        payment_status: str | None = None,
        date_from: Any = None,
        date_to: Any = None,
        limit: int = 25,
        page: int = 1,
        fresh: bool = False,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if board_id:
            filters["BOARD_ID"] = int(board_id)
        if status_id:
            filters["STATUS_ID"] = int(status_id)
        if responsible_id:
            filters["OFFER_RESPONSIBLE_ID"] = int(responsible_id)
        if contact_id:
            filters["CONTACT_ID"] = int(contact_id)
        if payment_status:
            filters["PAYMENT_STATUS"] = payment_status
        if date_from or date_to:
            filters["DATE_CREATE"] = date_range(date_from, date_to)

        key = "offers:" + json.dumps([filters, limit, page], sort_keys=True, default=str)
        def read_rows() -> list[Any]:
            return self._page("find_offers", "offers/get_from_filter",
                filters=filters or None, sort={"DATE_CREATE": "DESC"}, limit=limit, page=page)
        rows = read_rows() if fresh else self._cached(key, read_rows, ttl=20.0)
        # Строка без OFFER_ID означает, что ответ панели не разобрался (неизвестная
        # обёртка). Такую «сделку» показывать нельзя — иначе бот расскажет про пустышку.
        valid = [r for r in rows if isinstance(r, dict) and r.get("OFFER_ID") not in (None, "", False)]
        if fresh and len(valid) != len(rows):
            raise ValueError("Ответ CRM содержит непроверяемые строки сделок")
        if rows and not valid:
            log.warning("find_offers: панель вернула %s строк без OFFER_ID — ответ не разобран", len(rows))
        return [self._slim_offer(r) for r in valid]

    @staticmethod
    def _slim_offer(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "offer_id": r.get("OFFER_ID"),
            "board": r.get("BOARD_NAME"),
            "board_id": r.get("BOARD_ID"),
            "status": r.get("STATUS_NAME"),
            "status_id": r.get("STATUS_ID"),
            "contact_id": r.get("CONTACT_ID"),
            "contact": r.get("CONTACT_TITLE"),
            "car": r.get("CAR_TITLE_TABLE"),
            "plate": r.get("CAR_NUMBER_FLAT"),
            "vin": r.get("CAR_VIN") or r.get("VIN"),
            "car_profile_id": r.get("CAR_PROFILE_ID"),
            "sum": _money(r.get("OFFER_SUM")),
            "works_sum": _money(r.get("WORKS_SUM")),
            "parts_sum": _money(r.get("ORDERS_SUM")),
            "payment_status": r.get("PAYMENT_STATUS"),
            "responsible": r.get("OFFER_RESPONSIBLE"),
            "created": r.get("OFFER_DATE_CREATE_FRONTEND_TIMESTAMP"),
            "source": r.get("OFFER_SOURCE"),
        }

    def _find_offer_row(self, offer_id: int) -> tuple[dict[str, Any] | None, int]:
        """Ищет строку сделки по ID. Возвращает (строка | None, сколько строк просмотрено).

        Панель может **проигнорировать** `FILTER[OFFER_ID]` и вернуть последние сделки —
        тогда раньше бот отвечал «сделка не найдена», хотя она есть. Поэтому после фильтра
        проходим страницы и сравниваем ID сами.

        Результат живёт в кэше 20 секунд: в одном сценарии (ТО, карточка, смена статуса)
        сделку читают по несколько раз, а между записями кэш сбрасывается.
        """
        return self._cached(f"offer_row:{int(offer_id)}",
                            lambda: self._search_offer_row(int(offer_id)), ttl=20.0)

    def prewarm_offer(self, offer_id: int) -> bool:
        """Заранее читает строку сделки ОДНИМ запросом — пока модель «думает».

        Отличие от `_find_offer_row`: перебора страниц нет. Если панель не отдала
        сделку по фильтру, ничего не запоминаем — обычное чтение потом пройдёт
        как всегда и честно скажет, что не нашло. Так предзагрузка не может
        «закэшировать» ложное «сделки нет» и не тратит десятки запросов на
        опечатку в номере.
        """
        key = f"offer_row:{int(offer_id)}"
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < 20.0:
                return True
            epoch = self._epoch
        try:
            rows = self._page("offer_card", "offers/get_from_filter",
                              filters={"OFFER_ID": int(offer_id)}, limit=5)
        except StocrmError as exc:
            log.debug("предзагрузка сделки %s: %s", offer_id, exc)
            return False
        for row in rows:
            if isinstance(row, dict) and _same_id(row.get("OFFER_ID"), str(offer_id)):
                with self._cache_lock:
                    if epoch == self._epoch:      # пока читали, в панель могли записать
                        self._cache[key] = (time.monotonic(), copy.deepcopy((row, len(rows))))
                        if len(self._cache) > 200:
                            for old in sorted(self._cache, key=lambda k: self._cache[k][0])[:50]:
                                self._cache.pop(old, None)
                return True
        return False

    def _search_offer_row(self, offer_id: int) -> tuple[dict[str, Any] | None, int]:
        wanted = str(offer_id).strip()
        rows = self._page("offer_card", "offers/get_from_filter",
                          filters={"OFFER_ID": int(offer_id)}, limit=5)
        seen = len(rows)
        for row in rows:
            if isinstance(row, dict) and _same_id(row.get("OFFER_ID"), wanted):
                return row, seen
        if rows:
            log.info("offer_card: фильтр по OFFER_ID не сработал (вернулось %s строк), смотрю страницы",
                     len(rows))
        page_size = min(self.page_size, 200)
        for page in range(1, self.max_pages + 1):
            try:
                chunk = self._page("offer_card", "offers/get_from_filter", page=page, limit=page_size)
            except StocrmError:
                break
            seen += len(chunk)
            for row in chunk:
                if isinstance(row, dict) and _same_id(row.get("OFFER_ID"), wanted):
                    return row, seen
            if len(chunk) < page_size:
                break
        return None, seen

    def offer_identity_diagnostic(self, offer_id: int) -> dict[str, Any]:
        """Только свежая проверка полей сделки, без вывода содержимого CRM."""
        offer_id = int(offer_id)
        if offer_id <= 0:
            raise ValueError('Нужен положительный номер сделки')
        try:
            row, _ = self._search_offer_row(offer_id)
        except Exception:  # noqa: BLE001 — сырой текст может содержать секреты
            return {'status': 'error'}
        if not row:
            return {'status': 'not_found'}
        def number(value: Any) -> int | None:
            if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(value)):
                return None
            return int(value)
        return {'status': 'ok',
                'offer_id_matches': number(row.get('OFFER_ID')) == offer_id,
                'contact_id_present': number(row.get('CONTACT_ID')) is not None,
                'board_id': number(row.get('BOARD_ID')),
                'status_id': number(row.get('STATUS_ID')),
                'status_name_present': isinstance(row.get('STATUS_NAME'), str) and bool(row.get('STATUS_NAME', '').strip())}

    def balance_diagnostic(self, offer_id: int) -> dict[str, Any]:
        """Только структура ответа и флаги проверки; суммы и данные клиента не возвращаем."""
        offer_id = int(offer_id)
        if offer_id <= 0:
            raise ValueError('Нужен положительный номер сделки')
        try:
            balance = self._read('balance_diagnostic', 'payment/get_balance',
                                 {'ENTITY_ID': offer_id, 'ENTITY_TYPE': 'OFFER'})
        except Exception:  # noqa: BLE001 — сырые ошибки могут содержать секреты
            return {'status': 'error'}
        kind = ('null' if balance is None else 'boolean' if isinstance(balance, bool) else
                'object' if isinstance(balance, dict) else 'array' if isinstance(balance, list) else 'other')
        result: dict[str, Any] = {'status': 'ok', 'response_type': kind}
        if not isinstance(balance, dict):
            return result
        rub = [v for k, v in balance.items() if isinstance(k, str) and k.startswith('RUB_')]
        result['rub_count'] = len(rub)
        result['known_wrappers'] = [key for key in ('DATA', 'ITEMS', 'BALANCE', 'BALANCES', 'RESPONSE') if key in balance]
        result['malformed_rows'] = sum(not isinstance(v, dict) for v in rub)
        result['invalid_amounts'] = sum(not isinstance(v, dict) or _known_work_sum(v.get('CURRENCY_VALUE')) is None for v in rub)
        result['missing_entity'] = sum(isinstance(v, dict) and (v.get('ENTITY_ID') in (None, False, '') or v.get('ENTITY_TYPE') in (None, False, '')) for v in rub)
        result['wrong_entity'] = sum(isinstance(v, dict) and ((v.get('ENTITY_ID') not in (None, False, '') and not _same_id(v.get('ENTITY_ID'), offer_id)) or (v.get('ENTITY_TYPE') not in (None, False, '', 'OFFER'))) for v in rub)
        return result

    def offer_card(self, offer_id: int, *, with_items: bool = False) -> dict[str, Any]:
        """Карточка сделки.

        with_items=True добавляет к ней то, что человек обычно и хочет увидеть в
        «подробностях»: товары (позиции), услуги (работы) и оплаты по валютам —
        из них считается долг. По умолчанию не читаем: карточка нужна в десятке
        мест, и лишние запросы к панели там ни к чему.
        """
        # Карточку в одном сценарии спрашивают по 2–3 раза подряд; 15 секунд кэша
        # экономят поход в панель, а любая запись кэш сразу сбрасывает.
        key = f"card:{int(offer_id)}" + (":full" if with_items else "")
        if not with_items:
            return self._cached(key, lambda: self._offer_card(int(offer_id)), ttl=15.0)
        # CRM может измениться вне бота; подробности всегда перечитываем. Параллельные
        # одинаковые запросы всё равно объединяются в один (single-flight).
        return self._cached(key, lambda: self._offer_card(int(offer_id), with_items=True), ttl=0.0)

    def _offer_card(self, offer_id: int, with_items: bool = False) -> dict[str, Any]:
        offer_id = int(offer_id)

        # Баланс и карточка независимы. Как только найдена сделка, начинаем
        # чтение товаров/работ, не дожидаясь баланса: без дополнительных запросов.
        balance: Any = None
        balance_error: str | None = None
        items: dict[str, Any] = {"parts": [], "works": []}
        errors: list[str] = []

        def read_parts() -> None:
            try:
                items["parts"], problem = self._offer_parts_details(offer_id)
                if problem:
                    items["parts_error"] = problem
                    errors.append(f"товары: {problem}")
            except Exception as exc:  # noqa: BLE001 — ошибка не означает отсутствие товаров
                items["parts_error"] = str(exc) or type(exc).__name__
                errors.append(f"товары: {items['parts_error']}")

        def read_works() -> None:
            try:
                items["works"] = self.offer_works(offer_id, limit=200)
            except Exception as exc:  # noqa: BLE001 — ошибка не означает отсутствие услуг
                items["works_error"] = str(exc) or type(exc).__name__
                errors.append(f"услуги: {items['works_error']}")

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stocrm-balance") as pool:
            future = pool.submit(copy_context().run, self._read, "offer_balance", "payment/get_balance",
                                 {"ENTITY_ID": offer_id, "ENTITY_TYPE": "OFFER"})
            # Полная карточка должна отражать и изменения, сделанные вне бота.
            row, seen = self._search_offer_row(offer_id) if with_items else self._find_offer_row(offer_id)
            if with_items and row is not None and _same_id(row.get("OFFER_ID"), offer_id):
                self._run_parallel([read_parts, read_works])
            try:
                balance = future.result()
            except Exception as exc:  # noqa: BLE001 — баланс не критичен
                balance_error = str(exc) or type(exc).__name__

        if row is None:
            return {
                "error": (f"Сделка {offer_id} не найдена: просмотрел {seen} сделк(и) в панели "
                          "и не встретил такой номер. Проверьте номер или права ключа на сделку"),
            }
        # Сверяем ID ещё раз: бот не должен рассказать про чужую сделку.
        returned = row.get("OFFER_ID")
        if returned is not None and not _same_id(returned, offer_id):
            log.warning("offer_card: запросили %s, API вернул %s", offer_id, returned)
            return {"error": f"Сделка {offer_id} не найдена (API вернул другую сделку)"}
        offer = self._slim_offer(row)
        if balance_error is not None:
            offer["balances_error"] = balance_error
        elif isinstance(balance, dict):
            offer["balances"] = {
                k: _money(v.get("CURRENCY_VALUE")) for k, v in balance.items() if isinstance(v, dict)
            }
        # Баланс нельзя подменять нулём при ошибке API или повреждённой сумме.
        total = _known_work_sum(row.get("OFFER_SUM"))
        rub = {k: v for k, v in (balance or {}).items() if str(k).startswith("RUB_")} if isinstance(balance, dict) else {}
        amounts = [_known_work_sum(v.get("CURRENCY_VALUE")) if isinstance(v, dict) else None for v in rub.values()]
        # Без номера и типа источника нельзя доказать, что баланс относится к этой
        # сделке. В документированном ответе оба поля присутствуют (WIKI-A-612).
        balance_known = bool(rub) and all(v is not None for v in amounts) and all(
            isinstance(v, dict) and v.get("ENTITY_ID") not in (None, False, "")
            and _same_id(v.get("ENTITY_ID"), offer_id) and v.get("ENTITY_TYPE") == "OFFER"
            for v in rub.values()
        )
        if total is not None and balance_known:
            paid = round(sum(v for v in amounts if v is not None), 2)
            offer["paid"] = paid
            offer["debt"] = round(max(0.0, total - paid), 2)
            offer["overpaid"] = round(max(0.0, paid - total), 2)
        else:
            offer["paid"] = offer["debt"] = offer["overpaid"] = None
        if not with_items:
            return offer

        offer["parts"] = items["parts"]
        offer["works"] = items["works"]
        if items.get("parts_error"):
            offer["parts_error"] = items["parts_error"]
        if items.get("works_error"):
            offer["works_error"] = items["works_error"]
        if errors:
            offer["items_error"] = "; ".join(errors)
        offer["payments"] = _payments_total({k: _money(v.get("CURRENCY_VALUE")) for k, v in rub.items()}) if balance_known else {}
        return offer

    # ------------------------------------------------------------------- работы
    def offer_works(self, offer_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Работы (услуги) внутри сделки с их исполнителями."""
        raw = self._read(
            "offer_works", "work/get_filtered",
            {"FILTER": {"OFFER_ID": int(offer_id)}, "SORT": {"WORK_ID": "ASC"}, "PAGE": 1, "LIMIT": limit},
        )
        if raw is None or isinstance(raw, bool):
            raise ValueError("CRM не подтвердила список работ")
        rows = _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw)
        out = []
        for r in rows[:limit]:
            if not isinstance(r, dict) or r.get("OFFER_ID") in (None, False, ""):
                raise ValueError("CRM вернула работу без номера сделки; принадлежность не подтверждена")
            row_offer = r.get("OFFER_ID")
            if not _same_id(row_offer, offer_id):
                continue
            performers = r.get("PERFORMERS")
            names: list[dict[str, Any]] = []
            if isinstance(performers, dict):
                performers = list(performers.values())
            if isinstance(performers, list):
                for perf in performers:
                    if isinstance(perf, dict):
                        names.append({
                            "user_id": perf.get("USER_ID") or perf.get("ID"),
                            "name": " ".join(filter(None, [
                                perf.get("LAST_NAME"), perf.get("FIRST_NAME"), perf.get("NAME")
                            ])) or perf.get("PERFORMER"),
                            "sum": perf.get("PERFORMER_SUM"),
                            "share_percent": perf.get("PERFORMER_VOLUME_PERCENT") or perf.get("SHARE"),
                        })
            out.append({
                "work_id": r.get("WORK_ID"),
                "title": r.get("TITLE") or r.get("DOCUMENT_TITLE"),
                "type": r.get("TYPE"),
                "sum": _known_work_sum(r.get("SUM")),
                "hours": r.get("WORKING_HOUR"),
                "performers": names,
                "performers_count": r.get("PERFORMERS_COUNT") or len(names),
                "performer": r.get("PERFORMER"),
                "locked": r.get("LOCKED"),
            })
        return out

    # ------------------------------------------------------------------ запчасти
    def offer_parts(self, offer_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Первые строки товаров сделки. Для подробной карточки см. ограниченный обход."""
        raw = self._read("offer_parts", "products/get_filtered_orders",
            {"FILTER": {"OFFER_ID": int(offer_id)}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": 1, "LIMIT": limit})
        rows = _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw)
        return self._normalize_offer_parts(offer_id, rows[:limit])

    def _offer_parts_details(self, offer_id: int) -> tuple[list[dict[str, Any]], str | None]:
        """Ограниченный обход страниц; неполный список не выдаётся за полный."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_size, page_limit, result_limit = 200, 5, 200
        for page in range(1, page_limit + 1):
            try:
                raw = self._read("offer_parts", "products/get_filtered_orders",
                    {"FILTER": {"OFFER_ID": int(offer_id)}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": page_size})
                if raw is None or isinstance(raw, bool):
                    raise ValueError("CRM не подтвердила список товаров")
                rows = _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw)
                if any(not isinstance(r, dict) for r in rows):
                    raise ValueError("CRM вернула неизвестный формат строк товара")
                if any(r.get("OFFER_ID") in (None, False, "") for r in rows):
                    raise ValueError("CRM вернула товары без номера сделки; принадлежность не подтверждена")
                fresh = []
                for row in rows:
                    order_id = row.get("ORDER_ID")
                    fingerprint = str(order_id) if order_id not in (None, False, "") else json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
                    if fingerprint in seen:
                        return items, "CRM повторила строки или страницу; список товаров может быть неполным"
                    seen.add(fingerprint)
                    fresh.append(row)
                items.extend(self._normalize_offer_parts(offer_id, fresh))
            except (StocrmError, ValueError, TypeError) as exc:
                return items, f"Не удалось полностью прочитать товары: {exc}"
            if len(items) > result_limit:
                return items[:result_limit], f"Показаны первые {result_limit} товаров; список ограничен"
            if len(rows) < page_size:
                return items, None
            if len(items) == result_limit:
                return items, f"Показаны первые {result_limit} товаров; возможно, есть ещё"
        return items, f"Проверены первые {page_limit} страниц; список товаров может быть неполным"

    @staticmethod
    def _normalize_offer_parts(offer_id: int, rows: list[Any]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            if not isinstance(r, dict):
                raise ValueError("CRM вернула строку товара неизвестного формата")
            row_offer = r.get("OFFER_ID")
            if row_offer not in (None, False, "") and int(row_offer) != int(offer_id):
                continue
            stock: dict[str, Any] = r["FORMATTED_STOCK"] if isinstance(r.get("FORMATTED_STOCK"), dict) else {}
            out.append({
                "order_id": r.get("ORDER_ID"),
                "offer_id": r.get("OFFER_ID"),
                "require_id": r.get("REQUIRE_ID"),
                "uid": r.get("UID") or stock.get("UID"),
                "brand": r.get("BRAND") or stock.get("BRAND"),
                "sku": r.get("SKU") or stock.get("SKU"),
                "name": r.get("NAME") or stock.get("NAME"),
                "qty": r.get("QUANTITY_CURRENT") if r.get("QUANTITY_CURRENT") is not None else r.get("QUANTITY"),
                "qty_source": "QUANTITY_CURRENT" if r.get("QUANTITY_CURRENT") is not None else "QUANTITY",
                "price": _money(r.get("PRICE")),
                "order_sum": _money(r.get("ORDER_SUM")),
                "supplier_price": _money(r.get("SUPPLIER_PRICE")),
                "supplier": r.get("SUPPLIER_NAME"),
                "supplier_id": r.get("SUPPLIER_ID"),
                "source": r.get("ORDER_TYPE"),
                "markup": r.get("MARKUP"),
                "profit": _money(r.get("ORDER_PROFIT")),
                "warehouse": r.get("WAREHOUSE_NAME"),
                "active": r.get("ACTIVE"),
            })
        return out

    def _existing_part(self, offer_id: int, *, name: str, sku: str = "",
                       brand: str = "") -> dict[str, Any] | None:
        """Ищет такую же позицию в сделке (тот же артикул или то же название)."""
        sku_key = parts.sku_key(sku) if sku else ""
        name_key = parts.normalize_text(name)
        brand_key = parts.normalize_text(brand)
        for row in self.offer_parts(int(offer_id), limit=200):
            row_sku = parts.sku_key(str(row.get("sku") or ""))
            if sku_key and row_sku and row_sku == sku_key:
                return row
            row_name = parts.normalize_text(str(row.get("name") or ""))
            if not name_key or not row_name:
                continue
            # Сравниваем названия целиком: «фильтр» не должен совпадать
            # с «фильтр масляный W914/2», иначе бот предупреждал бы о дубле всегда.
            if name_key == row_name:
                row_brand = parts.normalize_text(str(row.get("brand") or ""))
                if not brand_key or not row_brand or brand_key == row_brand:
                    return row
        return None

    def find_offer_parts(self, offer_id: int, query: str = "") -> list[dict[str, Any]]:
        """Уже добавленные в сделку позиции, подходящие под слова человека.

        Нужно, чтобы бот не добавлял второй раз то, что в сделке уже есть
        (именно так появлялся «дубль масла»), и чтобы уметь найти позицию,
        у которой человек просит поменять цену.
        """
        rows = self.offer_parts(offer_id, limit=200)
        words = [w for w in parts.normalize_text(query).split() if len(w) > 2]
        if not words:
            return rows
        found: list[dict[str, Any]] = []
        for row in rows:
            haystack = parts.normalize_text(
                " ".join(str(row.get(k) or "") for k in ("name", "brand", "sku")))
            if all(word in haystack for word in words):
                found.append(row)
                continue
            sku_key = parts.sku_key(str(row.get("sku") or ""))
            if sku_key and sku_key in parts.sku_key(query):
                found.append(row)
        return found

    def offer_requires(self, offer_id: int) -> list[dict[str, Any]]:
        """Искомые запчасти сделки (то, что ещё не подобрано/не заказано)."""
        raw = self._read("offer_requires", "products/get_requires", {"OFFER_ID": int(offer_id)})
        rows = _as_items(raw)
        return [{
            "require_id": r.get("REQUIRE_ID"),
            "brand": r.get("BRAND"),
            "sku": r.get("SKU"),
            "title": r.get("TITLE"),
            "locked": r.get("LOCK"),
            "is_recommendation": r.get("IS_RECOMMENDATION"),
            "linked_works": [
                link.get("WORK_ID") for link in (r.get("WORK_REQUIRE_LINK") or []) if isinstance(link, dict)
            ],
        } for r in rows]

    #: Как по названию узнать «поставщика-заглушку» в панели. У STOCRM он обычно
    #: называется «Виртуальный» — именно его панель сама подставляет свободным позициям.
    VIRTUAL_SUPPLIER_RE = re.compile(r"виртуальн|virtual|без\s+поставщик|не\s+указан", re.IGNORECASE)

    def free_order_warehouse(self, explicit: int | None = None) -> int:
        """Технический контекст FREE_ORDER, НЕ поиск/списание товарных остатков.

        WIKI-A-617 перечисляет WAREHOUSE_ID в STOCK даже для FREE_ORDER.
        WIKI-A-678: POST, а при явном Wrong URI — документированный GET.
        Это только чтение; ошибки прав/авторизации не обходятся.
        """
        def collect() -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            seen: set[int] = set()
            use_get = False
            with read_budget(8.0):
                for page in range(1, self.max_pages + 1):
                    payload = {"PAGE": page, "LIMIT": self.page_size}
                    if not use_get:
                        try:
                            chunk = self._page("warehouses", "wms/get_warehouses", page=page, limit=self.page_size)
                        except StocrmError as exc:
                            if not re.search(r"wrong uri|no api method available", str(exc), re.I):
                                raise
                            use_get = True
                            chunk = _as_items(self._read_uncached("warehouses", "wms/warehouses/get_filtered", payload, get=True))
                    else:
                        chunk = _as_items(self._read_uncached("warehouses", "wms/warehouses/get_filtered", payload, get=True))
                    if not chunk:
                        return result
                    fresh = False
                    for row in chunk:
                        if not isinstance(row, dict):
                            continue
                        ident = row.get("WAREHOUSE_ID")
                        if isinstance(ident, bool) or not str(ident).isdigit() or int(str(ident)) <= 0:
                            continue
                        if int(str(ident)) in seen:
                            continue
                        seen.add(int(str(ident)))
                        fresh = True
                        if str(row.get("ARCHIVE", "N")).upper() in {"Y", "TRUE", "1"}:
                            continue
                        if str(row.get("HAS_USE_ACCESS_RIGHT", "Y")).upper() in {"N", "FALSE", "0"}:
                            continue
                        result.append(row)
                    if not fresh:
                        raise ValueError("CRM повторяет страницы справочника складов; запись не начата")
                    if len(chunk) < self.page_size:
                        return result
            raise ValueError("Справочник складов проверен не полностью; запись не начата")
        rows = self._cached("free_order_warehouses", collect, ttl=120)
        if explicit is not None:
            if isinstance(explicit, bool) or not any(str(r["WAREHOUSE_ID"]) == str(explicit) for r in rows):
                raise ValueError("Указанный технический склад недоступен или архивный; запись не начата")
            return int(explicit)
        defaults = [r for r in rows if str(r.get("DEFAULT", "N")).upper() in {"Y", "TRUE", "1"}]
        selected = defaults if defaults else rows
        if len(selected) != 1:
            raise ValueError("Не определён однозначный технический склад для свободной позиции. Выберите склад по умолчанию в настройках STOCRM или явно укажите технический склад из справочника. Остатки не списываются; запись не начата")
        return int(selected[0]["WAREHOUSE_ID"])

    def default_supplier(self) -> dict[str, Any]:
        """Поставщик, которого подставляем, если человек никого не назвал.

        Зачем: `offer/order/add` (единственный документированный метод с количеством,
        AMOUNT) панель принимает ТОЛЬКО с полем SUPPLIER_ID. Без него ответ был
        «Поля SUPPLIER_ID нет в исходных данных!», и всё откатывалось на старый
        `add_free_order`. Поведение его полей количества на реальной CRM требует проверки.

        Порядок: настройка STOCRM_DEFAULT_SUPPLIER_ID → «Виртуальный» из списка
        поставщиков панели → единственный поставщик, если он один.
        """
        if self.default_supplier_id is not None:
            if isinstance(self.default_supplier_id, bool) or not str(self.default_supplier_id).isdigit() or int(self.default_supplier_id) <= 0:
                raise ValueError("STOCRM_DEFAULT_SUPPLIER_ID должен быть положительным ID поставщика; запись не начата")
            return {"supplier_id": int(self.default_supplier_id), "name": "из настроек"}
        rows = self.suppliers()
        virtual = [row for row in rows if self.VIRTUAL_SUPPLIER_RE.search(str(row.get("name") or ""))]
        if len(virtual) == 1:
            return virtual[0]
        if not virtual and len(rows) == 1:
            return rows[0]
        choices = "; ".join(f"{r.get('name') or 'без названия'} (ID {r.get('supplier_id')})" for r in rows[:15])
        raise SupplierNeeds("Не выбран поставщик для свободной позиции: "
                         + ("в справочнике несколько виртуальных поставщиков. " if virtual else "однозначный виртуальный поставщик не найден. ")
                         + ("Доступны: " + choices + ". " if choices else "Справочник поставщиков пуст. ")
                         + "Укажите поставщика явно или настройте STOCRM_DEFAULT_SUPPLIER_ID; первый из списка автоматически не назначается. Запись не начата", rows)

    def suppliers(self) -> list[dict[str, Any]]:
        """Подключённые поставщики (id + название) — для выбора при заказе запчасти."""
        raw = self._read("suppliers", "wms/get_supplier_ids", {})
        out = []
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    out.append({
                        "supplier_id": value.get("SUPPLIER_ID") or value.get("ID") or key,
                        "name": value.get("NAME") or value.get("SUPPLIER_NAME") or str(value),
                    })
                else:
                    out.append({"supplier_id": key, "name": str(value)})
        elif isinstance(raw, list):
            out = [{"supplier_id": r.get("SUPPLIER_ID") or r.get("ID"), "name": r.get("NAME")} for r in raw]
        return out

    def draft_add_offer_part(
        self,
        *,
        offer_id: int,
        name: str,
        brand: str | None = None,
        sku: str | None = None,
        qty: int = 1,
        source: str = "manual",
        price: float | None = None,
        supplier_price: float | None = None,
        supplier_id: int | None = None,
        supplier_name: str | None = None,
        warehouse_id: int | None = None,
        stock_ids: list[int] | None = None,
        require_id: int | None = None,
        need_markup: bool = False,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК добавления запчасти в сделку.

        source:
          * "supplier" — заказ у подключённого поставщика (FREE_ORDER + SUPPLIER_ID);
          * "manual"   — основной add_free_order, без справочников склада/поставщика.

        Со склада запчасти в сделку НЕ подставляем (правило заказчика): остаток склада —
        только справочная информация для человека, списания/резерва не делаем.

        Порядок как в документации: сначала `offer/require/add` (создаёт «искомую
        запчасть»), затем `offer/order/add_free_order` для ручного товара.
        Заказы поставщику и старый метод offer/order/add заблокированы.
        """
        source = (source or "manual").lower()
        if source == "stock":
            raise ValueError(
                "Со склада запчасти в сделку не подставляем. Выберите source=\"manual\" "
                "(свободная позиция без заказа поставщику). "
                "Наличие на складе можно показать человеку, но списание не оформляем"
            )
        if stock_ids:
            raise ValueError("Складские позиции нельзя привязывать к сделке. Уберите stock_ids; запись не начата")
        if source == "supplier" or self.manual_order_method != "offer/order/add_free_order":
            raise ValueError("Заказы поставщикам через бота запрещены. Доступна только свободная позиция без поставщика; запись не начата.")
        if source not in {"supplier", "manual"}:
            raise ValueError("source должен быть supplier (заказ у поставщика) или manual (свободная позиция)")
        if not (name or "").strip():
            raise ValueError("Нужно название запчасти (name)")

        # Такая позиция уже есть в сделке? Тогда предупреждаем в самом черновике:
        # раньше «поменяй цену масла» превращалось в «добавь второе масло», и в сделке
        # оказывались две одинаковые позиции с разными ценами.
        duplicate_warning: str | None = None
        if not allow_duplicate:
            existing = self._existing_part(offer_id, name=name, sku=sku or "",
                                            brand=brand or "")
            if existing is not None:
                duplicate_warning = (
                    f"В сделке {offer_id} такая позиция уже есть: «{existing.get('name')}» "
                    f"{existing.get('brand') or ''} {existing.get('sku') or ''} — "
                    f"{_num(_money(existing.get('price')))} ₽ × {existing.get('qty')}. "
                    "Добавление создаст вторую такую же строку. Если нужно поменять цену — "
                    "это отдельное действие (изменение цены позиции), а не добавление"
                )
        if price is None:
            raise ValueError(
                "Не указана цена продажи (price). Возьмите её из найденной позиции — "
                "stock/offer_parts — или спросите человека"
            )
        if _money(price) <= 0:
            # Позиция с ценой 0 ₽ уезжала в сделку молча (так и получилось «масло за 0»).
            raise ValueError(
                "Цена продажи 0 — такую позицию не добавляю: в сделке она будет бесплатной. "
                "Уточните у человека цену продажи и передайте её в price"
            )
        if source == "supplier" and not supplier_id:
            raise ValueError(
                "Для source=supplier нужен supplier_id — сначала вызовите suppliers и выберите поставщика"
            )
        qty = max(1, int(qty or 1))
        if not parts.known_brand(brand):
            raise ValueError("Бренд товара не указан или неизвестен. Укажите бренд вручную; запись не начата")
        brand = (brand or "").strip()
        sku = (sku or "").strip()

        steps: list[dict[str, Any]] = []
        if require_id:
            require_ref: Any = int(require_id)
        else:
            steps.append({
                "method": "offer/require/add",
                "payload": {"OFFER_ID": int(offer_id), "TITLE": name, "BRAND": brand, "SKU": sku},
            })
            require_ref = {"$from_step": 0}

        # Единственный доступный вариант — свободная строка без полей поставщика.
        # Количество/цену после применения проверяем чтением, повторов записи нет.
        if supplier_id is not None or supplier_price is not None or warehouse_id is not None or stock_ids:
            raise ValueError("Для свободного ордера закупка, поставщик и склад не передаются. Заказы поставщикам через бота запрещены. Запись не начата")
        payload: dict[str, Any] = {
            "REQUIRE_ID": require_ref, "NAME": name, "BRAND": brand, "SKU": sku,
            "PRICE": float(price), "NEED_MARKUP": False,
            "AMOUNT": qty, "CURRENT_VOLUME": qty, "MIN_VOLUME": 1,
        }
        method = "offer/order/add_free_order"
        where = "свободный ордер (без подбора склада и заказа поставщику)"

        step: dict[str, Any] = {"method": method, "payload": payload}
        steps.append(step)

        check: dict[str, Any] = {
            "kind": "added_line",
            "offer_id": int(offer_id),
            "sku": sku,
            "name": name,
            "brand": brand,
            "price": float(price or 0),
            "qty": qty,
            "where": where,
        }
        summary = f"Добавить в сделку {offer_id}: {_part_line(check)}"
        if supplier_price:
            summary += f" · закупка {_num(supplier_price)} ₽"
        summary += f" · {where}"

        if duplicate_warning:
            summary += f" (внимание: {duplicate_warning})"

        # Блокировка переживает /drafts, перезапуск и изменение бренда в новой просьбе.
        self.drafts.assert_part_unblocked(check)
        extra: dict[str, Any] = {"check": check}
        messages = [duplicate_warning] if duplicate_warning else []
        if messages:
            extra["warning"] = "; ".join(messages)
        return self.drafts.create(
            tool="add_offer_part", method=steps[0]["method"],
            payload=steps[0]["payload"], summary=summary, steps=steps, extra=extra,
        )

    @staticmethod
    def _draft_offer_id(draft: dict[str, Any]) -> int | None:
        """Номер сделки из служебных данных черновика (check/checks/payload)."""
        candidates: list[Any] = []
        check = draft.get("check")
        if isinstance(check, dict):
            candidates.append(check.get("offer_id"))
            candidates += [item.get("offer_id") for item in (check.get("items") or [])
                           if isinstance(item, dict)]
        for item in draft.get("checks") or []:
            if isinstance(item, dict):
                candidates.append(item.get("offer_id"))
        payload = draft.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("OFFER_ID"))
            data = payload.get("DATA")
            if isinstance(data, dict):
                candidates.append(data.get("OFFER_ID"))
        for value in candidates:
            try:
                if value:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _checks_of(draft: dict[str, Any]) -> list[dict[str, Any]]:
        """Проверки позиций черновика — и из одиночного, и из уже объединённого."""
        check = draft.get("check") or {}
        if check.get("kind") == "added_lines":
            return [dict(item) for item in check.get("items") or [] if isinstance(item, dict)]
        return [dict(check)] if check else []

    @staticmethod
    def _title_of(draft: dict[str, Any]) -> str:
        """Одна строка описания позиции из резюме черновика."""
        summary = str(draft.get("summary") or "").strip()
        head, _, rest = summary.partition(": ")
        if not rest:
            return summary
        first = rest.split("\n")[0] if "\n" in rest else rest
        first = first.split(" (внимание")[0].strip()
        if first.startswith("позиций"):
            # уже объединённое резюме — берём строки позиций целиком
            lines = [line.strip() for line in rest.split("\n")[1:] if line.strip()]
            return "; ".join(line.split(". ", 1)[-1] for line in lines)
        return first

    @staticmethod
    def _parts_header(offer_id: int | None, items: list[dict[str, Any]]) -> str:
        """Шапка списка позиций: сколько их и на какую сумму."""
        deal = f" в сделку {offer_id}" if offer_id else ""
        total = sum(_money(item.get("price") or 0) * max(1, int(item.get("qty") or 1))
                    for item in items)
        head = f"Добавить{deal} — позиций {len(items)}"
        if total:
            head += f", итого {_thousands(total)} ₽"
        return head + ":"

    def _parts_summary(self, offer_id: int | None, items: list[dict[str, Any]]) -> str:
        """Один список без вложенной нумерации: строка = позиция."""
        lines = [f"{number}. {_part_line(item)}" for number, item in enumerate(items, 1)]
        return self._parts_header(offer_id, items) + "\n" + "\n".join(lines)

    @classmethod
    def _duplicate_part_check(cls, draft: dict[str, Any]) -> str | None:
        seen: set[tuple[str, str, str]] = set()
        for item in cls._checks_of(draft):
            sku = parts.sku_exact(str(item.get("sku") or ""))
            if not sku:
                continue
            key = (str(item.get("offer_id") or ""), parts.canonical_brand(str(item.get("brand") or "")), sku)
            if key in seen:
                return f"В черновике повторяется {key[1]} {sku}. Запись остановлена: одинаковые строки могут иметь разные цены. Отмените конфликтный черновик в /drafts и подготовьте корректный состав заново; CRM не изменена."
            seen.add(key)
        return None

    def merge_part_drafts(self, drafts: list[dict[str, Any]]) -> dict[str, Any]:
        """Складывает несколько черновиков запчастей в ОДИН — с одной кнопкой.

        Зачем: «добавь артикулы Pc2031, Mb664285, 2996255SX…» давало по карточке на
        каждую позицию, и человек нажимал «Применить» пять раз. Теперь позиции идут
        одним черновиком: шаги дописываются в конец, ссылки на предыдущий шаг
        сдвигаются, а после записи проверяется КАЖДАЯ позиция.
        """
        live = list({d["token"]: d for d in drafts if isinstance(d, dict) and d.get("token")}.values())
        if len(live) < 2:
            return live[0] if live else {}
        base = live[0]
        for draft in live:
            problem = self._duplicate_part_check(draft)
            if problem:
                raise ValueError(problem)
        accepted = [base]
        seen_items = {(str(i.get("offer_id") or ""), parts.canonical_brand(str(i.get("brand") or "")), parts.sku_exact(str(i.get("sku") or ""))): i for i in self._checks_of(base) if i.get("sku")}
        for other in live[1:]:
            items = self._checks_of(other)
            duplicates = [i for i in items if (str(i.get("offer_id") or ""), parts.canonical_brand(str(i.get("brand") or "")), parts.sku_exact(str(i.get("sku") or ""))) in seen_items]
            if duplicates:
                same = len(duplicates) == len(items) and all(
                    _money(i.get("price")) == _money(seen_items[(str(i.get("offer_id") or ""), parts.canonical_brand(str(i.get("brand") or "")), parts.sku_exact(str(i.get("sku") or "")))].get("price"))
                    and int(i.get("qty") or 1) == int(seen_items[(str(i.get("offer_id") or ""), parts.canonical_brand(str(i.get("brand") or "")), parts.sku_exact(str(i.get("sku") or "")))].get("qty") or 1)
                    for i in duplicates)
                self.drafts.mark(other["token"], "discarded", reason="повтор/конфликт уже подготовленной позиции", merged_into=base["token"] if same else None)
                if same:
                    continue
                message = "Повторный артикул с другой ценой или количеством: автоматическое объединение остановлено. Отмените конфликтный черновик в /drafts и подготовьте корректный состав заново. CRM не изменена."
                self.drafts.mark(base["token"], "pending", part_conflict=message)
                raise ValueError(message)
            accepted.append(other)
            for item in items:
                if item.get("sku"):
                    seen_items[(str(item.get("offer_id") or ""), parts.canonical_brand(str(item.get("brand") or "")), parts.sku_exact(str(item.get("sku") or "")))] = item
        live = accepted
        offset = len(base.get("steps") or [])
        offer_id = self._draft_offer_id(base)
        checks: list[dict[str, Any]] = self._checks_of(base)
        titles: list[str] = []
        head_title = self._title_of(base) or _part_line(checks[0]) if checks else ""
        if head_title:
            titles.append(head_title)
        #: Какие шаги относятся к какой позиции — нужно, чтобы можно было убрать
        #: ОДНУ позицию из общего черновика, не задев остальные.
        positions: list[dict[str, Any]] = [dict(item) for item in base.get("positions") or []]
        if not positions:
            positions = [{"title": head_title,
                          "sku": (checks[0].get("sku") if checks else ""),
                          "steps": [0, max(0, offset - 1)]}]
        warnings: list[str] = [str(base["warning"])] if base.get("warning") else []
        for other in live[1:]:
            extra_steps = list(other.get("steps") or [])
            if not extra_steps:
                continue
            try:
                updated = self.drafts.extend(base["token"], steps=extra_steps, shift=offset)
            except (ValueError, KeyError) as exc:
                log.warning("не смог объединить черновик %s: %s", other.get("token"), exc)
                continue
            other_checks = self._checks_of(other)
            checks.extend(other_checks)
            title = (self._title_of(other)
                     or (_part_line(other_checks[0]) if other_checks else ""))
            if title:
                titles.append(title)
            if other.get("positions"):
                for position in other["positions"]:
                    start, end = position["steps"]
                    positions.append({**position, "steps": [offset + start, offset + end]})
            else:
                positions.append({
                    "title": title,
                    "sku": (other_checks[0].get("sku") if other_checks else ""),
                    "steps": [offset, offset + len(extra_steps) - 1],
                })
            offset += len(extra_steps)
            base = updated
            if other.get("warning"):
                warnings.append(str(other["warning"]))
            self.drafts.mark(other["token"], "discarded",
                             reason=f"объединено с {base['token']}",
                             merged_into=base["token"])
        if checks:
            merged_summary = self._parts_summary(offer_id, checks)
        else:                             # старые черновики без проверок — по заголовкам
            deal = f" в сделку {offer_id}" if offer_id else ""
            merged_summary = f"Добавить{deal} — позиций {len(titles)}:\n" + "\n".join(
                f"{number}. {title}" for number, title in enumerate(titles, 1)
            )
        extra: dict[str, Any] = {"check": {"kind": "added_lines", "items": checks},
                                 "positions": positions}
        if warnings:
            extra["warning"] = "; ".join(dict.fromkeys(warnings))
        try:
            return self.drafts.extend(base["token"], steps=[], summary=merged_summary, extra=extra)
        except (ValueError, KeyError):
            return base

    def drop_draft_position(
        self,
        token: str,
        *,
        number: int | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Убирает ОДНУ позицию из подготовленного черновика (остальные остаются).

        Зачем: несколько артикулов идут одним черновиком с одной кнопкой «Применить»,
        и человеку нужно уметь выкинуть одну строку («убери второй артикул»), не
        отменяя всю просьбу и не собирая её заново.
        """
        try:
            draft = self.drafts.get(token)
        except KeyError as exc:
            raise ValueError(
                "Этот черновик уже не найти — он истёк или был применён. "
                "Повторите просьбу, и я подготовлю заново"
            ) from exc
        if draft.get("tool") == "quick_deal":
            from .quick_deal import drop_position
            return drop_position(self, token, number=number, query=query)
        if draft.get("status") != "pending":
            raise ValueError(
                "Черновик уже " + ("применён" if draft.get("status") == "applied" else "закрыт") +
                " — убирать из него позиции поздно. Проверьте сделку в STOCRM"
            )
        if (draft.get('check') or {}).get('kind') not in {'added_lines', 'added_line', 'added_work'}:
            raise ValueError("Этот вид черновика нельзя редактировать по позициям")
        items = self._checks_of(draft)
        if not items:
            raise ValueError("В этом черновике нет позиций — отменять нечего")
        positions = list(draft.get("positions") or [])
        index = self._position_index(items, positions, number=number, query=query)
        if index is None:
            listing = "; ".join(
                f"{i}. {item.get('name') or item.get('sku') or 'позиция'}"
                for i, item in enumerate(items, 1)
            )
            raise ValueError(
                f"Не понял, какую позицию убрать. В черновике: {listing}. "
                "Скажите номер — например «убери 2»"
            )
        if len(items) == 1:
            with self.drafts._locked():
                current = self.drafts._verify_unlocked(token)
                current['status'] = 'discarded'
                current['reason'] = 'последняя позиция удалена человеком'
                self.drafts._save()
            return {"status": "discarded", "token": token, "left": 0}

        span = None
        if index < len(positions):
            span = positions[index].get("steps")
        if not isinstance(span, list) or len(span) != 2:
            raise ValueError(
                "Не могу разделить этот черновик на позиции — отмените его целиком "
                "и повторите просьбу без лишней позиции"
            )
        indices = list(range(int(span[0]), int(span[1]) + 1))
        left_items = [item for i, item in enumerate(items) if i != index]
        left_titles = [str((positions[i].get("title") if i < len(positions) else "")
                           or items[i].get("name") or items[i].get("sku") or "позиция")
                       for i in range(len(items)) if i != index]
        remaining_steps = [step for i, step in enumerate(draft.get("steps") or [])
                           if i not in set(indices)]
        # пересчитываем, какие шаги остались у каждой позиции
        new_positions: list[dict[str, Any]] = []
        offset = 0
        for i, position in enumerate(positions):
            if i == index:
                continue
            span_i = position.get("steps") or [0, 0]
            size = max(1, int(span_i[1]) - int(span_i[0]) + 1)
            new_positions.append({**position, "steps": [offset, offset + size - 1]})
            offset += size
        if offset != len(remaining_steps):        # метаданные разошлись — не рискуем
            new_positions = []
        offer_id = self._draft_offer_id(draft)
        summary = self._parts_summary(offer_id, left_items)
        if not any(item.get("sku") or item.get("name") for item in left_items):
            deal = f" в сделку {offer_id}" if offer_id else ""
            summary = (f"Добавить{deal} — позиций {len(left_items)}:\n"
                       + "\n".join(f"{n}. {title}" for n, title in enumerate(left_titles, 1)))
        extra: dict[str, Any] = {"check": {"kind": "added_lines", "items": left_items}}
        if new_positions:
            extra["positions"] = new_positions
        updated = self.drafts.remove_steps(token, indices, summary=summary, extra=extra)
        removed = self._short_position_title(
            (positions[index].get("title") if index < len(positions) else "")
            or items[index].get("name") or items[index].get("sku") or "позиция")
        return {"status": "pending", "token": updated["token"], "left": len(left_items),
                "summary": updated.get("summary"), "items": left_items,
                "removed": removed, "draft": updated}

    @staticmethod
    def _short_position_title(text: str, limit: int = 70) -> str:
        """Короткое имя позиции для ответа: без цены, поставщика и склада."""
        clean = str(text or "").split(" — ")[0].split(" × ")[0].strip()
        clean = clean or str(text or "").strip()
        return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"

    @staticmethod
    def _position_index(items: list[dict[str, Any]], positions: list[dict[str, Any]],
                        *, number: int | None, query: str | None) -> int | None:
        """Какую позицию человек имеет в виду: по номеру, артикулу или названию."""
        if number is not None:
            if 1 <= int(number) <= len(items):
                return int(number) - 1
            return None
        needle = parts.normalize_text(query or "")
        sku_needle = parts.sku_exact(query or "")
        if not needle and not sku_needle:
            return None
        for index, item in enumerate(items):
            sku = parts.sku_exact(str(item.get("sku") or ""))
            name = parts.normalize_text(str(item.get("name") or ""))
            title = parts.normalize_text(str((positions[index].get("title")
                                              if index < len(positions) else "") or ""))
            if sku_needle and sku and (sku_needle == sku or sku_needle in sku):
                return index
            if needle and (needle in name or needle in title):
                return index
        return None

    def draft_change_part_price(
        self,
        *,
        offer_id: int,
        price: float | None = None,
        query: str | None = None,
        order_id: int | None = None,
        qty: int | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК изменения цены (и при желании количества) уже добавленной позиции.

        Зачем: человек говорит «поменяй цену масла на 5200», а раньше бот добавлял
        ВТОРУЮ такую же позицию — в сделке появлялись два масла с разными ценами.

        Метод правки строки сделки не подтверждён публичной документацией.
        В штатном режиме запрос отклоняется без записи. Совместимость с явно
        настроенной отдельно проверенной интеграцией сохранена; её результат
        всегда проверяется повторным чтением.
        """
        if qty is not None and int(qty) <= 0:
            raise ValueError(
                "Количество 0 — это не правка: панель не принимает количество меньше 1, "
                "а позиция остаётся в сделке. Чтобы убрать позицию, используйте "
                "draft_remove_offer_part (удаление строки)"
            )
        if price is None and not qty:
            raise ValueError(
                "Нечего менять: передайте новую цену продажи (price) и/или количество (qty)")

        if not self.price_methods:
            raise ValueError("Стандартный публичный API не подтвердил метод изменения цены уже добавленного товара. Новую строку вместо правки не создаю. Что сделать: откройте сделку → «Товары» → нужная строка → цена продажи. В CRM ничего не изменено.")

        line: dict[str, Any] | None = None
        if order_id:
            line = next(
                (r for r in self.offer_parts(int(offer_id), limit=200)
                 if _same_id(r.get("order_id"), order_id)),
                None,
            )
            if line is None:
                raise ValueError(f"В сделке {offer_id} нет позиции с order_id={order_id}")
        else:
            matches = self.find_offer_parts(int(offer_id), query or "")
            if not matches:
                names = ", ".join(
                    f"«{r.get('name')}» {_money(r.get('price')):.0f} ₽"
                    for r in self.offer_parts(int(offer_id), limit=10)
                )
                raise ValueError(
                    f"В сделке {offer_id} не нашёл позицию «{query or ''}». "
                    f"Сейчас в сделке: {names or 'позиций нет'}. Уточните название или артикул"
                )
            if len(matches) > 1:
                listing = "; ".join(
                    f"{r.get('name')} {r.get('brand') or ''} {r.get('sku') or ''} "
                    f"— {_money(r.get('price')):.0f} ₽ (order_id={r.get('order_id')})"
                    for r in matches[:6]
                )
                raise ValueError(
                    f"Под «{query}» подходят несколько позиций в сделке: {listing}. "
                    "Уточните, у какой меняем цену (артикул или order_id)"
                )
            line = matches[0]

        old_price = _money(line.get("price"))
        old_qty = int(line.get("qty") or 1)
        # Меняем только количество — цену оставляем прежней (и всё равно проверяем её
        # после записи: панель могла применить наценку).
        price = _money(price) if price is not None else old_price
        if price <= 0:
            raise ValueError("Нужна новая цена продажи больше нуля (price)")
        new_qty = max(1, int(qty)) if qty else old_qty
        payload: dict[str, Any] = {
            "ORDER_ID": int(line["order_id"]),
            # цена, которую назвал человек — это ЦЕНА ПРОДАЖИ, наценку не добавляем
            "PRICE": price,
            "CURRENT_VOLUME": new_qty,
        }
        if new_qty != old_qty:
            payload["QUANTITY"] = new_qty

        summary = (
            f"Изменить позицию в сделке {offer_id}: «{line.get('name')}» "
            f"{line.get('brand') or ''} {line.get('sku') or ''} — "
            f"цена продажи {_num(old_price)} ₽ → {_num(price)} ₽"
        )
        if new_qty != old_qty:
            summary += f", количество {old_qty} → {new_qty}"
        if price == old_price and new_qty == old_qty:
            raise ValueError(
                f"В сделке {offer_id} у позиции «{line.get('name')}» уже цена "
                f"{old_price:.2f} ₽ × {old_qty} — менять нечего")

        # Имени метода правки в документации STOCRM нет, а `offer/order/update` в боевой
        # панели отвечает «Wrong URI. No api method available». Поэтому пробуем
        # кандидатов по очереди (первый, кто не ответил «нет такого метода», и выигрывает).
        fallbacks: list[dict[str, Any]] = []
        for alternative in self.price_methods[1:]:
            if self._is_missing_method(alternative):
                continue          # этот уже проверяли — панель его не знает
            fallbacks.append({"method": alternative, "payload": dict(payload)})

        step: dict[str, Any] = {"method": self.price_methods[0], "payload": payload}
        if fallbacks:
            step["fallbacks"] = fallbacks

        return self.drafts.create(
            tool="change_part_price",
            method=step["method"],
            payload=payload,
            summary=summary,
            steps=[step],
            extra={
                # после записи перечитываем сделку и убеждаемся, что цена правда изменилась
                "check": {
                    "kind": "order_price",
                    "offer_id": int(offer_id),
                    "order_id": int(line["order_id"]),
                    "price": price,
                    "qty": new_qty,
                    "old_price": old_price,
                },
            },
        )

    # ------------------------------------------------------------------ оплата
    def offer_money(self, offer_id: int) -> dict[str, Any]:
        """Сумма сделки, сколько уже оплачено и сколько осталось — без чтения позиций."""
        card = self.offer_card(int(offer_id))
        if card.get("error"):
            raise ValueError(str(card["error"]))
        totals = _payments_total(card.get("balances") or {})
        total = _money(card.get("sum"))
        paid = totals["total"]
        return {
            "offer_id": int(offer_id),
            "sum": round(total, 2),
            "paid": round(paid, 2),
            "debt": round(max(0.0, total - paid), 2),
            "overpaid": round(max(0.0, paid - total), 2),
            "by_kind": totals["by_kind"],
            "payment_status": card.get("payment_status"),
        }

    def _is_missing_method(self, method: str) -> bool:
        """Панель уже говорила, что такого метода нет? (кэш в capabilities.json)."""
        return method in (self._price_capability().get("missing") or [])

    def _price_capability(self) -> dict[str, Any]:
        data = self._capabilities()
        return data.get("offer_price", {})

    def _remember_price_method(self, method: str, *, ok: bool) -> None:
        cap = self._price_capability()
        missing = [m for m in (cap.get("missing") or []) if m != method]
        if not ok:
            missing.append(method)
        self._save_capability("offer_price", {
            "method": method,
            "verified": bool(ok),
            "missing": missing,
            "checked_at": time.time(),
        })

    def _verify_price_change(self, check: dict[str, Any]) -> str | None:
        """Что показала панель после попытки изменить цену. None — всё как просили."""
        offer_id = int(check.get("offer_id") or 0)
        order_id = check.get("order_id")
        wanted = _money(check.get("price"))
        self._invalidate_cache()
        try:
            rows = self.offer_parts(offer_id, limit=200)
        except StocrmError as exc:
            return f"не удалось перечитать сделку для проверки: {exc}"
        row = next((r for r in rows if _same_id(r.get("order_id"), order_id)), None)
        if row is None:
            return (
                "позиция после записи в сделке не нашлась — проверьте сделку "
                f"{offer_id} в STOCRM"
            )
        actual = _money(row.get("price"))
        problems = []
        if abs(actual - wanted) > 0.01:
            problems.append(f"цена в сделке {actual:.2f} ₽ вместо {wanted:.2f} ₽")
        wanted_qty = int(check.get("qty") or 0)
        actual_qty = _known_work_sum(row.get("qty"))
        if wanted_qty and actual_qty != wanted_qty:
            if row.get("qty_source") == "QUANTITY_CURRENT" and actual_qty is not None:
                problems.append(f"количество в строке {_num(actual_qty)} вместо {wanted_qty}")
            else:
                problems.append("не удалось надёжно проверить количество в строке сделки")
        if not problems:
            return None
        return (
            "Не удалось подтвердить изменение уже добавленной позиции: "
            + ", ".join(problems) + ".\n"
            "Что сделать: откройте сделку в STOCRM → блок «Реализация» → «Товары» → "
            "в строке позиции поставьте нужную цену продажи и количество "
            "(публичный метод правки позиции не подтверждён)."
        )

    def draft_remove_offer_part(
        self,
        *,
        offer_id: int,
        query: str | None = None,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК удаления позиции из сделки.

        Раньше на «убери её» бот показывал «удаление позиции (количество изменено на 0)» —
        то есть просто ставил количество 0, чего панель не принимает (минимальное
        количество 1), а позиция оставалась. Теперь пробуем настоящие методы удаления
        (`offer/order/delete`, `offer/order/remove`), а после записи ПЕРЕЧИТЫВАЕМ сделку:
        если строка на месте — честно говорим, что удалить через API нельзя, и даём
        точный путь в панели.
        """
        line = self._line_for_change(int(offer_id), query=query, order_id=order_id)
        payload: dict[str, Any] = {
            "ORDER_ID": int(line["order_id"]),
            "OFFER_ID": int(offer_id),
        }
        # ОДИН шаг с запасными вариантами: сначала настоящие методы удаления, а если
        # панель их не знает — пометка строки неактивной (`offer/order/update` + ACTIVE=N):
        # в карточке такая позиция не участвует в сумме сделки. Раньше «не активна» была
        # отдельным шагом и не выполнялась вовсе, потому что первый шаг уже падал.
        archive_payload = dict(payload) | {DELETE_ARCHIVE_FIELD: DELETE_ARCHIVE_VALUE}
        fallbacks: list[dict[str, Any]] = [
            {"method": m, "payload": dict(payload)} for m in DELETE_METHODS[1:]
        ]
        fallbacks += [{"method": m, "payload": dict(archive_payload)} for m in (self.price_methods or DEFAULT_ARCHIVE_METHODS)]
        steps: list[dict[str, Any]] = [{"method": DELETE_METHODS[0], "payload": payload,
                                        "fallbacks": fallbacks}]
        summary = (
            f"Удалить из сделки {offer_id} позицию «{line.get('name')}» "
            f"{line.get('brand') or ''} {line.get('sku') or ''} — "
            f"{_num(_money(line.get('price')))} ₽ × {int(line.get('qty') or 1)}"
        )
        return self.drafts.create(
            tool="remove_offer_part",
            method=steps[0]["method"],
            payload=payload,
            summary=summary,
            steps=steps,
            extra={
                "check": {
                    "kind": "removed_line",
                    "offer_id": int(offer_id),
                    "order_id": int(line["order_id"]),
                    "name": line.get("name"),
                    "sku": line.get("sku"),
                },
            },
        )

    def _line_for_change(self, offer_id: int, *, query: str | None,
                         order_id: int | None) -> dict[str, Any]:
        """Находит строку сделки по order_id или по словам человека (общая логика)."""
        if order_id:
            line = next(
                (r for r in self.offer_parts(offer_id, limit=200)
                 if _same_id(r.get("order_id"), order_id)),
                None,
            )
            if line is None:
                raise ValueError(f"В сделке {offer_id} нет позиции с order_id={order_id}")
            return line
        matches = self.find_offer_parts(offer_id, query or "")
        if not matches:
            names = ", ".join(
                f"«{r.get('name')}» {_money(r.get('price')):.0f} ₽"
                for r in self.offer_parts(offer_id, limit=10)
            )
            raise ValueError(
                f"В сделке {offer_id} не нашёл позицию «{query or ''}». "
                f"Сейчас в сделке: {names or 'позиций нет'}. Уточните название или артикул"
            )
        if len(matches) > 1:
            listing = "; ".join(
                f"{r.get('name')} {r.get('brand') or ''} {r.get('sku') or ''} "
                f"— {_money(r.get('price')):.0f} ₽ (order_id={r.get('order_id')})"
                for r in matches[:6]
            )
            raise ValueError(
                f"Под «{query}» подходят несколько позиций в сделке: {listing}. "
                "Уточните, какую именно (артикул или order_id)"
            )
        return matches[0]

    #: Значения поля ACTIVE, которые панель считает «позиция убрана».
    _INACTIVE = {None, "", "n", "нет", "no", "false", "0", False}

    @classmethod
    def _line_is_gone(cls, row: dict[str, Any] | None) -> bool:
        """Позиция «убранa»: её нет, или панель пометила её неактивной."""
        if row is None:
            return True
        active = row.get("active")
        if isinstance(active, str):
            return active.strip().lower() in cls._INACTIVE
        return active in cls._INACTIVE

    def _verify_removed_line(self, check: dict[str, Any]) -> str | None:
        """Строка после «удаления» правда исчезла? None — да."""
        self._invalidate_cache()
        try:
            rows = self.offer_parts(int(check.get("offer_id") or 0), limit=200)
        except StocrmError:
            return "Не удалось проверить результат в STOCRM. Что сделать: откройте сделку и проверьте запись; не повторяйте действие до проверки."                      # перечитать не смогли — не пугаем зря
        still = next((r for r in rows if _same_id(r.get("order_id"), check.get("order_id"))), None)
        if self._line_is_gone(still):
            return None
        return DELETE_MANUAL_HINT

    def _find_work(self, check: dict[str, Any]) -> dict[str, Any] | None:
        """Работа из черновика — в сделке (по WORK_ID, иначе по названию). None — не видно."""
        offer_id = int(check.get("offer_id") or 0)
        if not offer_id:
            return None
        self._invalidate_cache()
        try:
            works = self.offer_works(offer_id, limit=200)
        except StocrmError:
            return None
        work_id = check.get("work_id")
        target = None
        if work_id and not isinstance(work_id, dict):
            target = next((w for w in works if _same_id(w.get("work_id"), work_id)), None)
        if target is None and not work_id:
            title = parts.normalize_text(str(check.get("title") or ""))
            if title:
                matches = [w for w in works if parts.normalize_text(str(w.get("title") or "")) == title]
                target = matches[0] if len(matches) == 1 else None
        return target

    def _verify_work_exists(self, check: dict[str, Any]) -> str | None:
        if self._find_work(check) is not None:
            return None
        return "Не удалось подтвердить появление услуги в STOCRM. Откройте сделку → «Работы» и проверьте результат; повторно не добавляйте до проверки."

    def _verify_work_sum(self, check: dict[str, Any]) -> str | None:
        """Одно чтение проверяет и наличие работы, и её сумму."""
        check.pop("actual_sum", None)
        check.pop("rounding_note", None)
        target = self._find_work(check)
        if target is None:
            return "Не удалось однозначно проверить услугу в STOCRM. Что сделать: откройте сделку → «Работы» и проверьте наличие и сумму; не добавляйте повторно до проверки."
        wanted = check.get("sum")
        if wanted is None:
            return None
        if target.get("sum") is None:
            return "STOCRM не вернул сумму услуги. Проверьте сумму в сделке → «Работы»."
        actual = _money(target.get("sum"))
        difference = abs(actual - _money(wanted))
        if difference < 0.01:
            check["actual_sum"] = actual
            return None
        # Небольшая корректировка суммы панелью — не ошибка масштаба «тысяч».
        if actual > 0 and self.price_rounding_step > 0 and difference < self.price_rounding_step and abs(actual / 5 - round(actual / 5)) < 0.001:
            check["actual_sum"] = actual
            check["rounding_note"] = f"Учтено округление панели: запрошено {_thousands(wanted)} ₽, фактически {_thousands(actual)} ₽."
            return None
        return (
            f"В сделке у услуги «{check.get('title')}» сумма {_thousands(actual)} ₽, "
            f"а просили {_thousands(wanted)} ₽ — панель записала по-своему.\n"
            "Что сделать: откройте сделку → блок «Работы» → в строке услуги поправьте сумму."
        )

    def _capabilities(self) -> dict[str, Any]:
        """Что бот уже выяснил про недокументированные методы (.state/capabilities.json)."""
        if not self.capabilities_path or not self.capabilities_path.exists():
            return {}
        try:
            data = json.loads(self.capabilities_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_capability(self, key: str, info: dict[str, Any]) -> None:
        if not self.capabilities_path:
            return
        data = self._capabilities()
        data[key] = info
        # Запоминание «метод работает» — не повод падать: запись в CRM уже прошла.
        try:
            self.capabilities_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.capabilities_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.capabilities_path)
        except OSError as exc:
            log.warning("не смог сохранить capabilities (%s): %s", self.capabilities_path, exc)

    def draft_add_offer_work(
        self,
        *,
        offer_id: int,
        title: str,
        hours: float = 1,
        price_of_hour: float | None = None,
        sum_value: float | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК: добавить работу в сделку.

        Работа добавляется в свободной форме (без ID из каталога): название, нормочасы,
        цена нормочаса или сумма.

        Исполнителя бот НЕ назначает: в Public API STOCRM метода «назначить исполнителя
        работе» нет, и перебор угаданных названий методов ничего не назначал — только
        тратил время и создавал ложное «готово». Исполнитель ставится в панели, в блоке
        «Работы».
        """
        data: dict[str, Any] = {
            "OFFER_ID": int(offer_id),
            "TYPE": self.free_form_type,
            "DESCRIPTION": title,
            "TITLE": title,
            "WORKING_HOUR": float(hours or 1),
        }
        if price_of_hour is not None:
            data["PRICE_OF_HOUR"] = float(price_of_hour)
        if sum_value is not None:
            data["SUM"] = float(sum_value)
            # Панель может пересчитать SUM по своему тарифу. Передаём согласованную
            # цену нормочаса из названной суммы, не тариф по умолчанию.
            if price_of_hour is None:
                data["PRICE_OF_HOUR"] = float(sum_value) / float(hours or 1)
        if price_of_hour is None and sum_value is None:
            raise ValueError("Назовите стоимость услуги или цену нормочаса — нулевую сумму вместо неизвестной не подставляю")

        summary = f"Добавить работу «{title}» ({hours} н/ч) в сделку {offer_id}"
        if price_of_hour is not None:
            summary += f" по {price_of_hour} ₽/час"
        elif sum_value is not None:
            summary += f" на сумму {_num(sum_value)} ₽"

        steps: list[dict[str, Any]] = [{"method": "work/add", "payload": {"DATA": data}}]
        check: dict[str, Any] = {"kind": "added_work", "offer_id": int(offer_id), "title": title,
                                 "sum": float(sum_value) if sum_value is not None else (
                                     float(price_of_hour) * float(hours or 1)
                                     if price_of_hour is not None else None)}
        return self.drafts.create(
            tool="add_offer_work", method="work/add", payload={"DATA": data}, summary=summary,
            steps=steps, extra={"check": check},
        )

    def offer_history(self, offer_id: int, limit: int = 50) -> list[dict[str, Any]]:
        raw = self._read("offer_history", "offer/status/get_filtered_change_history", {})
        rows = [r for r in _as_items(raw) if int(r.get("OFFER_ID") or 0) == int(offer_id)]
        return [{
            "from": r.get("FROM_STATUS_NAME"),
            "to": r.get("TO_STATUS_NAME"),
            "user_id": r.get("USER_ID"),
            "date": r.get("DATE_CREATE"),
        } for r in rows[:limit]]

    def find_contacts(self, *, query: str | None = None, phone: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Локальный поиск; незавершённый обход не выдаётся за отсутствие клиента."""
        with read_budget(8.0):
            return self._find_contacts_bounded(query=query, phone=phone, limit=limit)

    def _find_contacts_bounded(self, *, query: str | None, phone: str | None, limit: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 8.0
        details = 0
        if not phone and query and re.fullmatch(r"[+\d ()-]+", query.strip()):
            phone, query = query, None
        needle = name_key(query)
        wanted = phone_key(phone)
        if phone and len(wanted) < 4:
            raise ValueError("Укажите телефон целиком или хотя бы последние четыре цифры")
        limit = max(1, min(int(limit), 100))
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        detail_error: Exception | None = None
        incomplete = "Поиск контактов не завершён: CRM повторяет страницы или достигнут предел обхода. Нельзя утверждать, что клиента нет. Укажите номер сделки для точной проверки."
        for page in range(1, self.max_pages + 1):
            self.client._check_running()
            if time.monotonic() >= deadline:
                if out:
                    for match in out:
                        match["search_incomplete"] = True
                    return out
                raise ValueError(incomplete)
            raw = self._read("find_contacts", "contacts/get_from_filter", {
                "FILTER": {}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": self.page_size})
            if raw is None or isinstance(raw, bool):
                raise ValueError("CRM не подтвердила список контактов — поиск не завершён")
            rows = _as_items(raw)
            if not rows:
                if detail_error and not out:
                    raise ValueError("Не удалось проверить дополнительные телефоны контактов — поиск не завершён") from detail_error
                if detail_error:
                    for match in out:
                        match["search_incomplete"] = True
                return out
            fresh = 0
            for row in rows:
                self.client._check_running()
                if not isinstance(row, dict) or not str(row.get("CONTACT_ID") or "").isdigit():
                    raise ValueError("CRM вернула контакт без номера — поиск не завершён")
                ident = str(row["CONTACT_ID"])
                if ident in seen:
                    continue
                seen.add(ident)
                fresh += 1
                names = " ".join(str(row.get(k) or "") for k in ("TITLE", "SURNAME", "FIRSTNAME", "MIDDLENAME"))
                if needle and needle not in name_key(names):
                    continue
                phones = unique_phones([row.get("MAIN_PHONE")])
                if wanted and not any(phone_matches(wanted, number) for number in phones):
                    if details >= 20:
                        detail_error = ValueError("Достигнут лимит дополнительных карточек: поиск не завершён")
                        continue
                    details += 1
                    try:
                        detail = self._cached(f"contact-detail:{ident}", partial(self.contact_card, int(ident)), ttl=120.0)
                        phones = unique_phones(phones + detail_phones(detail, int(ident)))
                    except (StocrmError, ValueError) as exc:
                        detail_error = exc
                        continue
                    if not any(phone_matches(wanted, number) for number in phones):
                        continue
                contact = self._slim_contact(row)
                if wanted:
                    contact["matched_phone"] = next(number for number in phones if phone_matches(wanted, number))
                out.append(contact)
                if len(out) >= limit:
                    for match in out:
                        match["search_incomplete"] = True
                    return out
            if not fresh:
                if out:
                    for match in out:
                        match["search_incomplete"] = True
                    return out
                raise ValueError(incomplete)
        if out:
            for match in out:
                match["search_incomplete"] = True
            return out
        raise ValueError(incomplete)

    def offer_contact_phones(self, offer_id: int) -> dict[str, Any]:
        """Точный контакт сделки: без баланса, обхода всех контактов и изменений."""
        row, _ = self._find_offer_row(int(offer_id))
        if row is None or not _same_id(row.get("OFFER_ID"), offer_id):
            raise ValueError(f"Сделка №{offer_id} не найдена или недоступна для ключа")
        contact_id = row.get("CONTACT_ID")
        if not str(contact_id or "").isdigit() or int(str(contact_id)) <= 0:
            raise ValueError(f"В сделке №{offer_id} CRM не указала контакт — телефон не подтверждён")
        raw = self.contact_card(int(str(contact_id)))
        phones = detail_phones(raw, int(str(contact_id)))
        return {"offer_id": int(offer_id), "contact_id": int(str(contact_id)),
                "contact": row.get("CONTACT_TITLE"), "phones": phones}

    @staticmethod
    def _slim_contact(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "contact_id": r.get("CONTACT_ID"),
            "title": title_of(r),
            "phone": r.get("MAIN_PHONE"),
            "created": r.get("DATE_CREATE"),
            "rating": r.get("RATING"),
            "active": r.get("ACTIVE"),
            "archive": r.get("ARCHIVE"),
        }

    # ------------------------------------------------------- подбор запчастей
    def resolve_part(
        self,
        *,
        query: str | None = None,
        brand: str | None = None,
        sku: str | None = None,
        offer_id: int | None = None,
        limit: int = 8,
        allow_web: bool = True,
    ) -> dict[str, Any]:
        """Ищет запчасть по названию/бренду/артикулу и ранжирует варианты.

        Возвращает:
            {"status": "matched",     "best": {...}, "candidates": [...]}
            {"status": "ambiguous",   "candidates": [...]}   — нужно спросить человека
            {"status": "not_found",   "candidates": []}
        Первым в candidates всегда самый вероятный вариант.
        """
        query = (query or "").strip() or None
        brand = (brand or "").strip() or None
        raw_sku = (sku or "").strip() or None
        # один и тот же подбор в одном разговоре не повторяем (человек уточняет фразу,
        # но ищет ту же деталь) — это уже готовый ответ, без запросов к панели
        cache_key = f"resolve|{query}|{brand}|{raw_sku}|{offer_id}|{limit}|{allow_web}"
        with self._cache_lock:
            hit = self._cache.get(cache_key)
            if hit and (hit[1].get("status") == "not_found" or (hit[1].get("rossko") or {}).get("status") == "unavailable" or (hit[1].get("profit_liga") or {}).get("status") in {"unavailable", "partial"}) and time.monotonic() - hit[0] >= 5:
                self._cache.pop(cache_key, None)
        def bounded() -> dict[str, Any]:
            with read_budget(8.0):
                return self._resolve_part(query=query, brand=brand, raw_sku=raw_sku, offer_id=offer_id, limit=limit, allow_web=allow_web)
        return self._cached(cache_key, bounded)

    def _resolve_part(
        self,
        *,
        query: str | None = None,
        brand: str | None = None,
        raw_sku: str | None = None,
        offer_id: int | None = None,
        limit: int = 8,
        allow_web: bool = True,
    ) -> dict[str, Any]:

        # артикул могли назвать внутри фразы: «фильтр w 914/2».
        # Берём НАПИСАНИЕ (со слешем/дефисом), а не нормализованное: FILTER[SKU] в панели
        # сравнивает артикул как он записан, иначе «W9142» ≠ «W914/2».
        if not raw_sku and query:
            found = parts.raw_sku_in_text(query)
            raw_sku = found[0] if found else None
        # «фильтр воздушный 2019 Camry»: год — не артикул
        if raw_sku and parts.is_probably_year(raw_sku) and len((query or "").split()) > 1:
            raw_sku = None
        if not brand and query:
            brand = parts.find_brand_in_text(query) or parts.latin_word_before_sku(query)

        collected: list[dict[str, Any]] = []
        errors: list[str] = []

        def add(item: dict[str, Any], source: str) -> None:
            if raw_sku and not parts.known_brand(item.get("brand")):
                return  # пустой бренд не прекращает перебор вариантов и источников
            if raw_sku and parts.sku_exact(item.get("sku")) != parts.sku_exact(raw_sku):
                return
            if raw_sku and brand and not parts.exact_brand_match(brand, item.get("brand")):
                return
            collected.append({**item, "source": item.get("source") or source})

        def search_cards(term: str | None) -> None:
            try:
                for card in self.product_cards(query=term, limit=max(limit * 4, 20)):
                    add(card, "номенклатура")
            except (StocrmError, ValueError) as exc:
                errors.append(f"номенклатура: {exc}")

        def search_history() -> None:
            before = len(collected)
            try:
                for row in self.part_history(sku=raw_sku, brand=brand, term=term,
                                             limit=max(limit * 2, 16)):
                    add(row, "история")
            except (StocrmError, ValueError) as exc:
                errors.append(f"история: {exc}")
                tried.append("история заказов запчастей: источник не проверен из-за отказа API")
                return
            tried.append(f"история заказов запчастей: {len(collected) - before}")

        # из фразы убираем артикул и бренд — они идут отдельными фильтрами
        term = parts.search_term(query, brand=brand, sku=raw_sku) or None

        tried: list[str] = []

        # 1) по артикулу: «как написали», затем все написания (W 914/2 ↔ W9142 ↔ W-914-2)
        if raw_sku:
            def search_cards_by_sku() -> None:
                for variant in parts.sku_variants(raw_sku):
                    search_cards(variant)
                    if any(item.get("source") == "номенклатура" for item in collected):
                        break
                tried.append(f"номенклатура по «{raw_sku}»: {sum(item.get('source') == 'номенклатура' for item in collected)}")

            search_cards_by_sku()
            if not collected:
                search_history()

        # 2) по названию и по бренду — независимые запросы, идём ими параллельно:
        #    раньше они шли строго по очереди, и каждый ждал ответа панели.
        if not raw_sku and not collected and (term or brand):
            tasks: list[Callable[[], None]] = []
            if term:
                tasks.append(lambda: search_cards(term))
            if brand:
                tasks.append(lambda: search_cards(brand))
            # историю заказов не запускаем «на всякий случай»: это лишние страницы
            # в панели. Она подключается шагом ниже, только если ничего не нашлось.
            self._run_parallel(tasks)

        # Склад — только справочный источник: поиск не резервирует, не списывает и
        # не передаёт ID складской позиции в будущий черновик. Не смешиваем остаток
        # с количеством в сделке или закупочную цену с ценой продажи.
        stock_errors: list[str] = []
        stock_rows: list[dict[str, Any]] = []
        stock_checked = bool(raw_sku or term or brand)
        if stock_checked:
            if raw_sku:
                stock_rows = self.stock_by_sku_variants(raw_sku, brand=brand,
                                                        limit=max(limit * 4, 20), errors=stock_errors)
            else:
                try:
                    stock_rows = self.stock(query=term, brand=brand, limit=max(limit * 4, 20))
                except (StocrmError, ValueError) as exc:
                    stock_errors.append(str(exc))
            for row in stock_rows:
                add({"brand": row.get("brand"), "sku": row.get("sku"), "name": row.get("name"),
                     "stock_records": [{"warehouse_id": row.get("warehouse_id"), "qty": row.get("qty")}],
                     "source": "склад"}, "склад")
            tried.append(f"склад (только чтение): {len(stock_rows)} строк" + ("; поиск проверен не полностью" if stock_errors else ""))
            errors.extend(f"склад: {error}" for error in stock_errors)
        stock_lookup = {"status": ("partial" if stock_rows else "unavailable") if stock_errors else
                        ("ok" if stock_rows else "empty") if stock_checked else "skipped",
                        "rows": len(stock_rows), "errors": stock_errors}

        # 4) Последняя попытка перед интернетом: страницы номенклатуры.
        if not collected:
            fallback: list[Callable[[], None]] = []
            if raw_sku:
                # QUERY/NAME документированы как поиск по названию, не обязательно SKU.
                # Проверяем страницы, а не только первые limit брендовых позиций.
                def scan_cards() -> None:
                    seen: set[tuple[Any, ...]] = set()
                    try:
                        for page in range(1, self.text_pages + 1):
                            rows = self._page("product_cards", "wms/product_cards/get_filtered",
                                              page=page, limit=self.page_size)
                            if not rows:
                                return
                            fresh = False
                            for row in rows:
                                key = (row.get("CARD_ID"), row.get("SKU"), row.get("BRAND"), row.get("NAME"))
                                if key in seen:
                                    continue
                                seen.add(key)
                                fresh = True
                                add({"card_id": row.get("CARD_ID"), "sku": row.get("SKU"),
                                     "brand": row.get("BRAND"), "name": row.get("NAME") or row.get("TITLE"),
                                     "price_purchase": row.get("PRICE") or row.get("PRICE_SUPP"),
                                     "price_sale": row.get("PRICE_W_MARKUP") or row.get("PRICE_POSITION_ON_MARKUP"),
                                     "price": row.get("PRICE")}, "номенклатура")
                            if not fresh or collected:
                                return
                        errors.append("Поиск номенклатуры ограничен числом страниц; отсутствие артикула в базе не доказано")
                    except (StocrmError, ValueError) as exc:
                        errors.append(f"номенклатура: {exc}")
                fallback.append(scan_cards)
            elif term:
                fallback.extend([
                    lambda: search_cards(None),
                ])
            if fallback:
                self._run_parallel(fallback)
                tried.append(f"ограниченный поиск по страницам: собрано {len(collected)} позиций")

        # Независимый источник брендов: даже если CRM уже нашла один бренд,
        # проверяем Росско, чтобы не скрыть другие варианты точного артикула.
        rossko_result: dict[str, Any] = {"status": "skipped", "brands": []}
        if raw_sku and not parts.is_viscosity(raw_sku) and not parts.is_probably_year(raw_sku):
            rossko_result = rossko.search_brands(raw_sku)
            for row in rossko_result.get("brands") or []:
                add(row, "Росско")
            rstatus = rossko_result.get("status")
            if rstatus == "ok":
                tried.append(f"Росско (только поиск): {len(rossko_result['brands'])} брендов")
            elif rstatus == "empty":
                tried.append("Росско: точных брендов в полученном ответе нет; поиск ограничен")
            elif rstatus == "unavailable":
                tried.append("Росско: поиск недоступен; отсутствие бренда не доказано")
                errors.append("Росско: " + str(rossko_result.get("error") or "недоступно"))

        # Профит Лига — только подтверждённый метод поиска; заказов и цен нет.
        profit_result: dict[str, Any] = {"status": "skipped", "brands": []}
        if raw_sku and not parts.is_viscosity(raw_sku):
            profit_result = profit_liga.search_brands(raw_sku)
            for row in profit_result.get("brands") or []:
                add(row, "Профит Лига")
            pstatus = profit_result.get("status")
            if pstatus == "ok":
                tried.append(f"Профит Лига (только поиск): {len(profit_result['brands'])} брендов")
            elif pstatus == "empty":
                tried.append("Профит Лига: точных брендов в ответе нет; отсутствие детали не доказано")
            elif pstatus in {"unavailable", "partial"}:
                warning = "Профит Лига: поиск недоступен или неполон; отсутствие детали не доказано"
                detail = warning + ": " + str(profit_result.get("error") or "недоступно")
                tried.append(detail)
                errors.append(detail)

        # 5b) интернет: артикула нет в панели — узнаём бренды по реальным сайтам.
        #     Бренд не выдумываем: только упоминания в выдаче, со ссылкой.
        # Только осмысленный артикул: «5/30», «2020», «масло» — не повод перебирать
        # поисковики (именно на этом «добавь масло bardahl 5/30» висело минутами).
        online: dict[str, Any] | None = None
        article = raw_sku or ""
        sku_worth_searching = bool(article) and (
            (any(ch.isdigit() for ch in article) and any(ch.isalpha() for ch in article)
             and len(parts.normalize_sku(article)) >= 4)
            or (article.isdigit() and len(article) >= 4)
        ) and not parts.is_viscosity(article) and not parts.is_probably_year(article)
        if allow_web and sku_worth_searching and not any(
            parts.sku_exact(item.get("sku")) == parts.sku_exact(raw_sku) for item in collected
        ):
            before = len(collected)
            try:
                online = self.web_lookup_part(sku=article, term=term, brand=brand, limit=limit)
            except Exception as exc:
                errors.append(f"интернет: {exc}")
            else:
                for item in online.get("brands") or []:
                    add(item, "интернет")
                if online.get("status") == "unavailable":
                    errors.append(f"интернет: {online.get('error') or 'поиск недоступен'}")
                state = online.get("status")
                if state in ("unavailable", "pending"):
                    tried.append("интернет: поиск недоступен" if state == "unavailable" else "интернет: поиск ещё не завершён")
                else:
                    tried.append(f"интернет: подтверждённых вариантов {len(online.get('brands') or [])}; результатов выдачи {online.get('hits') or 0}")
                if len(collected) - before:
                    tried.append("интернет дал варианты — они помечены как не из панели")

        # 6) запчасти в самой сделке — «как в прошлый раз»
        if offer_id:
            try:
                for row in self.offer_parts(int(offer_id), limit=limit * 4):
                    add({
                        "brand": row.get("brand"), "sku": row.get("sku"), "name": row.get("name"),
                        "qty": row.get("qty"), "price": row.get("price"),
                    }, "в сделке")
            except (StocrmError, ValueError) as exc:
                errors.append(f"сделка: {exc}")

        # Для строгой проверки передаём исходное написание: normalize_sku
        # использует визуальные двойники и не является обратимым преобразованием.
        sku = raw_sku or None

        # убираем дубли по (артикул или название, бренд)
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in collected:
            key = (parts.sku_exact(item.get("sku")) or str(item.get("name") or "").lower(),
                   parts.exact_brand_identity(item.get("brand")))
            if not any(key):
                continue
            current = unique.get(key)
            if current is None:
                unique[key] = item
            elif item.get("source") == "склад":
                current["stock_records"] = (current.get("stock_records") or []) + (item.get("stock_records") or [])
            elif current.get("source") == "склад":
                unique[key] = {**item, "stock_records": current.get("stock_records") or []}

        # Для Профит Лиги список может содержать десятки брендов; клавиатура постраничная.
        # Сначала посчитать реально подходящие позиции, затем честно сообщить об обрезании.
        expanded = bool(profit_result.get("brands"))
        rank_limit = 100 if expanded else limit
        ranked = parts.rank_candidates(query, list(unique.values()), sku=sku, brand=brand,
                                       limit=max(rank_limit, len(unique)))
        if len(ranked) > rank_limit and expanded:
            warning = f"Профит Лига: список вариантов обрезан до {rank_limit} из {len(ranked)}; уточните бренд для полного поиска"
            tried.append(warning)
            errors.append(warning)
        ranked = ranked[:rank_limit]
        # при равном балле сначала то, что реально есть в CRM, потом интернет
        ranked.sort(key=lambda item: (item.get("source") == "интернет", -item.get("score", 0)))
        status = parts.decide(ranked)
        if ranked and (all(i.get("source") == "склад" for i in ranked) or
                       (not brand and all(i.get("source") in {"интернет", "Росско", "Профит Лига"} for i in ranked))):
            status = "ambiguous"  # найденный бренд — выбор человека, не доказательство уникальности
        result: dict[str, Any] = {
            "status": status,
            "query": query,
            "sku": raw_sku,
            "brand": brand,
            "internet": online,
            "stock_lookup": stock_lookup,
            "rossko": {"status": rossko_result.get("status"), "count": len(rossko_result.get("brands") or []),
                       **({"error": rossko_result["error"]} if rossko_result.get("error") else {})},
            "profit_liga": {"status": profit_result.get("status"), "count": len(profit_result.get("brands") or []),
                            "brands": [row["brand"] for row in (profit_result.get("brands") or [])[:100]],
                            **({"error": profit_result["error"]} if profit_result.get("error") else {})},
            "candidates": ranked,
            "best": ranked[0] if status == "matched" and ranked else None,
        }
        if tried:
            result["tried"] = tried
        if errors:
            result["warnings"] = errors
        return result

    def contact_card(self, contact_id: int) -> Any:
        return self._read("contact_card", "contact/get_info", {"CONTACT_ID": int(contact_id)})

    def stock(self, *, query: str | None = None, brand: str | None = None, sku: str | None = None, warehouse_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._cached(
            f"stock|{query}|{brand}|{sku}|{warehouse_id}|{limit}",
            lambda: self._stock(query=query, brand=brand, sku=sku,
                                warehouse_id=warehouse_id, limit=limit),
        )

    def _stock(self, *, query: str | None = None, brand: str | None = None, sku: str | None = None, warehouse_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if brand:
            filters["BRAND"] = brand
        if sku:
            filters["SKU"] = sku
        if warehouse_id:
            filters["WAREHOUSE_ID"] = int(warehouse_id)
        if query:
            # поиск по названию/артикулу — серверного фильтра по подстроке нет, поэтому
            # обходим страницы. Раньше обход шёл до self.max_pages (20 страниц × 500 строк)
            # на КАЖДЫЙ вариант запроса — из-за этого «добавь масло …» думало минутами.
            rows = self.client.filtered_all(
                "wms/get_filtered_stocks", filters=filters or None,
                page_size=self.page_size, max_pages=self.text_pages,
            )
        else:
            rows = self._page("stock", "wms/get_filtered_stocks", filters=filters or None, limit=limit)
        needle = (query or "").strip().lower()
        out = []
        for r in rows:
            if sku and parts.sku_exact(r.get("SKU")) != parts.sku_exact(sku):
                continue
            if brand and not parts.exact_brand_match(brand, r.get("BRAND")):
                continue
            name = str(r.get("NAME") or "")
            if needle and not parts.text_matches(query, f"{name} {r.get('SKU') or ''}"):
                continue
            out.append({
                "warehouse_id": r.get("WAREHOUSE_ID"),
                "brand": r.get("BRAND"),
                "sku": r.get("SKU"),
                "name": name,
                "qty": next((r[k] for k in ("QUANTITY", "COUNT", "STOCK_COUNT") if r.get(k) not in (None, "")), None),
                "reserved": r.get("RESERVED"),
                # В API склада PRICE — это ЗАКУПКА (она же PRICE_W_TAX), а цена продажи
                # панель считает наценкой: PRICE_W_MARKUP. Раньше бот брал «price» и
                # подставлял закупку как продажу — человек видел в сделке цену дешевле
                # той, которую назвал. Поэтому поля названы прямо.
                "price_purchase": r.get("PRICE") or r.get("PRICE_W_TAX") or r.get("PRICE_SUPP"),
                "price_sale": (r.get("PRICE_W_MARKUP") or r.get("PRICE_POSITION_ON_MARKUP")
                               or r.get("PRICE_POSITION_ON_MARKUP_AND_DISCOUNT")),
                "price": r.get("PRICE"),
            })
            if len(out) >= limit:
                break
        return out

    def product_cards(self, *, query: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        return self._cached(
            f"cards|{query}|{limit}", lambda: self._product_cards(query=query, limit=limit)
        )

    def _product_cards(self, *, query: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """Карточки номенклатуры.

        В документации STOCRM (WIKI-A-692) фильтр называется **QUERY** («поиск по названию
        товара») и **NAME**. Раньше отправлялся FILTER[SEARCH] — панель такого ключа не знает,
        поэтому на живых данных поиск молча не работал (в демо-режиме всё совпадало, потому что
        мок понимал оба ключа). Пробуем QUERY → NAME, и только потом отдаём пустой список.
        """
        rows: list[Any] = []
        if query:
            for field in ("QUERY", "NAME"):
                rows = self._page("product_cards", "wms/product_cards/get_filtered",
                                  limit=limit, extra={"FILTER": {field: query}})
                if rows:
                    break
        else:
            rows = self._page("product_cards", "wms/product_cards/get_filtered", limit=limit)
        return [{
            "card_id": r.get("CARD_ID"),
            "brand": r.get("BRAND"),
            "sku": r.get("SKU"),
            "name": r.get("NAME") or r.get("TITLE"),
            # см. stock(): PRICE — закупка, цена продажи считается наценкой панели
            "price_purchase": r.get("PRICE") or r.get("PRICE_SUPP"),
            "price_sale": r.get("PRICE_W_MARKUP") or r.get("PRICE_POSITION_ON_MARKUP"),
            "price": r.get("PRICE"),
        } for r in rows[:limit]]

    def stock_by_sku_variants(self, sku: str | list[str], *, brand: str | None = None,
                              limit: int = 40, errors: list[str] | None = None) -> list[dict[str, Any]]:
        """Ищет остатки, перебирая написания артикула (FILTER[SKU] сравнивает как в панели)."""
        variants = parts.sku_variants(sku) if isinstance(sku, str) else list(sku)
        # Одинаковые по смыслу написания второй раз не спрашиваем («w914/2» ↔ «W9142»).
        # Сравниваем «как написано» (без регистра), а не по нормализованному ключу:
        # панель ищет FILTER[SKU] буквально, и «W 914/2» с «W914/2» — разные запросы.
        unique: list[str] = []
        seen_variants: set[str] = set()
        for variant in variants:
            literal = (variant or "").strip().lower()
            if not literal or literal in seen_variants:
                continue
            seen_variants.add(literal)
            unique.append(variant)

        found: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, str]] = set()
        batch_rows: dict[str, list[dict[str, Any]]] = {}

        def fetch(variant: str) -> None:
            try:
                batch_rows[variant] = self.stock(sku=variant, brand=brand, limit=limit)
            except (StocrmError, ValueError) as exc:
                if errors is not None:
                    errors.append(str(exc))
                batch_rows[variant] = []

        def collect(variant: str) -> None:
            for row in batch_rows.get(variant) or []:
                if unique and parts.sku_exact(row.get("sku")) != parts.sku_exact(unique[0]):
                    continue
                if brand and not parts.exact_brand_match(brand, row.get("brand")):
                    continue
                row_key = (row.get("warehouse_id"),
                           parts.sku_exact(row.get("sku")) or row.get("name"),
                           parts.exact_brand_identity(row.get("brand")))
                if row_key in seen:
                    continue
                seen.add(row_key)
                found.append(row)

        # Первое написание спрашиваем отдельно: в панели артикул почти всегда записан
        # так, как его продиктовали, и лишние запросы не нужны.
        rest = unique
        if unique:
            fetch(unique[0])
            collect(unique[0])
            rest = unique[1:]

        # Остальные написания — параллельно, по три за раз: раньше они шли строго по
        # одному, и «W 914/2» → «W914/2» → «W-914-2» стоило трёх ожиданий панели.
        for start in range(0, len(rest), 3):
            if any(parts.known_brand(row.get("brand")) for row in found):
                break
            chunk = rest[start:start + 3]
            tasks: list[Callable[[], None]] = []
            for variant in chunk:
                def run(v: str = variant) -> None:
                    fetch(v)

                tasks.append(run)
            self._run_parallel(tasks)
            for variant in chunk:
                collect(variant)
        return found

    def web_lookup_part(self, *, sku: str, term: str | None = None, limit: int = 8,
                        brand: str | None = None) -> dict[str, Any]:
        """Ищет артикул в интернете и возвращает бренды, которые реально есть в выдаче.

        Возвращает:
            {"status": "ok"|"empty"|"unavailable", "brands": [{brand, why, url, ...}],
             "queries": [...], "error": "..."}
        Бренд не придумывается: только то, что написано на сайтах, вместе со ссылкой.
        """
        from . import websearch

        brand = parts.canonical_brand(brand) if brand else None
        cache_key = f"{parts.sku_exact(sku)}|{brand or ''}|{(term or '').lower()}|{limit}"
        if not hasattr(self, "_web_cache"):
            self._web_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        cached = self._web_cache.get(cache_key)
        if cached:
            # удачный поиск помним сутки, временный отказ — лишь 5 секунд
            ttl = 24 * 3600 if cached[1].get("brands") else 5
            if time.time() - cached[0] < ttl:
                return cached[1]

        result: dict[str, Any] = {"status": "empty", "brands": [], "queries": [], "hits": 0}
        if not websearch.enabled():
            result["status"] = "unavailable"
            result["error"] = "интернет-поиск выключен (WEBSEARCH_ENABLED=0)"
            return result
        if not websearch.available():
            # сеть недавно отказала: не заставляем человека ждать те же 10 секунд снова
            result["status"] = "unavailable"
            result["error"] = ("интернет сейчас недоступен — искать в сети не пробую ещё "
                               f"{websearch.seconds_until_retry() / 60:.0f} мин")
            self._web_cache[cache_key] = (time.time(), result)
            return result

        # Один запрос к выбранному провайдеру — без переключения поисковиков,
        # повторных запросов, обхода ограничений или капчи. Интернет — подсказка.
        hits: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            found = websearch.search_batch([sku], limit=8)
            result["queries"].append(sku)
            hits = [{"title": h.title, "snippet": h.snippet, "url": h.url, "site": h.site}
                    for h in found]
        except (websearch.WebSearchError, ValueError) as exc:
            errors.append(str(exc))

        result["hits"] = len(hits)
        if not hits:
            result["status"] = "unavailable" if errors else "empty"
            if errors:
                result["error"] = errors[0]
            pending = any("маршрут до поиска" in str(err) for err in errors)
            if pending:
                # сеть ещё проверяется в фоне — это не «не нашли», запоминать нельзя,
                # иначе 10 минут будем считать, что бренда нет
                result["status"] = "pending"
                return result
            # неудачный поиск тоже запоминаем (ненадолго): иначе человек повторит
            # фразу и снова будет ждать поисковики
            self._web_cache[cache_key] = (time.time(), result)
            return result

        brands = parts.brands_from_hits(hits, sku, brand=brand)[:limit]
        result["brands"] = [
            {
                "brand": item["brand"],
                "sku": sku,
                "name": item.get("name_hint") or f"{item['brand']} {sku}",
                "source": "интернет",
                "why": item["why"],
                "url": item["url"],
                "evidence_score": item["score"],
                "sites": item["sites"],
            }
            for item in brands
        ]
        result["status"] = "ok" if brands else "empty"
        self._web_cache[cache_key] = (time.time(), result)
        return result

    def find_part_online(self, *, sku: str, term: str | None = None, brand: str | None = None) -> dict[str, Any]:
        """То же, что `web_lookup_part`, но в форме, удобной «мозгу» (инструмент бота)."""
        found = self.web_lookup_part(sku=sku, term=term, brand=brand or parts.find_brand_in_text(term or ""))
        return {
            "status": found.get("status"),
            "brands": found.get("brands") or [],
            "error": found.get("error"),
            "hint": "бренд бери только из brands; если их несколько — спроси человека",
        }

    def part_history(self, *, sku: str | None = None, brand: str | None = None,
                     term: str | None = None, limit: int = 20,
                     max_pages: int = 6) -> list[dict[str, Any]]:
        return self._cached(
            f"history|{sku}|{brand}|{term}|{limit}|{max_pages}",
            lambda: self._part_history(sku=sku, brand=brand, term=term,
                                       limit=limit, max_pages=max_pages),
        )

    def _part_history(self, *, sku: str | None = None, brand: str | None = None,
                      term: str | None = None, limit: int = 20,
                      max_pages: int | None = None) -> list[dict[str, Any]]:
        """Запчасти из прошлых сделок (products/get_filtered_orders), уникальные по бренд+артикул.

        Ищем серверным фильтром по артикулу (все написания) и по бренду; если ничего —
        проходим несколько страниц и сравниваем сами: серверного поиска по названию нет.
        """
        max_pages = max_pages or max(self.text_pages, 2)

        def collect(filters: dict[str, Any] | None, pages: int) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for page in range(1, pages + 1):
                # Отказ API не означает пустую историю: передаём ошибку вызывающему.
                chunk = self._page("part_history", "products/get_filtered_orders",
                                   filters=filters or None, page=page,
                                   limit=self.page_size,
                                   sort={"DATE_CREATE": "DESC"})
                if not chunk:
                    break
                rows.extend(r for r in chunk if isinstance(r, dict)
                            and (not sku or parts.sku_exact(r.get("SKU") or r.get("REQUIRE_SKU")) == parts.sku_exact(sku))
                            and (not brand or parts.brand_match(brand, r.get("BRAND") or r.get("REQUIRE_BRAND")) == 100))
            return rows

        found: list[dict[str, Any]] = []
        if sku:
            for variant in dict.fromkeys(parts.sku_variants(sku) + parts.sku_variants(parts.normalize_sku(sku))):
                found.extend(collect({"SKU": variant}, pages=2))
                if any(parts.known_brand(row.get("BRAND") or row.get("REQUIRE_BRAND")) for row in found):
                    break
            found.sort(key=lambda row: not parts.known_brand(row.get("BRAND") or row.get("REQUIRE_BRAND")))
        if not found and brand:
            found = collect({"BRAND": brand}, pages=2)
        if not found and not sku and (term or brand):
            found = collect(None, pages=max_pages)

        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in found:
            item = {
                "brand": row.get("BRAND") or row.get("REQUIRE_BRAND"),
                "sku": row.get("SKU") or row.get("REQUIRE_SKU"),
                "name": row.get("NAME"),
                "price": _money(row.get("PRICE")),
                "supplier_price": _money(row.get("SUPPLIER_PRICE")),
                "supplier": row.get("SUPPLIER_NAME"),
                "offer_id": row.get("OFFER_ID"),
            }
            key = (parts.canonical_brand(str(item["brand"] or "")),
                   parts.sku_key(str(item["sku"] or "")))
            if key in seen or not any(key):
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def resolve_employee(self, query: str | None, *, employees: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Ищет сотрудника по имени/фамилии из сказанного («Петя», «Петров», «Пётр Петров»).

        Возвращает:
            {"status": "matched", "employee": {...}}      — однозначное совпадение
            {"status": "ambiguous"|"not_found"|"empty", "candidates": [...]}
        """
        staff = employees if employees is not None else self.users()
        if not staff:
            return {"status": "empty", "candidates": []}
        needle = (query or "").strip().lower().replace("ё", "е")
        if not needle:
            return {"status": "not_found", "candidates": staff}

        words = [w for w in needle.replace(",", " ").split() if len(w) >= 2]
        scored: list[tuple[int, dict[str, Any]]] = []
        for emp in staff:
            name = str(emp.get("name") or "").lower().replace("ё", "е")
            parts = name.split()
            score = 0
            for word in words:
                if word in parts:
                    score += 3                      # точное слово целиком
                elif any(p.startswith(word) for p in parts):
                    score += 2                      # начало слова (Петя → Петров? нет, но Пет → Петров)
                elif word in name:
                    score += 1                      # подстрока
            if score:
                scored.append((score, emp))

        if not scored:
            # мягкий фолбэк: «Петя» → «Пётр» по первым 3 символам
            scored = [
                (1, emp) for emp in staff
                if any(
                    len(word) >= 3
                    and any(len(p) >= 3 and p[:3] == word[:3] for p in str(emp.get("name") or "").lower().split())
                    for word in words
                )
            ]

        if not scored:
            return {"status": "not_found", "candidates": staff}

        best = max(score for score, _ in scored)
        top = [emp for score, emp in scored if score == best]
        if len(top) == 1:
            return {"status": "matched", "employee": top[0]}
        return {"status": "ambiguous", "candidates": top}

    def assign_task_responsible(self, *, task_id: int, responsible_id: int) -> dict[str, Any]:
        """Назначить ответственного у существующей задачи."""
        payload = {"TASK_ID": int(task_id), "USER_ID": int(responsible_id)}
        return self.drafts.create(
            tool="assign_task_responsible",
            method="task/mark_member_as_responsible",
            payload=payload,
            risk="low",
            summary=f"Назначить исполнителем задачи {task_id} сотрудника {responsible_id}",
        )

    # алиас: бот-слой вызывает инструменты по именам draft_*
    draft_assign_task_responsible = assign_task_responsible

    def users(self) -> list[dict[str, Any]]:
        # Список сотрудников спрашивают на каждом выборе исполнителя — кэшируем.
        return self._cached("users", self._users, ttl=300.0)

    def _users(self) -> list[dict[str, Any]]:
        rows = self._page("users", "users/get_from_filter", limit=self.page_size)
        return [{
            "user_id": r.get("USER_ID"),
            "name": " ".join(filter(None, [r.get("LAST_NAME"), r.get("FIRST_NAME"), r.get("MIDDLE_NAME")])) or r.get("EMAIL"),
            "email": r.get("EMAIL"),
            "phone": r.get("PHONE"),
        } for r in rows]

    def tasks(self, *, status_id: int | None = None, board_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if status_id:
            filters["TASK_STATUS_ID"] = int(status_id)
        if board_id:
            filters["TASK_BOARD_ID"] = int(board_id)
        rows = self._page("tasks", "task/get_filtered", filters=filters or None, sort={"DATE_CREATE": "DESC"}, limit=limit)
        return [{
            "task_id": r.get("TASK_ID"),
            "title": r.get("TITLE"),
            "text": r.get("TEXT"),
            "deadline": r.get("DEADLINE"),
            "board_id": r.get("TASK_BOARD_ID"),
            "responsible_id": r.get("RESPONSIBLE_ID"),
        } for r in rows]

    def cashflow(self, *, date_from: Any = None, date_to: Any = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = {"DATE_CREATE": date_range(date_from, date_to)} if (date_from or date_to) else None
        return self._page("cashflow", "finances/get_filtered_transactions", filters=filters, limit=limit)

    def balances(self) -> Any:
        return self._read("balances", "balances/get_statistic", {})

    def makers_report(self, *, date_from: Any, date_to: Any, split_mode: int = 3, user_id: int | None = None) -> Any:
        payload: dict[str, Any] = {
            "DATE_FROM": to_ts(date_from),
            "DATE_TO": to_ts(date_to),
            "SPLIT_MODE": int(split_mode),
        }
        if user_id:
            payload["USER_ID"] = int(user_id)
        return self._read("makers_report", "makers/report", payload)

    # ---------------------------------------------------------------- DRAFTS
    def draft_create_offer(
        self, *, board_id: int, contact_id: int | None = None, phone: str | None = None,
        title: str | None = None, comment: str | None = None,
        _contact_snapshot: list[int] | None = None,
    ) -> dict[str, Any]:
        """Новая сделка только после проверки номера, без угадывания контакта."""
        if not phone or not 10 <= len(phone_key(phone)) <= 15:
            raise ValueError("Укажите полный телефон клиента для поиска")
        self._invalidate_cache()
        boards = [b for b in self.boards() if str(b.get("board_id")) == str(board_id) and b.get("active") != "N"]
        if len(boards) != 1:
            raise ValueError("Воронка не найдена или недоступна")
        matches = self._booking_contacts(phone)
        ids = sorted(int(c["contact_id"]) for c in matches)
        if _contact_snapshot is not None and _contact_snapshot != ids:
            raise ValueError("Список клиентов изменился; выберите клиента заново")
        if (len(matches) > 1 or any(c.get("search_incomplete") for c in matches)) and (contact_id is None or _contact_snapshot != ids):
            raise ValueError("По номеру найдено несколько клиентов или поиск неполон. Выберите клиента явно в чате.")
        if contact_id is not None and int(contact_id) not in ids:
            raise ValueError("Выбранный контакт не найден по этому телефону")
        contact = next((c for c in matches if c["contact_id"] == int(contact_id)), None) if contact_id else (matches[0] if matches else None)
        contact_id = int(contact["contact_id"]) if contact else None
        if not contact and (not str(title or "").strip() or str(title).strip().casefold() in {"новая сделка", "сделка", "клиент", "новый клиент"}):
            raise ValueError("Клиент не найден. Укажите настоящее имя нового клиента")
        self._booking_uncertain_client(phone, contact_id)
        for previous in self.drafts.list("all"):
            check = previous.get("check") or {}
            old_phone = check.get("phone") or (previous.get("payload") or {}).get("PHONE")
            if previous.get("tool") != "create_offer" or phone_key(old_phone) != phone_key(phone):
                continue
            if previous.get("status") in ("partial", "applying") or previous.get("archived_partial"):
                raise ValueError("Предыдущая попытка создания сделки не подтверждена. Проверьте CRM и /drafts; повтор заблокирован")
            if previous.get("status") == "pending":
                raise ValueError("Для этого номера уже есть черновик сделки. Проверьте /drafts, второй не создаю")
        payload: dict[str, Any] = {"BOARD_ID": int(board_id), "PHONE": str(phone)}
        name = str(contact.get("title") or f"Контакт №{contact_id}") if contact else str(title).strip()
        if contact:
            payload["CONTACT_ID"] = contact_id
        else:
            payload.update(PHONE=str(phone), TITLE=name)
        if comment:
            payload["COMMENT"] = comment
        board_name = str(boards[0].get("name") or board_id)
        summary = (f"Создать новую сделку в воронке «{board_name}» для существующего клиента {name} (контакт №{contact_id}, телефон {phone})"
                   if contact else f"Создать клиента {name} (телефон {phone}) и новую сделку в воронке «{board_name}»")
        if comment:
            summary += f". Комментарий: {comment}"
        check = {"kind": "standalone_offer", "phone": str(phone), "contact_id": contact_id,
                 "client_name": name, "board_id": int(board_id), "contact_matches": ids,
                 "incomplete": any(c.get("search_incomplete") for c in matches), "expected": dict(payload)}
        return self.drafts.create(tool="create_offer", method="offer/new/with_contact", payload=payload,
                                  summary=summary, extra={"check": check})

    def _apply_standalone_offer(self, token: str, current: dict[str, Any]) -> dict[str, Any]:
        from .booking import _id_from, key
        from .calendar import _positive
        check = current["check"]
        if current.get("status") == "pending":
            if current.get("method") != "offer/new/with_contact" or current.get("steps") or current.get("payload") != check.get("expected"):
                raise ValueError("Данные черновика изменились; запись остановлена")
            self._invalidate_cache()
            if not any(str(b.get("board_id")) == str(check["board_id"]) and b.get("active") != "N" for b in self.boards()):
                raise ValueError("Воронка больше недоступна")
            matches = self._booking_contacts(check["phone"])
            if sorted(int(c["contact_id"]) for c in matches) != check["contact_matches"]:
                raise ValueError("Клиенты по телефону изменились; подготовьте новый черновик")
            if bool(any(c.get("search_incomplete") for c in matches)) != bool(check.get("incomplete")):
                raise ValueError("Полнота поиска клиента изменилась; подготовьте новый черновик")
            if check.get("contact_id") is not None and check["contact_id"] not in check["contact_matches"]:
                raise ValueError("Контакт больше не найден по телефону")
            if check.get("contact_id") is not None:
                chosen = next(c for c in matches if c["contact_id"] == check["contact_id"])
                current_name = str(chosen.get("title") or f"Контакт №{check['contact_id']}")
                if current_name != check["client_name"]:
                    raise ValueError("Имя клиента изменилось после подготовки; проверьте его и подготовьте новый черновик")
            self._booking_uncertain_client(check["phone"], check.get("contact_id"))
            prior_ids = self._booking_offer_ids(check.get("contact_id"))
        else:
            prior_ids = set()
        draft = self.drafts.claim(token)
        created: dict[str, Any] = {}
        try:
            self.drafts.mark(token, "applying", booking_stage="создание сделки отправляется")
            response = self.client.call("offer/new/with_contact", draft["payload"])
            ident = _positive(response, "ID новой сделки") if isinstance(response, (int, str)) and not isinstance(response, bool) else _id_from(response, "OFFER_ID")
            created["OFFER_ID"] = ident
            self.drafts.mark(token, "applying", result=dict(created))
            if ident in prior_ids:
                raise ValueError("CRM вернула номер уже существовавшей сделки")
            self._invalidate_cache()
            offer = self._booking_offer(ident)
            if offer.get("error") or str(offer.get("offer_id")) != str(ident) or str(offer.get("board_id")) != str(check["board_id"]):
                raise ValueError("Не удалось подтвердить новую сделку и воронку")
            contact_id = _positive(offer.get("contact_id"), "Контакт сделки")
            created["CONTACT_ID"] = contact_id
            if check.get("contact_id") and contact_id != check["contact_id"]:
                raise ValueError("Сделка привязана к другому контакту")
            matches = self._booking_contacts(check["phone"], contact_id)
            if not any(c["contact_id"] == contact_id and (check.get("contact_id") or key(c.get("title")) == key(check["client_name"])) for c in matches):
                raise ValueError("Не удалось подтвердить телефон или имя клиента")
            created["SUMMARY"] = f"Новая сделка №{ident}, контакт №{contact_id}. {draft['summary']}"
            self.drafts.mark(token, "applied", applied_at=time.time(), result=created)
            return {"status": "applied", "token": token, "method": draft["method"], "result": created, "verified": True}
        except BaseException as exc:
            warning = (f"Результат создания сделки не подтверждён: {exc}. Известные номера: {created or 'нет'}. "
                       "Проверьте STOCRM и /drafts; повтор заблокирован, ничего автоматически не удаляю.")
            self.drafts.mark(token, "partial", failed_at=time.time(), result=created, check_warning=warning)
            if not isinstance(exc, Exception):
                raise
            return {"status": "partial", "token": token, "method": draft["method"], "result": created, "warning": warning, "verified": False}

    def draft_change_status(
        self, *, offer_id: int, status_id: int, board_id: int | None = None,
        reason_comment: str | None = None, reason_id: int | None = None,
    ) -> dict[str, Any]:
        """Черновик смены статуса. board_id можно не передавать — возьмём из самой сделки.

        Панель требует REASON_ID (причину перевода): без неё запись отклоняется с
        «Не указана причина перевода» — так 29.10 упал перевод сделки в «Мусор».
        Причину берём из `offer/all_statuses` (первую, если человек не выбрал), а в
        резюме пишем и название статуса, и причину — человеку понятнее, чем «статус 4».
        """
        if board_id is None:
            card = self.offer_card(int(offer_id))
            if card.get("error"):
                raise ValueError(f"Сделка {offer_id}: {card['error']}")
            board_id = card.get("board_id")
            if board_id is None:
                raise ValueError(
                    f"Не удалось определить воронку сделки {offer_id} — укажите board_id явно"
                )
        status_title, reasons = self._status_info(int(board_id), int(status_id))
        payload: dict[str, Any] = {
            "OFFER_ID": int(offer_id),
            "BOARD_ID": int(board_id),
            "STATUS_ID": int(status_id),
        }
        reason_title = ""
        if reason_id:
            chosen = next((r for r in reasons if str(r["reason_id"]) == str(reason_id)), None)
            payload["REASON_ID"] = int(reason_id)
            reason_title = str((chosen or {}).get("title") or reason_id)
        elif reasons:
            payload["REASON_ID"] = int(str(reasons[0]["reason_id"]))
            reason_title = str(reasons[0].get("title") or reasons[0]["reason_id"])
        if reason_comment:
            payload["REASON_COMMENT"] = reason_comment

        where = f"«{status_title}»" if status_title else f"статус {status_id}"
        summary = f"Перевести сделку {offer_id} в статус {where}"
        if reason_title:
            summary += f" (причина: {reason_title})"
        if reason_comment:
            summary += f", комментарий: {reason_comment}"
        if not payload.get("REASON_ID"):
            summary += " (панель может потребовать причину перевода — тогда укажите её)"
        return self.drafts.create(
            tool="change_status",
            method="offer/status/change",
            payload=payload,
            summary=summary,
        )

    def _status_info(self, board_id: int, status_id: int) -> tuple[str, list[dict[str, Any]]]:
        """Название статуса и его причины перевода (для смены статуса)."""
        try:
            rows = self.statuses(int(board_id))
        except StocrmError as exc:
            log.warning("не смог прочитать статусы воронки %s: %s", board_id, exc)
            return "", []
        for row in rows:
            if _same_id(row.get("status_id"), status_id):
                return str(row.get("title") or ""), list(row.get("reasons") or [])
        return "", []

    def draft_update_offer_field(self, *, offer_id: int, field: str, value: str) -> dict[str, Any]:
        allowed = {
            "COMMENT", "DOCUMENTS_COMMENT", "PAYMENT_COMMENT", "CAR_PROFILE_ID", "MILEAGE",
            "CITY_ID", "SEGMENT_ID", "CUSTOMER_ID", "CUSTOMER_REQUISITE_ID",
            "OFFER_TYPE_ID", "RESPONSIBLE_ID",
        }
        if field not in allowed:
            raise ValueError(f"Поле {field} менять нельзя. Разрешены: {', '.join(sorted(allowed))}")
        payload = {"OFFER_ID": int(offer_id), "FIELD_NAME": field, "DATA": str(value)}
        summary = f"В сделке {offer_id} изменить {field} на «{value}»"
        if field == "CAR_PROFILE_ID":
            # человеку важно видеть, ЧТО за автомобиль, а не «профиль 277»
            title = ""
            try:
                car = self._car_by_profile(int(value))
                title = str((car or {}).get("title") or "")
            except (TypeError, ValueError, StocrmError):
                title = ""
            summary = (f"Привязать автомобиль{f' {title}' if title else ''} "
                       f"(профиль {value}) к сделке {offer_id}")
        return self.drafts.create(
            tool="update_offer_field", method="offer/update_field", payload=payload,
            summary=summary,
        )

    def draft_create_contact(self, *, surname: str = "", firstname: str, middlename: str | None = None, phone: str | None = None) -> dict[str, Any]:
        if not str(firstname or "").strip():
            raise ValueError("Укажите имя клиента")
        data = {"SURNAME": surname, "FIRSTNAME": firstname}
        if middlename:
            data["MIDDLENAME"] = middlename
        payload: dict[str, Any] = {"DATA": data}
        if phone:
            payload["PROPERTIES"] = {"1": [{"VALUE": str(phone), "MAIN": "Y"}]}
        check: dict[str, Any] = {}
        if phone:
            if not 10 <= len(phone_key(phone)) <= 15:
                raise ValueError("Укажите полный номер телефона клиента")
            self._invalidate_cache()
            matches = self._booking_contacts(phone)
            if matches:
                names = ", ".join(f"{c.get('title') or 'Без имени'} (контакт №{c['contact_id']})" for c in matches[:10])
                raise ValueError("Этот телефон уже найден у клиента: " + names + ". Новый контакт не создаю.")
            for previous in self.drafts.list("all"):
                old = previous.get("check") or {}
                if previous.get("tool") != "create_contact":
                    continue
                old_props = ((previous.get("payload") or {}).get("PROPERTIES") or {}).get("1") or []
                old_phone = old.get("phone") or (old_props[0].get("VALUE") if isinstance(old_props, list) and old_props and isinstance(old_props[0], dict) else None)
                if phone_key(old_phone) != phone_key(phone):
                    continue
                uncertain_old = previous.get("status") == "applied" and old.get("kind") != "added_contact"
                if previous.get("status") in ("partial", "applying") or previous.get("archived_partial") or uncertain_old:
                    raise ValueError("Предыдущая попытка создания клиента с этим номером не подтверждена. Проверьте CRM и /drafts; повтор заблокирован.")
                if previous.get("status") == "pending":
                    if previous.get("payload") == payload:
                        return previous
                    raise ValueError("Для этого номера уже подготовлен другой черновик. Проверьте /drafts; второй не создаю.")
            check = {"kind": "added_contact", "phone": str(phone)}
        return self.drafts.create(
            tool="create_contact", method="contact/create", payload=payload,
            summary=f"Создать контакт {surname} {firstname} {middlename or ''}".strip()
                    + (f", телефон {phone}" if phone else ""), extra={"check": check} if check else None,
        )

    # ------------------------------------------------- правка карточки клиента
    CONTACT_FLAT_FALLBACK = ("TITLE", "SURNAME", "FIRSTNAME", "MIDDLENAME", "PHONE", "EMAIL")

    def contact_flat_fields(self) -> list[str]:
        """Названия полей, которые принимает `contact/update` (FLAT). Кэш на процесс.

        По документации (WIKI-A-645) список отдаёт `contact/get_flat_fields`. Метод есть
        не в каждой панели, поэтому при ошибке работаем по известному набору.
        """
        if self._flat_fields is not None:
            return self._flat_fields
        try:
            raw = self._read("contact_flat_fields", "contact/get_flat_fields", {})
        except StocrmError:
            self._flat_fields = list(self.CONTACT_FLAT_FALLBACK)
            return self._flat_fields
        names: list[str] = []
        items = raw.get("DATA") if isinstance(raw, dict) and isinstance(raw.get("DATA"), list) else raw
        if isinstance(items, dict):
            items = list(items.values())
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                value = item.get("FIELD") or item.get("CODE") or item.get("NAME") or item.get("KEY")
                if value:
                    names.append(str(value))
        self._flat_fields = names or list(self.CONTACT_FLAT_FALLBACK)
        return self._flat_fields

    # ------------------------------------------------------------------- ТО
    def car_for_offer(self, offer_id: int, *, car_profile_id: int | None = None,
                      car_hint: str | None = None) -> dict[str, Any]:
        """Автомобиль для сделки: VIN и профиль авто клиента.

        Порядок: явно выбранный автомобиль (car_profile_id) или названная марка/модель
        (car_hint: «Audi») → профиль из карточки сделки → VIN сделки → единственный
        профиль клиента. Если авто не определить, поднимаем ValueError с понятным шагом.

        Зачем car_profile_id/car_hint: человек говорит «выбери Audi и собери ТО».
        Привязка авто к сделке — это ЧЕРНОВИК (в панели ещё ничего не изменилось),
        поэтому читать VIN из карточки сделки бессмысленно — берём тот профиль,
        который человек только что выбрал.
        """
        # Раньше здесь вызывался offer_card — он дополнительно тянет балансы оплат.
        # Для подбора по VIN они не нужны: берём только саму строку сделки (на 1 запрос меньше).
        row, seen = self._find_offer_row(int(offer_id))
        if row is None:
            raise ValueError(
                f"Сделка {offer_id} не найдена: просмотрел {seen} строк в панели. "
                "Проверьте номер сделки"
            )
        vin = _norm_vin(row.get("CAR_VIN") or row.get("VIN"))
        profile_id = row.get("CAR_PROFILE_ID")
        contact_id = row.get("CONTACT_ID")
        note = ""
        car: dict[str, Any] | None = None
        try:
            if car_profile_id:
                car = self._car_by_profile(int(car_profile_id), contact_id=contact_id)
            if car_profile_id and car is None:
                raise ValueError(
                    f"Автомобиль с профилем {car_profile_id} не найден у клиента {contact_id}. "
                    "Уточните автомобиль — покажу список"
                )
            if profile_id and contact_id:
                rows = self.cars(contact_id=int(contact_id))
                car = next(
                    (r for r in rows if str(r.get("car_profile_id")) == str(profile_id)), None
                )
            if car is None and vin:
                rows = self.cars(vin=vin)
                car = rows[0] if rows else None
            if car is None and contact_id:
                rows = self.cars(contact_id=int(contact_id))
                if len(rows) == 1:
                    car = rows[0]
                elif len(rows) > 1:
                    # «собери ТО по Audi» — марка в тексте уже названа, не переспрашиваем
                    matched = self._car_by_hint(rows, car_hint) if car_hint else None
                    if matched is not None:
                        car = matched
                    else:
                        titles = "; ".join(str(r.get("title") or r.get("car_profile_id")) for r in rows[:5])
                        note = (f"у клиента {contact_id} несколько автомобилей ({titles}) — "
                                "VIN в карточке сделки не заполнен, уточните авто")
        except StocrmError as exc:
            note = f"профили автомобилей недоступны ({exc})"

        if car is None and not vin:
            raise ValueError(
                (note + ". " if note else "")
                + f"У сделки {offer_id} не заполнен VIN и не найден профиль автомобиля клиента. "
                "Заполните VIN в карточке сделки (или добавьте авто клиенту) — и повторите «собери ТО»"
            )
        return {
            "offer_id": int(offer_id),
            "contact_id": contact_id,
            "vin": vin or _norm_vin((car or {}).get("vin")),
            "car_profile_id": (car or {}).get("car_profile_id") or profile_id,
            "car": car or {},
            "title": " ".join(
                str(x) for x in ((car or {}).get("brand"), (car or {}).get("model"),
                                 (car or {}).get("year")) if x
            ) or str((car or {}).get("title") or ""),
            "note": note,
        }

    def cars_for_offer(self, offer_id: int) -> list[dict[str, Any]]:
        """Все автомобили клиента из сделки — чтобы предложить выбор, а не требовать VIN."""
        try:
            row, _seen = self._find_offer_row(int(offer_id))
        except (StocrmError, ValueError):
            return []
        contact_id = (row or {}).get("CONTACT_ID")
        if not contact_id:
            return []
        try:
            return self.cars(contact_id=int(contact_id))
        except (StocrmError, ValueError):
            return []

    def _car_by_profile(self, car_profile_id: int, *, contact_id: Any = None) -> dict[str, Any] | None:
        """Профиль автомобиля по ID (у клиента сделки, иначе — по всем доступным)."""
        rows: list[dict[str, Any]] = []
        if contact_id:
            rows = self.cars(contact_id=int(contact_id))
        found = next((r for r in rows if str(r.get("car_profile_id")) == str(car_profile_id)), None)
        if found is None:
            # профиль мог быть у другого контакта (или сделка без клиента)
            found = next(
                (r for r in self.cars() if str(r.get("car_profile_id")) == str(car_profile_id)),
                None,
            )
        return found

    @staticmethod
    def _car_by_hint(rows: list[dict[str, Any]], hint: str | None) -> dict[str, Any] | None:
        """Единственный автомобиль, подходящий под названную марку/модель («Audi», «X-Trail»)."""
        words = [w for w in parts.normalize_text(hint).split() if len(w) >= 2]
        if not words:
            return None
        matched = []
        for row in rows:
            haystack = parts.normalize_text(
                " ".join(str(row.get(k) or "") for k in ("brand", "model", "title")))
            if all(word in haystack for word in words):
                matched.append(row)
        return matched[0] if len(matched) == 1 else None

    def maintenance_filters(
        self,
        *,
        offer_id: int,
        brand: str = DEFAULT_MAINTENANCE_BRAND,
        car_profile_id: int | None = None,
        car_hint: str | None = None,
        limit: int = 6,
    ) -> dict[str, Any]:
        """Подбирает воздушный и масляный фильтры указанного бренда по авто сделки (VIN).

        По каждому фильтру:
          * status="matched"   — ровно одна подходящая позиция, её и берём;
          * status="ambiguous" — вариантов несколько, пусть человек выберет;
          * status="not_found" — у этого бренда в панели ничего нет.
        """
        car_info = self.car_for_offer(offer_id, car_profile_id=car_profile_id, car_hint=car_hint)
        car = car_info.get("car") or {}
        # Год в запрос НЕ добавляем: «2019» распознавалось как артикул, и бот уходил
        # в интернет-поиск по «2019» вместо подбора фильтра.
        car_name = " ".join(str(x) for x in (car.get("brand"), car.get("model")) if x)
        found_list: list[dict[str, Any]] = []
        for kind, title in MAINTENANCE_KINDS:
            wanted = parts.canonical_brand(brand)
            tried: list[str] = []
            warnings: list[str] = []
            candidates: list[dict[str, Any]] = []
            brand_fallback = False
            last_query = title
            # Запросы от узкого к широкому. Раньше запрос был один — «фильтр масляный
            # Zekkert Toyota Camry»; если панель такое не находит, бот писал «масляного
            # фильтра нет» и НИЧЕГО не предлагал на выбор. 29.10 человек именно так и
            # остался без фильтров в просчёте ТО.
            #
            # Последние два захода — вообще без бренда: если Zekkert для этой машины в
            # панели нет, покажем то, что есть (MANN, Bosch, Filtron…), и дадим выбрать.
            # Сам такой фильтр без выбора человека не добавляем: бренд не придумываем.
            attempts: list[tuple[str, str | None]] = [
                (" ".join(x for x in (title, brand, car_name) if x).strip(), brand),
                (" ".join(x for x in (title, brand) if x).strip(), brand),
                (" ".join(x for x in (title, car_name) if x).strip(), brand),
                (title, brand),
                (" ".join(x for x in (title, car_name) if x).strip(), None),
                (title, None),
            ]
            for query, brand_arg in attempts:
                if not query:
                    continue
                tried.append(query)
                last_query = query
                try:
                    found = self.resolve_part(query=query, brand=brand_arg,
                                              offer_id=int(offer_id), limit=limit)
                except (StocrmError, ValueError) as exc:
                    warnings.append(str(exc))
                    continue
                # Берём только позиции ЭТОГО типа: масляный фильтр нельзя предлагать
                # вместо воздушного, даже если других вариантов у бренда нет.
                found_kind = [
                    c for c in (found.get("candidates") or [])
                    if _is_filter_of_kind(c.get("name"), kind)
                ]
                if not found_kind:
                    continue
                same_brand = [
                    c for c in found_kind if parts.brand_match(wanted, c.get("brand")) >= 70
                ]
                if same_brand:
                    candidates = same_brand
                    break
                if brand_arg is None:
                    # фильтров нужного бренда нет вовсе — предлагаем другие, но выбор
                    # остаётся за человеком
                    candidates = found_kind
                    brand_fallback = True
                    break
            if brand_fallback:
                for candidate in candidates:
                    candidate["brand_mismatch"] = True
            # Если подходящий по типу фильтр есть, но другого бренда — это всегда вопрос
            # человеку, даже когда вариант один: бренд не придумываем.
            if brand_fallback:
                status = "ambiguous"
            else:
                status = "matched" if len(candidates) == 1 else (
                    "ambiguous" if candidates else "not_found"
                )
            if car_info.get("vin"):
                status_note = f"по VIN {car_info['vin']}" + (f" → {car_name}" if car_name else "")
            else:
                status_note = f"по авто из карточки клиента{f': {car_name}' if car_name else ''}"
            found_list.append({
                "kind": kind,
                "title": title,
                "query": last_query,
                "status": status,
                "brand_fallback": brand_fallback,
                "requested_brand": brand,
                "candidates": candidates,
                "best": candidates[0] if status == "matched" else None,
                "why": status_note,
                "tried": tried,
                "warnings": warnings,
            })
        return {"car": car_info, "brand": brand, "parts": found_list}

    def _flat_key(self, want: str) -> str | None:
        """Подбирает настоящее имя поля в панели (регистр/синонимы)."""
        fields = [str(f) for f in self.contact_flat_fields()]
        upper = {f.upper(): f for f in fields}
        if want.upper() in upper:
            return upper[want.upper()]
        aliases = {
            "TITLE": ("TITLE", "NAME", "FULL_NAME", "CONTACT_TITLE"),
            "FIRSTNAME": ("FIRSTNAME", "FIRST_NAME", "NAME"),
            "SURNAME": ("SURNAME", "LASTNAME", "LAST_NAME"),
            "MIDDLENAME": ("MIDDLENAME", "MIDDLE_NAME", "SECONDNAME"),
            "PHONE": ("PHONE", "MAIN_PHONE", "PHONE_NUMBER"),
            "EMAIL": ("EMAIL", "E_MAIL", "MAIL"),
        }
        for candidate in aliases.get(want.upper(), (want.upper(),)):
            if candidate in upper:
                return upper[candidate]
        return None

    def draft_update_contact(
        self,
        *,
        contact_id: int | None = None,
        phone: str | None = None,
        title: str | None = None,
        surname: str | None = None,
        firstname: str | None = None,
        middlename: str | None = None,
        email: str | None = None,
        new_phone: str | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК правки карточки клиента: ФИО, телефон, e-mail.

        `phone` — телефон, по которому найти контакт (если нет contact_id),
        `new_phone` — новый номер, который нужно записать.
        """
        if not contact_id and not phone:
            raise ValueError("Нужен contact_id (или телефон клиента), иначе непонятно, кого менять")
        wanted = {"TITLE": title, "SURNAME": surname, "FIRSTNAME": firstname,
                  "MIDDLENAME": middlename, "EMAIL": email, "PHONE": new_phone}
        flat: dict[str, str] = {}
        skipped: list[str] = []
        for field, value in wanted.items():
            if value in (None, ""):
                continue
            key = self._flat_key(field)
            if key is None:
                skipped.append(field)
                continue
            flat[key] = str(value).strip()
        if not flat:
            raise ValueError("Нечего менять: укажите хотя бы одно поле (имя, телефон, e-mail)")

        payload: dict[str, Any] = {"FLAT": flat}
        if contact_id:
            payload["CONTACT_ID"] = int(contact_id)
        else:
            payload["PHONE"] = str(phone)

        changes = ", ".join(f"{field}={value}" for field, value in wanted.items() if value not in (None, ""))
        summary = f"Изменить клиента {contact_id or phone}: {changes}"
        draft = self.drafts.create(
            tool="update_contact", method="contact/update", payload=payload, summary=summary,
        )
        if skipped:
            draft["warning"] = ("в панели нет полей: " + ", ".join(skipped)
                                + " — они не изменятся, проверьте карточку вручную")
        return draft

    # ------------------------------------------------------- автомобили клиента
    def cars(self, *, contact_id: int | None = None, vin: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        return self._cached(
            f"cars|{contact_id}|{vin}|{limit}",
            lambda: self._cars(contact_id=contact_id, vin=vin, limit=limit),
        )

    def _cars(self, *, contact_id: int | None = None, vin: str | None = None,
              limit: int = 50) -> list[dict[str, Any]]:
        """Профили автомобилей (car_profile/get_filtered_profiles)."""
        filters: dict[str, Any] = {}
        if contact_id:
            filters["CONTACT_ID"] = int(contact_id)
        if vin:
            filters["VIN"] = str(vin)
        rows = self._page("cars", "car_profile/get_filtered_profiles",
                          filters=filters or None, limit=limit)
        out = []
        for r in rows[:limit]:
            out.append({
                "car_profile_id": r.get("CAR_PROFILE_ID"),
                "contact_id": r.get("CONTACT_ID"),
                "title": r.get("TITLE"),
                "vin": r.get("VIN"),
                "brand_id": r.get("BRAND_ID"),
                "brand": r.get("BRAND_NAME"),
                "model_id": r.get("MODEL_ID"),
                "model": r.get("MODEL_NAME"),
                "year": r.get("YEAR"),
                "license_plate": r.get("LICENSE_PLATE_NUMBER"),
                "mileage": r.get("MILEAGE"),
                "color_id": r.get("CAR_COLOR_ID"),
            })
        return out

    def car_brands(self, *, type_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Справочник брендов автомобилей (carselect/get_all_brands) с кэшем."""
        if self._car_brands is not None and not type_id:
            return self._car_brands
        raw = self._read("car_brands", "carselect/get_all_brands",
                         {"TYPE_ID": int(type_id)} if type_id else {})
        items = _as_items(raw)
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            brand_id = item.get("BRAND_ID") or item.get("ID")
            name = item.get("NAME") or item.get("TITLE") or item.get("BRAND_NAME")
            if brand_id and name:
                out.append({"brand_id": int(brand_id), "name": str(name)})
        out = out[:limit]
        if not type_id:
            self._car_brands = out
        return out

    def car_models(self, brand_id: int, *, limit: int = 500) -> list[dict[str, Any]]:
        """Модели выбранного бренда (carselect/get_models)."""
        raw = self._read("car_models", "carselect/get_models", {"BRAND_ID": int(brand_id)})
        out = []
        for item in _as_items(raw):
            if not isinstance(item, dict):
                continue
            model_id = item.get("MODEL_ID") or item.get("ID")
            name = item.get("NAME") or item.get("TITLE")
            if model_id and name:
                out.append({"model_id": int(model_id), "name": str(name)})
        return out[:limit]

    @staticmethod
    def _match_by_name(items: list[dict[str, Any]], key: str, wanted: str) -> dict[str, Any] | None:
        """Ищет запись справочника по названию.

        Порядок: точное совпадение → без регистра/ё → вхождение слова →
        сравнение латиницей («королла» ↔ «Corolla») по похожести.
        """
        needle = (wanted or "").strip().lower().replace("ё", "е")
        if not needle:
            return None
        for item in items:
            if str(item.get(key) or "").lower().replace("ё", "е") == needle:
                return item
        for item in items:
            name = str(item.get(key) or "").lower().replace("ё", "е")
            if needle in name or name in needle:
                return item

        want_latin = parts.translit_latin(needle)
        best: tuple[float, dict[str, Any] | None] = (0.0, None)
        for item in items:
            name = str(item.get(key) or "").lower().replace("ё", "е")
            ratio = SequenceMatcher(None, want_latin, parts.translit_latin(name)).ratio()
            # «жигули»/«Lada» так не сойдутся, но «королла»/«Corolla» — да
            if ratio > best[0]:
                best = (ratio, item)
        return best[1] if best[0] >= 0.75 else None

    def draft_update_car(
        self,
        *,
        car_profile_id: int | None = None,
        contact_id: int | None = None,
        vin: str | None = None,
        brand: str | None = None,
        model: str | None = None,
        year: int | str | None = None,
        title: str | None = None,
        license_plate: str | None = None,
        mileage: int | str | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК правки автомобиля клиента: VIN, марка, модель, год.

        Марка и модель в API задаются ID из справочников (WIKI-A-1269/1270), поэтому
        названия переводим в ID. Если бренд/модель не нашлись — не выдумываем ID,
        а пишем свободное название в TITLE и предупреждаем.
        """
        if not car_profile_id and not vin and not contact_id:
            raise ValueError(
                "Нужен car_profile_id, VIN или contact_id (клиент), иначе непонятно, "
                "какой автомобиль менять"
            )

        args: dict[str, Any] = {}
        notes: list[str] = []
        if vin:
            args["VIN"] = str(vin).strip().upper()
        if year not in (None, ""):
            try:
                args["YEAR"] = int(str(year).strip())
            except ValueError as exc:
                raise ValueError(f"Год должен быть числом, а не «{year}»") from exc
        if license_plate:
            args["LICENSE_PLATE_NUMBER"] = str(license_plate).strip().upper()
        if mileage not in (None, ""):
            try:
                args["MILEAGE"] = int(str(mileage).strip())
            except ValueError as exc:
                raise ValueError(f"Пробег должен быть числом, а не «{mileage}»") from exc

        if brand:
            try:
                brands = self.car_brands()
            except StocrmError as exc:
                brands = []
                notes.append(f"справочник марок недоступен ({exc}) — марку проверьте вручную")
            found = self._match_by_name(brands, "name", brand) if brands else None
            if found:
                args["BRAND_ID"] = found["brand_id"]
                notes.append(f"марка: {found['name']}")
                if model:
                    try:
                        models = self.car_models(found["brand_id"])
                    except StocrmError:
                        models = []
                    found_model = self._match_by_name(models, "name", model) if models else None
                    if found_model:
                        args["MODEL_ID"] = found_model["model_id"]
                        notes.append(f"модель: {found_model['name']}")
                    else:
                        notes.append(f"модель «{model}» в справочнике не нашлась — проверьте вручную")
            elif brands:
                notes.append(f"марка «{brand}» в справочнике не нашлась — проверьте вручную")
        if title:
            args["TITLE"] = str(title).strip()
        if not args:
            raise ValueError("Нечего менять: укажите VIN, марку/модель или год")
        if contact_id and not car_profile_id:
            candidates = self.cars(contact_id=int(contact_id))
            if len(candidates) == 1:
                car_profile_id = int(candidates[0]["car_profile_id"])
                notes.append(f"автомобиль клиента: {candidates[0].get('title') or car_profile_id}")
            elif not candidates:
                raise ValueError(f"У клиента {contact_id} нет профиля автомобиля — сначала добавьте его")
            else:
                names = "; ".join(
                    f"{c.get('title') or 'без названия'} (car_profile_id {c.get('car_profile_id')})"
                    for c in candidates[:5]
                )
                raise ValueError(
                    f"У клиента {contact_id} несколько автомобилей: {names}. "
                    "Уточните, какой менять (car_profile_id)"
                )

        payload: dict[str, Any] = {"ARGS": args}
        if car_profile_id:
            payload["CAR_PROFILE_ID"] = int(car_profile_id)
        summary = "Изменить автомобиль " + (f"#{car_profile_id}" if car_profile_id else f"VIN {vin}")
        human = ", ".join(notes) if notes else ", ".join(f"{k}={v}" for k, v in args.items())
        summary += f": {human}"
        draft = self.drafts.create(
            tool="update_car", method="car/edit", payload=payload, summary=summary,
        )
        unclear = [n for n in notes if "не нашлась" in n or "проверьте вручную" in n]
        if unclear:
            draft["warning"] = "; ".join(unclear)
        return draft

    def _default_task_board(self) -> int:
        """Доску задач в API не перечислить, поэтому выводим её из уже существующих задач."""
        try:
            rows = self.tasks(limit=50)
        except StocrmError:
            rows = []
        boards = {r["board_id"] for r in rows if r.get("board_id")}
        if len(boards) == 1:
            return int(boards.pop())
        if not boards:
            raise ValueError(
                "Не знаю, на какую доску задач ставить задачу (в CRM не нашлось ни одной задачи). "
                "Укажите board_id — его видно в существующих задачах или в интерфейсе STOCRM"
            )
        names = ", ".join(str(b) for b in sorted(boards))
        raise ValueError(f"Досок задач несколько ({names}) — уточните, на какую ставить (board_id)")

    def draft_create_task(
        self,
        *,
        board_id: int | None = None,
        title: str,
        text: str | None = None,
        deadline: Any = None,
        responsible_id: int | None = None,
        responsible_name: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("У задачи должно быть название (title)")
        if board_id is None:
            board_id = self._default_task_board()
        payload: dict[str, Any] = {"TASK_BOARD_ID": int(board_id), "TITLE": title}
        if text:
            payload["TEXT"] = text
        ts = to_ts(deadline)
        if ts:
            payload["DEADLINE"] = ts

        summary = f"Создать задачу «{title}» на доске {board_id}" + (f" со сроком {deadline}" if deadline else "")
        steps: list[dict[str, Any]] = []
        if responsible_id:
            # В API STOCRM ответственный ставится отдельным вызовом уже после создания задачи,
            # поэтому черновик составной: создаём задачу → назначаем исполнителя.
            steps = [
                {"method": "task/create", "payload": {"DATA": payload}},
                {
                    "method": "task/mark_member_as_responsible",
                    "payload": {"TASK_ID": {"$from_step": 0}, "USER_ID": int(responsible_id)},
                },
            ]
            who = responsible_name or f"user_id={responsible_id}"
            summary += f". Исполнитель: {who}"
            return self.drafts.create(
                tool="create_task", method="task/create", payload={"DATA": payload},
                summary=summary, steps=steps,
            )

        return self.drafts.create(
            tool="create_task", method="task/create", payload={"DATA": payload}, summary=summary,
        )

    # ------------------------------------------------------------------ заметки
    def notes(self, *, entity: str = "OFFER_NOTE", code: int | None = None,
              limit: int = 100) -> list[dict[str, Any]]:
        """Заметки панели по сущности и её коду (для сделки CODE = номер сделки)."""
        filters: dict[str, Any] = {"ENTITY": entity}
        if code is not None:
            filters["CODE"] = int(code)
        payload = {"FILTER": filters, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": 1, "LIMIT": limit}
        raw = self._read(f"notes:{entity}", "notes/get_filtered", payload)
        out = []
        for row in _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw):
            if (code is not None and row.get("CODE") not in (None, "")
                    and str(row.get("CODE")) != str(int(code))):
                continue
            if row.get("ENTITY") and str(row["ENTITY"]) != entity:
                continue
            out.append({
                "note_id": row.get("NOTE_ID"),
                "entity": row.get("ENTITY"),
                "code": row.get("CODE"),
                "text": row.get("TEXT"),
                "unique": row.get("UNIQUE"),
                "created": row.get("DATE_CREATE"),
                "updated": row.get("DATE_UPDATE"),
                "creator_id": row.get("CREATOR_ID"),
                "updater_id": row.get("UPDATER_ID"),
            })
        return out

    def offer_notes(self, offer_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Все заметки сделки — сделку панель держит под двумя названиями сущности."""
        seen: dict[Any, dict[str, Any]] = {}
        errors: list[StocrmError] = []
        for entity in NOTE_ENTITIES:
            try:
                rows = self.notes(entity=entity, code=int(offer_id), limit=limit)
            except StocrmError as exc:
                log.debug("заметки %s недоступны: %s", entity, exc)
                errors.append(exc)
                continue
            for row in rows:
                seen.setdefault(row.get("note_id") or id(row), row)
        if len(errors) == len(NOTE_ENTITIES):
            raise errors[0]
        return sorted(seen.values(), key=lambda r: str(r.get("created") or ""), reverse=True)[:limit]

    def _note_for_change(self, offer_id: int, *, note_id: int | None,
                         query: str | None) -> dict[str, Any]:
        """Какую заметку правим: по номеру, по словам человека или последнюю."""
        rows = self.offer_notes(int(offer_id), limit=200)
        if not rows:
            raise ValueError(
                f"В сделке {offer_id} нет заметок — править нечего. "
                "Скажите текст, и я подготовлю новую заметку"
            )
        if note_id:
            found = next((r for r in rows if _same_id(r.get("note_id"), note_id)), None)
            if found is None:
                raise ValueError(
                    f"В сделке {offer_id} нет заметки с номером {note_id}. "
                    f"Последние: " + "; ".join(
                        f"№{r.get('note_id')} «{str(r.get('text') or '')[:40]}»" for r in rows[:5])
                )
            return found
        needle = parts.normalize_text(query or "")
        if needle:
            matches = [r for r in rows if _note_matches(str(r.get("text") or ""), needle)]
            if not matches:
                raise ValueError(
                    f"Не нашёл заметку со словами «{query}» в сделке {offer_id}. "
                    f"Последние: " + "; ".join(
                        f"№{r.get('note_id')} «{str(r.get('text') or '')[:40]}»" for r in rows[:5])
                )
            if len(matches) > 1:
                listing = "; ".join(
                    f"№{r.get('note_id')} «{str(r.get('text') or '')[:40]}»" for r in matches[:5])
                raise ValueError(
                    f"Под «{query}» подходят несколько заметок: {listing}. Скажите номер"
                )
            return matches[0]
        # не назвали какую — берём САМУЮ СВЕЖУЮ и говорим об этом в резюме
        return rows[0]

    def draft_update_offer_note(
        self,
        *,
        offer_id: int,
        text: str,
        note_id: int | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК правки заметки в сделке.

        В Public API описан только `note/create`, поэтому пробуем известные написания
        метода правки, а после записи ПЕРЕЧИТЫВАЕМ заметки: если текст не изменился —
        честно говорим об этом и подсказываем, что сделать в панели.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("Пустой текст заметки — править нечего")
        note = self._note_for_change(int(offer_id), note_id=note_id, query=query)
        old_text = str(note.get("text") or "")
        payload: dict[str, Any] = {"NOTE_ID": int(note["note_id"]), "TEXT": text,
                                   "ARGS": {"OVERWRITE": "Y", "UNIQUE": "N"},
                                   "ENTITY": str(note.get("entity") or "OFFER_NOTE"),
                                   "CODE": int(offer_id)}
        variants = [
            {"NOTE_ID": int(note["note_id"]), "TEXT": text},
            dict(payload),
            {"CODE": int(offer_id), "ENTITY": str(note.get("entity") or "OFFER_NOTE"),
             "TEXT": text, "ARGS": {"OVERWRITE": "Y", "UNIQUE": "N"}},
        ]
        steps = [{"method": NOTE_UPDATE_METHODS[0], "payload": variants[0],
                  "fallbacks": [{"method": m, "payload": dict(v)}
                                for v in variants for m in NOTE_UPDATE_METHODS]
                  + [{"method": m, "payload": dict(variants[1])}
                     for m in NOTE_UPDATE_METHODS]}]
        summary = (f"Изменить заметку №{note['note_id']} в сделке {offer_id}: "
                   f"«{old_text[:60]}» → «{text[:80]}»")
        return self.drafts.create(
            tool="update_offer_note", method=NOTE_UPDATE_METHODS[0],
            payload=variants[0], summary=summary, steps=steps, risk="low",
            extra={"check": {"kind": "updated_note", "offer_id": int(offer_id),
                             "note_id": note["note_id"], "text": text}},
        )

    def _verify_updated_note(self, check: dict[str, Any]) -> str | None:
        """Текст заметки правда изменился? None — да."""
        self._invalidate_cache()
        try:
            rows = self.offer_notes(int(check.get("offer_id") or 0), limit=200)
        except StocrmError:
            return "Не удалось проверить результат в STOCRM. Что сделать: откройте сделку и проверьте запись; не повторяйте действие до проверки."
        found = next((r for r in rows if _same_id(r.get("note_id"), check.get("note_id"))), None)
        if found is None:
            return (f"Заметка №{check.get('note_id')} в сделке больше не видна — "
                    "проверьте её в панели, возможно запись не сохранилась")
        if parts.normalize_text(str(found.get("text") or "")) == parts.normalize_text(
                str(check.get("text") or "")):
            return None
        return (
            "STOCRM не дал изменить уже созданную заметку: в Public API описан только "
            "«Создать заметку», метода правки нет.\n"
            "Что сделать: в сделке откройте «Заметки и задачи» и поправьте текст, "
            "или скажите «добавь заметку …» — я создам новую.\n"
            "Старый текст не тронут."
        )

    def _verify_added_note(self, check: dict[str, Any]) -> str | None:
        """Заметка правда появилась у сделки? None — да."""
        text_key = parts.normalize_text(str(check.get("text") or ""))
        self._invalidate_cache()
        try:
            rows = self.offer_notes(int(check.get("offer_id") or 0), limit=100)
        except StocrmError:
            return "Не удалось проверить результат в STOCRM. Что сделать: откройте сделку и проверьте запись; не повторяйте действие до проверки."
        if any(text_key and text_key == parts.normalize_text(str(r.get("text") or ""))
               and str(r.get("code")) == str(check.get("offer_id"))
               and (not check.get("note_id") or _same_id(r.get("note_id"), check["note_id"]))
               for r in rows):
            return None
        return ("Появление заметки в сделке не подтверждено — проверьте блок «Заметки и задачи» "
                "в карточке. Текст: «" + str(check.get("text") or "")[:120] + "»")

    # -------------------------------------------------------------------- теги
    def tags(self) -> list[dict[str, Any]]:
        """Все теги организации (TAG_ID, NAME, HEX)."""
        return self._cached("tags", self._tags, ttl=600.0)

    def _tags(self) -> list[dict[str, Any]]:
        raw = self._read("tags", "tags/get_filtered_tags",
                         {"SORT": {"NAME": "ASC"}, "PAGE": 1, "LIMIT": 500})
        rows = _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw)
        return [{"tag_id": r.get("TAG_ID"), "name": r.get("NAME"), "hex": r.get("HEX")}
                for r in rows if r.get("TAG_ID")]

    def tag_links(self, *, entity: str | None = None, code: Any = None,
                  tag_id: Any = None, limit: int = 500) -> list[dict[str, Any]]:
        """Связи «тег ↔ объект»: кто с каким тегом и на каком объекте."""
        filters: dict[str, Any] = {}
        if entity:
            filters["ENTITY"] = entity
        if code not in (None, ""):
            filters["CODE"] = code
        if tag_id:
            filters["TAG_ID"] = tag_id
        payload = {"FILTER": filters, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": 1, "LIMIT": limit}
        raw = self._read("tag_links", "tags/get_filtered_links", payload)
        rows = _as_items(raw.get("DATA") if isinstance(raw, dict) and "DATA" in raw else raw)
        return [{
            "link_id": r.get("TAG_LINK_ID"),
            "tag_id": r.get("TAG_ID"),
            "name": r.get("NAME"),
            "entity": r.get("ENTITY"),
            "code": r.get("CODE"),
            "created": r.get("DATE_CREATE"),
            "creator_id": r.get("CREATOR_ID"),
        } for r in rows]

    def offer_tags(self, offer_id: int) -> list[dict[str, Any]]:
        """Теги карточки сделки и теги её позиций (с пометкой, где какой)."""
        found: dict[Any, dict[str, Any]] = {}
        for entity in TAG_ENTITIES_OFFER:
            for link in self.tag_links(entity=entity, code=int(offer_id)):
                found.setdefault(link.get("link_id"), {**link, "where": "сделка"})
        try:
            orders = self.offer_parts(int(offer_id), limit=200)
        except StocrmError:
            orders = []
        ids = [str(o.get("order_id")) for o in orders if o.get("order_id")]
        if ids:
            for entity in TAG_ENTITIES_ORDER:
                try:
                    links = self.tag_links(entity=entity)
                except StocrmError:
                    continue
                for link in links:
                    if str(link.get("code")) in ids:
                        line = next((o for o in orders
                                     if str(o.get("order_id")) == str(link.get("code"))), None)
                        found.setdefault(link.get("link_id"), {
                            **link, "where": "позиция",
                            "line": f"{line.get('brand') or ''} {line.get('sku') or ''}".strip()
                            if line else "",
                        })
        return list(found.values())

    def _tag_by_name(self, name: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Ищет тег по названию: сначала точно, потом по вхождению."""
        want = parts.normalize_text(name)
        rows = self.tags()
        exact = [t for t in rows if parts.normalize_text(str(t.get("name") or "")) == want]
        if len(exact) == 1:
            return exact[0], rows
        similar = [t for t in rows if want and want in parts.normalize_text(str(t.get("name") or ""))]
        if len(similar) == 1:
            return similar[0], rows
        return None, rows

    def draft_add_tag(
        self,
        *,
        name: str,
        offer_id: int | None = None,
        sku: str | None = None,
        query: str | None = None,
        order_id: int | None = None,
        hex_color: str | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК: поставить тег на сделку или на позицию в сделке.

        Тег ищется по названию среди существующих; если такого нет — он создаётся
        первым шагом (с подтверждением человека, как любая запись). Пометка позиции
        в Public API не описана, поэтому пробуем известные названия сущностей и
        подтверждаем результат перечитыванием связей.
        """
        name = (name or "").strip().strip("#")
        if not name:
            raise ValueError("Не указано название тега — например «срочно» или «под заказ»")
        if not offer_id and not order_id:
            raise ValueError(
                "Не указано, что помечаем: назовите сделку (offer_id) или позицию в ней"
            )
        target_entity: tuple[str, ...]
        code: Any
        line: dict[str, Any] | None = None
        if order_id:
            target_entity, code = TAG_ENTITIES_ORDER, int(order_id)
            target = f"позицию (order_id {int(order_id)})"
        elif sku or query:
            if not offer_id:
                raise ValueError(
                    "Чтобы пометить позицию, назовите сделку — я найду строку по артикулу "
                    "или названию"
                )
            line = self._line_for_change(int(offer_id), query=sku or query, order_id=None)
            target_entity, code = TAG_ENTITIES_ORDER, int(line["order_id"])
            target = (f"позицию «{line.get('name')}» {line.get('brand') or ''} "
                      f"{line.get('sku') or ''} в сделке {offer_id}")
        else:
            target_entity, code = TAG_ENTITIES_OFFER, int(offer_id or 0)
            target = f"сделку {offer_id}"

        tag, known = self._tag_by_name(name)
        steps: list[dict[str, Any]] = []
        tag_ref: Any
        if tag:
            tag_ref = int(tag["tag_id"])
        else:
            if not hex_color:
                hex_color = "FFB300"
            steps.append({"method": "tags/create", "payload": {"DATA": {"NAME": name, "HEX": hex_color}}})
            tag_ref = {"$from_step": 0}

        link_variants = [{"TAG_ID": tag_ref, "ENTITY": entity, "CODE": code}
                         for entity in target_entity]
        steps.append({"method": "tags/link", "payload": link_variants[0],
                      "fallbacks": [{"method": "tags/link", "payload": dict(v)}
                                    for v in link_variants[1:]]})

        summary = f"Поставить тег «{name}» на {target}"
        if not tag:
            summary += " (тега в CRM ещё нет — создам его с цветом по умолчанию)"
        else:
            summary += f" (тег уже есть: {tag.get('name')})"
        return self.drafts.create(
            tool="add_tag", method=steps[0]["method"], payload=steps[0]["payload"],
            summary=summary, steps=steps, risk="low",
            extra={"check": {"kind": "tagged", "name": name, "code": code,
                             "tag_id": None if not tag else int(tag["tag_id"]),
                             "entities": list(target_entity),
                             "offer_id": int(offer_id or 0) or None,
                             "position": line is not None or order_id is not None}},
        )

    def _verify_tagged(self, check: dict[str, Any]) -> str | None:
        """Связь «тег ↔ объект» правда появилась? None — да."""
        self._invalidate_cache()
        want_name = parts.normalize_text(str(check.get("name") or ""))
        tag_id = check.get("tag_id")
        try:
            links = self.tag_links()
        except StocrmError:
            return "Не удалось проверить результат в STOCRM. Что сделать: откройте сделку и проверьте запись; не повторяйте действие до проверки."
        found = [
            link for link in links
            if str(link.get("code")) == str(check.get("code"))
            and str(link.get("entity")) in (TAG_ENTITIES_ORDER if check.get("position") else TAG_ENTITIES_OFFER)
            and ((tag_id and _same_id(link.get("tag_id"), tag_id))
                 or (want_name and parts.normalize_text(str(link.get("name") or "")) == want_name))
        ]
        if found:
            return None
        if check.get("position"):
            return TAG_ORDER_MANUAL_HINT
        return (
            "Тег не привязался к сделке — проверьте карточку (блок с тегами справа сверху).\n"
            "Что сделать: STOCRM → сделка → «Теги» → выберите нужный. Запись не подтверждена"
        )

    def draft_remove_tag(
        self,
        *,
        name: str | None = None,
        offer_id: int | None = None,
        sku: str | None = None,
        order_id: int | None = None,
        tag_link_id: int | None = None,
    ) -> dict[str, Any]:
        """ЧЕРНОВИК: снять тег со сделки или с позиции в сделке."""
        if tag_link_id:
            links = [link for link in self.tag_links() if _same_id(link.get("link_id"), tag_link_id)]
        else:
            if not name:
                raise ValueError("Не указано, какой тег снимать — например «срочно»")
            if order_id:
                code: Any = int(order_id)
            elif sku:
                if not offer_id:
                    raise ValueError("Чтобы снять тег с позиции, назовите сделку — найду строку по артикулу")
                line = self._line_for_change(int(offer_id), query=sku, order_id=None)
                code = int(line["order_id"])
            elif offer_id:
                code = int(offer_id)
            else:
                raise ValueError(
                    "Не указано, с чего снимать тег: назовите сделку или позицию в ней"
                )
            known, _all_tags = self._tag_by_name(name)
            want = parts.normalize_text(str(known.get("name") if known else name))
            links = [
                link for link in self.tag_links(code=code)
                if parts.normalize_text(str(link.get("name") or "")) == want
            ]
        if not links:
            raise ValueError(
                f"Не нашёл такой тег на объекте — снимать нечего. "
                f"Посмотрите теги сделки: «какие теги у сделки {offer_id or ''}»".strip()
            )
        if len(links) > 1:
            listing = "; ".join(
                f"«{link.get('name')}» на {link.get('entity')} {link.get('code')}"
                f" (связь №{link.get('link_id')})" for link in links[:6]
            )
            raise ValueError(f"Подходит несколько связей: {listing}. Скажите номер связи")
        link = links[0]
        payload = {"TAG_LINK_ID": int(link["link_id"]), "TAG_ID": int(link["tag_id"])}
        steps = [{"method": "tags/unlink", "payload": payload,
                  "fallbacks": [{"method": "tags/unlink", "payload": {"TAG_ID": int(link["tag_id"])}},
                                {"method": "tags/unlink_link", "payload": dict(payload)}]}]
        summary = (f"Снять тег «{link.get('name')}» с {link.get('entity')} {link.get('code')}"
                   f" (связь №{link['link_id']})")
        # Public API умеет отвязывать только «тег целиком»: если он стоит ещё где-то,
        # честно предупреждаем человека до подтверждения, а не после.
        same_tag = [other for other in self.tag_links(tag_id=link.get("tag_id"))
                    if not _same_id(other.get("link_id"), link.get("link_id"))]
        if same_tag:
            where = "; ".join(f"{o.get('entity')} {o.get('code')}" for o in same_tag[:4])
            summary += (f"\nВнимание: этот тег стоит ещё на {len(same_tag)} объект(ах): {where}. "
                        "Если панель не умеет отвязать одну связь, тег снимется у всех — "
                        "я проверю результат и скажу.")
        return self.drafts.create(
            tool="remove_tag", method="tags/unlink", payload=payload, summary=summary,
            steps=steps, risk="low",
            extra={"check": {"kind": "untagged", "link_id": int(link["link_id"]),
                             "tag_id": link.get("tag_id"), "code": link.get("code")}},
        )

    def _verify_untagged(self, check: dict[str, Any]) -> str | None:
        """Связь правда исчезла? None — да."""
        self._invalidate_cache()
        try:
            links = self.tag_links()
        except StocrmError:
            return "Не удалось проверить результат в STOCRM. Что сделать: откройте сделку и проверьте запись; не повторяйте действие до проверки."
        if not any(_same_id(link.get("link_id"), check.get("link_id")) for link in links):
            return None
        return (
            "Снять тег через API не удалось: в Public API нет метода отвязки для такого "
            "объекта.\n"
            "Что сделать: STOCRM → карточка → в блоке тегов нажмите «×» у нужного тега.\n"
            "Ничего не изменено."
        )

    def draft_add_offer_note(self, *, offer_id: int, text: str) -> dict[str, Any]:
        raise ValueError(
            "API умеет создавать заметки, но привязка к видимому блоку сделки не подтверждена. "
            "Автоматическую запись остановил, чтобы не создавать скрытые заметки. "
            "Добавьте заметку вручную в карточке сделки; повтор старой попытки через бота не делайте."
        )

    def apply_draft(self, token: str) -> dict[str, Any]:
        from .diagnostics import TRACE_ID
        import secrets
        context_token = TRACE_ID.set(TRACE_ID.get() or "apply_" + secrets.token_hex(6))
        started = time.monotonic()
        self.audit.write("write.request", token=token)
        try:
            result = self._apply_draft(token)
            self.audit.write("write.finish", token=token, ms=round((time.monotonic()-started)*1000))
            return result
        except Exception as exc:
            self.audit.write("write.error", token=token, error=type(exc).__name__, ms=round((time.monotonic()-started)*1000))
            raise
        finally:
            TRACE_ID.reset(context_token)

    def _apply_draft(self, token: str) -> dict[str, Any]:
        # Старая кнопка от уже объединённой карточки: применяем ОБЩИЙ черновик, а не
        # отвечаем «черновик в статусе discarded» (человек нажимает ту кнопку, что видит).
        try:
            current = self.drafts.get(token)
        except KeyError:
            current = None
        if current and _forbidden_supplier_order(current.get("steps") or [{"method": current.get("method"), "payload": current.get("payload")} ]):
            raise ValueError("Черновик привязан к складской позиции или потенциальному заказу поставщику; применение запрещено. Запись не начата; старые partial не повторяйте.")
        if current and (current.get("tool") == "add_offer_note" or current.get("check", {}).get("kind") == "added_note"):
            raise ValueError("Старый черновик заметки заблокирован: её отображение в сделке не подтверждено. Проверьте карточку CRM и не создавайте повтор вслепую.")
        if current and current.get("tool") == "create_contact" and (current.get("payload") or {}).get("PROPERTIES"):
            check = current.get("check") or {}
            if check.get("kind") != "added_contact":
                raise ValueError("Старый черновик контакта не содержит проверки телефона. Отмените его, только если он не отправлялся, и подготовьте заново.")
            payload_phone = (((current.get("payload") or {}).get("PROPERTIES") or {}).get("1") or [{}])[0].get("VALUE")
            if phone_key(payload_phone) != phone_key(check.get("phone")):
                raise ValueError("Телефон черновика не совпадает с проверкой; запись остановлена")
            if current.get("status") == "pending":
                self._invalidate_cache()
                if self._booking_contacts(str(check.get("phone") or "")):
                    raise ValueError("Контакт с этим телефоном появился в CRM после подготовки. Создание остановлено.")
        if current and current.get("batch_preparing"):
            raise ValueError("Заявка ещё собирается или сборка прервана; применять отдельные позиции нельзя. Отмените незавершённый черновик через /drafts.")
        if current and current.get("queue_parent"):
            parent_job = self.drafts.get(str(current["queue_parent"]))
            if parent_job.get("status") != "applying" or parent_job.get("queue_current") != token:
                raise ValueError("Внутренняя позиция выполняется только внутри подтверждённой заявки")
        if current and current.get("tool") == "add_offer_part":
            problem = current.get("part_conflict") or self._duplicate_part_check(current)
            if problem:
                raise ValueError(str(problem))
        if current and current.get("tool") == "create_offer":
            if current.get("check", {}).get("kind") != "standalone_offer":
                raise ValueError("Старый черновик сделки не содержит проверки клиента. Не применяйте его; если запись не отправлялась, отмените и подготовьте заново. Partial проверьте в CRM.")
            return self._apply_standalone_offer(token, current)
        if current and current.get("tool") == "quick_deal":
            from .quick_deal import apply as apply_quick_deal
            return apply_quick_deal(self, token, current)
        if current and current.get("check", {}).get("kind") == "existing_booking":
            return self._apply_existing_booking(token, current)
        if current and current.get("check", {}).get("kind") == "booking":
            return self._apply_booking(token, current)
        def removed_action(value: Any) -> bool:
            if isinstance(value, list):
                return any(removed_action(v) for v in value)
            if not isinstance(value, dict):
                return False
            method = str(value.get("method") or "").strip("/").lower()
            forbidden = method.startswith("payment/") and method != "payment/get_balance"
            forbidden = forbidden or method.startswith("offer/payment/")
            forbidden = forbidden or (method.startswith(("work/", "offer/work/")) and method not in ("work/add", "work/get_filtered"))
            return bool(forbidden or any(removed_action(v) for v in value.values()))

        def price_change(value: Any) -> bool:
            if isinstance(value, list):
                return any(price_change(v) for v in value)
            if not isinstance(value, dict):
                return False
            if value.get("tool") == "change_part_price" or value.get("kind") == "order_price":
                return True
            payload = value.get("payload") or {}
            if value.get("method") in DEFAULT_ARCHIVE_METHODS and isinstance(payload, dict) and "PRICE" in payload:
                return True
            return any(price_change(v) for v in value.values())

        if current and not self.price_methods and price_change(current):
            raise ValueError("Этот черновик использует неподтверждённый метод изменения цены. Применение остановлено; поправьте существующую строку в панели, не добавляйте дубль.")
        if current and (current.get("tool") in ("add_payment", "assign_work_performer") or removed_action(current)):
            raise ValueError("Этот черновик использует удалённую функцию оплаты или назначения исполнителя услуге. Отмените его и создайте новый запрос. Ничего не записано.")
        merged_into = (current or {}).get("merged_into")
        if merged_into and current is not None and current.get("status") == "discarded":
            log.info("черновик %s объединён с %s — применяю общий", token, merged_into)
            return self.apply_draft(str(merged_into))
        if current and current.get("status") == "pending" and current.get("check", {}).get("kind") == "calendar":
            self._calendar_preflight(current["check"])
        if current and current.get("status") == "pending" and current.get("recovery_parent"):
            review = self.review_draft(str(current["recovery_parent"]))
            if not review["can_resume"]:
                raise ValueError("Позиция уже появилась в CRM; продолжение остановлено, чтобы не создать дубль. Откройте /drafts и проверьте результат")
        if current and current.get("status") == "pending" and current.get("tool") == "add_offer_part":
            checks = self._checks_of(current)
            writes = current.get('steps') or [{'method': current.get('method'), 'payload': current.get('payload')}]
            if (any(not parts.known_brand(c.get("brand")) for c in checks) or
                    any(not parts.known_brand((step.get('payload') or {}).get('BRAND')) for step in writes
                        if step.get('method') in {'offer/require/add', 'offer/order/add_free_order'})):
                raise ValueError("В старом черновике не указан бренд товара. Отмените его, если запись не отправлялась, и укажите бренд вручную. Запись не начата")
            def has_stock_reference(value: Any) -> bool:
                if isinstance(value, list):
                    return any(has_stock_reference(v) for v in value)
                if not isinstance(value, dict):
                    return False
                for key, val in value.items():
                    name = str(key).upper()
                    if name in {"STOCK_ID", "STOCK_IDS", "STOCKS_IDS", "STOCKS_IDS_SYSTEM"} and val not in (None, "", [], 0):
                        return True
                    if name == "METHOD" and val not in {"offer/require/add", "offer/order/add", "offer/order/add_free_order"}:
                        return True
                    if name == "STOCK_TYPE" and str(val).upper() != "FREE_ORDER":
                        return True
                    if has_stock_reference(val):
                        return True
                return False
            if has_stock_reference(current.get("steps") or [{"method": current.get("method"), "payload": current.get("payload")} ]):
                raise ValueError("Этот черновик привязан к складской позиции; применение запрещено. Запись не начата. Старые partial повторять нельзя")
            if (self.manual_order_method == "offer/order/add_free_order"
                    and any(s.get("method") == "offer/order/add" for s in current.get("steps") or [])
                    and ("свободной позицией" in str(current.get("summary") or "")
                         or any("свободной позицией" in str(c.get("where") or "") for c in self._checks_of(current)))):
                raise ValueError("Этот неподтверждённый черновик подготовлен старым способом. Запись не начата. Отмените только черновик без попыток записи и подготовьте новый. Partial или уже отправленные заявки не пересоздавайте")
            for step in current.get("steps") or []:
                if step.get("method") == "offer/order/add_free_order":
                    native = step.get("payload") or {}
                    if native.get("NEED_MARKUP") is not False or _money(native.get("PRICE")) <= 0 or type(native.get("AMOUNT")) is not int or native["AMOUNT"] < 1:
                        raise ValueError("Свободный ордер не содержит подтверждённые цену/количество и запрет наценки. Запись не начата; старые partial не повторяются")
                    check = current.get("check") or {}
                    if check.get("kind") == "added_line" and (
                            _money(native["PRICE"]) != _money(check.get("price"))
                            or native["AMOUNT"] != int(check.get("qty") or 1)
                            or parts.sku_exact(native.get("SKU")) != parts.sku_exact(check.get("sku"))
                            or parts.canonical_brand(native.get("BRAND")) != parts.canonical_brand(check.get("brand"))):
                        raise ValueError("Данные свободного ордера не совпадают с подтверждённой карточкой; запись не начата")
                if step.get("method") == "offer/order/add":
                    stock = ((step.get("payload") or {}).get("DATA") or {}).get("STOCK") or {}
                    if not isinstance(stock.get("UID"), str) or re.fullmatch(r"[0-9a-f]{32}", stock["UID"]) is None:
                        raise ValueError("Черновик подготовлен со старым несовместимым UID. Запись не начата. Пересоздать можно только неподтверждённый черновик без попыток записи; partial или уже применявшуюся заявку повторять нельзя")
                    warehouse = stock.get("WAREHOUSE_ID")
                    if isinstance(warehouse, bool) or not str(warehouse).isdigit() or int(str(warehouse)) <= 0:
                        raise ValueError("Старый черновик не содержит технический WAREHOUSE_ID для свободного заказа. Запись не начата. Отмените только этот неподтверждённый черновик и подготовьте заново. Черновик partial повторять нельзя")
        # Проверка и захват — одной операцией: второй «Применить» получит отказ,
        # а не вторую запись в CRM.
        draft = self.drafts.claim(token)
        started = time.monotonic()
        steps: list[dict[str, Any]] = draft.get("steps") or [
            {"method": draft["method"], "payload": draft["payload"]}
        ]
        results: list[Any] = []
        done: list[dict[str, Any]] = []
        try:
            if (draft.get("tool") == "add_offer_part" and draft.get("check", {}).get("kind") == "added_lines"
                    and len(self._checks_of(draft)) > 1):
                jobs = self._single_part_jobs(draft)
                outcome = self._apply_part_queue(token, draft, jobs, results, done, started)
            else:
                outcome = self._apply_steps(token, draft, steps, results, done, started)
            parent = draft.get("recovery_parent")
            if parent and outcome.get("verified") and not outcome.get("warning"):
                try:
                    self.confirm_draft_result(str(parent))
                except Exception as exc:  # noqa: BLE001 — не повторять уже выполненный заказ
                    log.warning("продолжение выполнено, исходный черновик не закрыт: %s", exc)
                    outcome["warning"] = "Позиция добавлена; исходный черновик требует отдельной проверки через /drafts. Повторять добавление не нужно."
            return outcome
        except BaseException as exc:
            # Любая неожиданность (Ctrl+C, ошибка в подготовке payload, сбой записи
            # журнала) раньше оставляла черновик в статусе «применяется» навсегда:
            # человек не мог ни применить его, ни отменить. Возвращаем в понятное
            # состояние: если ничего не записалось — можно нажать ещё раз.
            self._release_after_crash(token, done)
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                # такие ошибки понятны человеку и без трейсбека
                raise StocrmError(str(exc), method=draft.get("method")) from exc
            raise

    @staticmethod
    def _single_part_jobs(draft: dict[str, Any]) -> list[dict[str, Any]]:
        """Разделить подписанные шаги на независимые одиночные задания до записи."""
        steps = draft.get("steps") or []
        checks = Toolset._checks_of(draft)
        positions = draft.get("positions") or []
        if len(positions) != len(checks) or len(checks) < 2:
            raise ValueError("Не удалось безопасно разделить заявку на одиночные позиции; запись не начата")
        jobs = []
        expected = 0
        for position, check in zip(positions, checks, strict=True):
            span = position.get("steps")
            if (not isinstance(span, list) or len(span) != 2
                    or any(type(i) is not int for i in span)):
                raise ValueError("Повреждены границы позиций; запись не начата")
            start, end = span
            if start != expected or end < start or end >= len(steps):
                raise ValueError("Шаги позиций пересекаются или пропущены; запись не начата")
            local = copy.deepcopy(steps[start:end + 1])
            for index, step in enumerate(local):
                if step.get("method") not in {"offer/require/add", "offer/order/add", "offer/order/add_free_order"}:
                    raise ValueError("В очереди запчастей найдено постороннее действие; запись не начата")
                def rebase(value: Any, start: int = start, index: int = index) -> Any:
                    if isinstance(value, dict):
                        if "$from_step" in value:
                            ref = value["$from_step"]
                            if type(ref) is not int or len(value) != 1 or not start <= ref < start + index:
                                raise ValueError("Позиция ссылается на чужой или будущий шаг; запись не начата")
                            return {"$from_step": ref - start}
                        return {k: rebase(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [rebase(v) for v in value]
                    return value
                step["payload"] = rebase(step.get("payload") or {})
                # В старом объединителе запасной payload оставался локальным.
                # Его REQUIRE_ID должен быть тем же, что у основного одиночного шага.
                alternatives = [step.get("fallback")] + list(step.get("fallbacks") or [])
                for alternative in alternatives:
                    if isinstance(alternative, dict) and "REQUIRE_ID" in (alternative.get("payload") or {}):
                        alternative["payload"]["REQUIRE_ID"] = copy.deepcopy(step["payload"].get("REQUIRE_ID"))
            if check.get("kind") != "added_line":
                raise ValueError("Для одиночной позиции отсутствует проверка результата; запись не начата")
            for step in local:
                payload = step["payload"]
                row = ((payload.get("DATA") or {}).get("STOCK") or payload)
                if step["method"] == "offer/require/add" and str(payload.get("OFFER_ID")) != str(check.get("offer_id")):
                    raise ValueError("Позиция относится к другой сделке; запись не начата")
                for field, item_field in (("SKU", "sku"), ("BRAND", "brand")):
                    normalize = parts.sku_exact if field == "SKU" else parts.canonical_brand
                    if normalize(str(row.get(field) or "")) != normalize(str(check.get(item_field) or "")):
                        raise ValueError("Данные позиции не совпадают с её проверкой; запись не начата")
                if (step["method"] in {"offer/order/add", "offer/order/add_free_order"}
                        and (_money(row.get("PRICE")) != _money(check.get("price"))
                             or int(row.get("AMOUNT") or 1) != int(check.get("qty") or 1))):
                    raise ValueError("Цена/количество позиции расходятся с подтверждённой карточкой; запись не начата")
            jobs.append({"steps": local, "check": copy.deepcopy(check), "offset": start})
            expected = end + 1
        if expected != len(steps):
            raise ValueError("Вне очереди остались шаги; запись не начата")
        return jobs

    def _apply_part_queue(self, token: str, draft: dict[str, Any], jobs: list[dict[str, Any]],
                          results: list[Any], done: list[dict[str, Any]], started: float) -> dict[str, Any]:
        """Одна авторизация человека, но полный одиночный apply/readback на каждый SKU.

        Внутренние задания не показываются как новые подтверждения. Следующее
        создаётся лишь после проверки предыдущего; неизвестный результат блокирует
        родительскую заявку. Плоский result сохраняется для прежнего review/resume.
        """
        methods: list[str] = []
        for index, job in enumerate(jobs):
            steps = job["steps"]
            child: dict[str, Any] | None = None
            recorded = False
            self.audit.write("write.queue.position.start", token=token, position_index=index, sku=job["check"].get("sku"))
            try:
                child = self.drafts.create(tool="add_offer_part", method=steps[0]["method"],
                    payload=steps[0]["payload"], steps=steps,
                    summary=str(job["check"].get("sku") or "Одиночная позиция"),
                    extra={"check": job["check"], "queue_parent": token, "queue_index": index})
                self.drafts.mark(token, "applying", queue_index=index, queue_completed=index,
                                 queue_current=child["token"])
                outcome = self.apply_draft(child["token"])
                saved = self.drafts.get(child["token"])
                raw = saved.get("result")
                child_results = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
                results.extend(child_results)
                got_methods = list(outcome.get("methods") or [step["method"] for step in steps])
                methods.extend(got_methods)
                recorded = True
                done.extend({"index": job["offset"] + i, "method": method, "ok": True}
                            for i, method in enumerate(got_methods))
                if outcome.get("warning") or not outcome.get("verified"):
                    raise StocrmError(str(outcome.get("warning") or "Цена и количество позиции не подтверждены"))
                self.drafts.mark(token, "applying", queue_completed=index + 1,
                                 result=list(results), done_steps={"methods": list(methods)})
                self.audit.write("write.queue.position.verified", token=token, position_index=index,
                                 sku=job["check"].get("sku"), queue_completed=index + 1)
            except BaseException as exc:
                saved = self.drafts.get(child["token"]) if child else {}
                failure = saved.get("done_steps") or {}
                if saved.get("status") != "applied" and not recorded:
                    raw = saved.get("result")
                    results.extend(raw if isinstance(raw, list) else ([raw] if raw is not None else []))
                    methods.extend(failure.get("methods") or [])
                default_failed_index = len(steps) - 1 if recorded or saved.get("status") == "applied" else min(len(failure.get("methods") or []), len(steps) - 1)
                failed_index = job["offset"] + int(failure.get("failed_index", default_failed_index))
                failed = failure.get("failed") or steps[-1]["method"]
                self.drafts.mark(token, "partial", failed_at=time.time(), result=list(results),
                    queue_index=index, queue_completed=index,
                    done_steps={"methods": list(methods), "failed": failed,
                                "failed_index": failed_index, "error": str(failure.get("error") or exc)})
                if child and saved.get("status") not in {"applied", "partial"}:
                    self.drafts.mark(child["token"], "partial", failed_at=time.time())
                self.audit.write("write.queue.stopped", token=token, position_index=index,
                                 queue_completed=index, error=type(exc).__name__)
                if not isinstance(exc, Exception):
                    raise
                raise StocrmError(f"Позиция {index + 1} из {len(jobs)} ({job['check'].get('sku') or 'запчасть'}) не завершена: {exc}. "
                    f"Подтверждено позиций: {index}. Следующие позиции не отправлены. "
                    "Проверьте CRM через /drafts; повторное применение заявки заблокировано.") from exc
            if index + 1 < len(jobs) and self.PART_WRITE_INTERVAL > 0:
                time.sleep(self.PART_WRITE_INTERVAL)
        self.drafts.mark(token, "applied", applied_at=time.time(), result=list(results),
                         queue_completed=len(jobs), queue_current=None)
        self.audit.write("write.applied", token=token, tool=draft["tool"], method=methods[0],
                         methods=methods, steps=draft["steps"], result=results,
                         queue_completed=len(jobs), ms=round((time.monotonic() - started) * 1000))
        return {"status": "applied", "token": token, "method": methods[0], "methods": methods,
                "result": results, "verified": True, "queue_completed": len(jobs)}

    def _call_step(self, step: dict[str, Any], payload: Any,
                   results: list[Any] | None = None) -> Any:
        """Выполняет выбранный метод без альтернативной записи после ошибки.

        add_free_order основной для manual; фактическое количество проверяется чтением.

        Для старых операций редактирования сохраняются совместимые альтернативы;
        потеря ответа никогда не является основанием для альтернативной записи.
        """
        if _forbidden_supplier_order({**step, "payload": payload}):
            raise ValueError("Заказ поставщику или небезопасная позиция запрещены; запись не выполнялась")
        if step["method"] == "contact/create":
            self._last_step_method = step["method"]
            return self.client.call(step["method"], payload)
        if step["method"] == "offer/order/add_free_order":
            self._last_step_method = step["method"]
            return self.client.call(step["method"], payload)
        if step["method"] in ("calendar/card/new", "calendar/card/edit"):
            self._last_step_method = step["method"]
            return self.client.call(step["method"], payload)
        candidates: list[dict[str, Any]] = [{"method": step["method"], "payload": payload}]
        fallback = step.get("fallback")
        if isinstance(fallback, dict) and fallback.get("method"):
            candidates.append(fallback)
        for extra in step.get("fallbacks") or []:
            if isinstance(extra, dict) and extra.get("method"):
                candidates.append(extra)

        first: StocrmError | None = None
        self._last_step_method = None
        for index, candidate in enumerate(candidates):
            method = candidate["method"]
            try:
                result = self._call_with_known_fields(
                    method, _resolve_refs(candidate.get("payload") or payload, results or []))
            except StocrmError as exc:
                # Отказ неизвестного метода допускает совместимый fallback.
                # Потеря ответа, квота или неопределённый 5xx — НЕ повод
                # отправлять то же изменение под другим именем.
                if lock_refusal(exc):
                    raise
                if not _no_such_method(exc) and (exc.code is None or exc.code in (401, 403, 429) or exc.code >= 500):
                    raise
                first = first or exc
                if index == 0:
                    log.warning("шаг %s не выполнился (%s) — пробую запасной метод",
                                method, exc)
                if method in self.price_methods and _no_such_method(exc):
                    self._remember_price_method(method, ok=False)
                continue
            self._last_step_method = method
            if index > 0:
                log.warning("шаг выполнен запасным методом %s", method)
            if method in self.price_methods:
                self._remember_price_method(method, ok=True)
                if self.price_methods and self.price_methods[0] != method:
                    # панель приняла именно этот метод — впредь начинаем с него
                    self.price_methods = (method,) + tuple(
                        m for m in self.price_methods if m != method)
                    self.price_method = method
            return result
        assert first is not None
        raise first

    def _call_with_known_fields(self, method: str, payload: Any) -> Any:
        """Вызов метода с «понижением»: убирает необязательные поля, если панель их не знает.

        Снимается только необязательное поле, явно отвергнутое панелью.
        Упоминание поля во внутренней ошибке, пустой ответ и обрыв не дают права
        повторять запись. FIX_USER_PRICE в публичной документации показан в ответе;
        поддержку на входе не считаем доказанной. Фактическую цену перечитываем.
        """
        cleaned = copy.deepcopy(payload)
        # Панель может не знать какое-то поле: тогда она называет его в ошибке.
        # Для недокументированных методов (оплата, правка цены) это единственный способ
        # подобрать рабочий набор полей — поэтому убираем названное поле и пробуем снова.
        for _attempt in range(5):
            try:
                return self.client.call(method, cleaned)
            except StocrmError as exc:
                if _is_transport_error(exc) or exc.code is None or exc.code in (401, 403, 429):
                    raise
                text = str(exc).upper()
                # Одно упоминание поля во внутренней ошибке не доказывает отказ до записи.
                explicit_field_refusal = re.search(
                    r"(?:UNKNOWN|UNRECOGNIZED|UNSUPPORTED)\s+(?:FIELD|PARAMETER)|"
                    r"ПОЛ[ЕЯ]\s+\w+.{0,30}(?:НЕТ В ИСХОДНЫХ ДАННЫХ|НЕ (?:ПОДДЕРЖИВАЕТСЯ|НАЙДЕНО))|"
                    r"НЕИЗВЕСТН(?:ОЕ|ЫЙ)\s+(?:ПОЛЕ|ПАРАМЕТР)", text)
                if not explicit_field_refusal:
                    raise
                dropped = self._drop_named_field(cleaned, text)
                if dropped is None:
                    raise
                log.warning("панель не приняла поле %s в %s (%s) — повторяю без него",
                            dropped, method, exc)
        return self.client.call(method, cleaned)

    #: Поля, которые нельзя выбрасывать «на всякий случай»: без них запись теряет смысл.
    _KEEP_FIELDS = frozenset({
        "ENTITY_ID", "ENTITY_TYPE", "SID", "DATA", "PRICE", "AMOUNT", "CURRENCY_VALUE",
        "ORDER_ID", "SUPPLIER_ID", "NAME", "SKU", "BRAND", "REQUIRE_ID", "OFFER_ID",
        "WAREHOUSE_ID", "STOCK_TYPE", "UID", "CURRENT_VOLUME", "PRICE_SUPP_HUM",
    })

    @classmethod
    def _drop_named_field(cls, payload: Any, error_text: str) -> str | None:
        """Убирает из payload поле, которое панель назвала в ошибке. None — убирать нечего."""
        if not isinstance(payload, dict):
            return None
        data = payload.get("DATA")
        stock = data.get("STOCK") if isinstance(data, dict) else None
        for scope in (stock, payload):
            if not isinstance(scope, dict):
                continue
            for key in list(scope):
                if key.upper() in cls._KEEP_FIELDS:
                    continue
                if key.upper() in error_text:
                    scope.pop(key, None)
                    return key
        return None

    def _read_back_uncertain_order(self, draft: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        """Только чтение. Старое совпадение по SKU не доказывает новую запись."""
        check = draft.get("check") or {}
        stock = (payload.get("DATA") or {}).get("STOCK") or {}
        uid = stock.get("UID")
        if not uid or check.get("kind") != "added_line":
            return None
        try:
            self._invalidate_cache()
            with read_budget(8):
                rows = self.offer_parts(int(check["offer_id"]), limit=200)
            matches = [r for r in rows
                       if str(r.get("offer_id")) == str(check["offer_id"])
                       and str(r.get("uid")) == str(uid)
                       and parts.sku_exact(str(r.get("sku") or "")) == parts.sku_exact(str(stock.get("SKU") or ""))
                       and parts.brand_match(stock.get("BRAND"), r.get("brand")) == 100
                       and str(r.get("active") or "Y").upper() not in {"N", "FALSE", "0"}
                       and r.get("order_id")]
            if len(matches) != 1:
                return None
            row = matches[0]
            if (_money(row.get("price")) != _money(stock.get("PRICE"))
                    or _money(row.get("qty")) != int(stock.get("AMOUNT") or 1)):
                return None
            return {"ORDER_ID": row["order_id"], "READBACK_CONFIRMED": True}
        except Exception:
            # Не заменять исходный неизвестный результат ошибкой диагностического чтения.
            return None

    def _check_added_line(self, check: dict[str, Any]) -> dict[str, Any]:
        """Что реально лежит в сделке после записи: строка, цена, количество, расхождения."""
        offer_id = int(check.get("offer_id") or 0)
        wanted_price = _money(check.get("price"))
        wanted_qty = int(check.get("qty") or 1)
        self._invalidate_cache()
        try:
            rows = self.offer_parts(offer_id, limit=200)
        except StocrmError as exc:
            return {"error": f"не удалось перечитать сделку для проверки: {exc}"}
        sku_key = parts.sku_exact(str(check.get("sku") or ""))
        name_key = parts.normalize_text(str(check.get("name") or ""))
        brand = str(check.get("brand") or "")
        order_id = check.get("order_id")
        matches = [
            row for row in rows
            if (str(row.get("active") or "Y").upper() not in {"N", "FALSE", "0"})
            and str(row.get("offer_id")) == str(offer_id)
            and (str(row.get("order_id")) == str(order_id) if order_id is not None else True)
            and (parts.sku_exact(str(row.get("sku") or "")) == sku_key if sku_key
                 else bool(name_key) and parts.normalize_text(str(row.get("name") or "")) == name_key)
            and (not brand or parts.brand_match(brand, row.get("brand")) == 100)
        ]
        if not matches:
            return {"error": (f"нужной позиции в сделке {offer_id} не видно — проверьте её в панели; "
                              "совпадение только по названию или с другим брендом не подтверждает запись")}
        if len(matches) != 1:
            return {"error": "Найдено несколько одинаковых позиций; однозначно подтвердить записанную строку нельзя"}
        line = matches[0]
        actual_price = _money(line.get("price"))
        # QUANTITY из списка товаров документирован как «количество», но в
        # неоднозначное значение может относиться к складским данным.
        # При расхождении без явного QUANTITY_CURRENT не выдаём догадку за факт.
        raw_qty = line.get("qty")
        try:
            quantity = float(raw_qty) if raw_qty is not None and not isinstance(raw_qty, bool) else float("nan")
        except (TypeError, ValueError):
            quantity = float("nan")
        valid_qty = math.isfinite(quantity) and quantity > 0 and quantity.is_integer()
        actual_qty = int(quantity) if valid_qty else None
        problems: list[str] = []
        if wanted_price and abs(actual_price - wanted_price) > 0.01:
            problems.append(f"цена в сделке {actual_price:.0f} ₽ вместо {wanted_price:.0f} ₽")
        if actual_qty != wanted_qty:
            if line.get("qty_source") == "QUANTITY_CURRENT" and actual_qty is not None:
                problems.append(f"количество в строке {actual_qty} вместо {wanted_qty}")
            else:
                problems.append("не удалось надёжно проверить количество в строке сделки")
        return {
            "order_id": line.get("order_id"),
            "price": actual_price,
            "qty": actual_qty,
            "wanted_price": wanted_price,
            "wanted_qty": wanted_qty,
            "problems": problems,
        }

    def _verify_added_lines(self, items: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
        """Проверяет пачкой все позиции одного черновика. (замечание, подробности)."""
        problems: list[str] = []
        details: list[dict[str, Any]] = []
        for check in items or []:
            info = self._check_added_line(check)
            details.append({"sku": check.get("sku"), "name": check.get("name"), **info})
            if info.get("error"):
                problems.append(f"«{check.get('sku') or check.get('name')}»: {info['error']}")
            elif info.get("problems"):
                problems.append(
                    f"«{check.get('sku') or check.get('name')}»: " + ", ".join(info["problems"]))
        if not problems:
            return None, details
        return (
            "Результат записи требует проверки — " + "; ".join(problems) +
            ". Проверьте эти строки в карточке сделки; не повторяйте заявку вслепую."
        ), details

    def _verify_added_line(self, check: dict[str, Any]) -> str | None:
        """Проверяет по панели, что позиция легла в сделку с нашей ценой и количеством.

        Не делаем вывод об остатке склада по неоднозначному полю количества.
        """
        info = self._check_added_line(check)
        if info.get("error"):
            return str(info["error"])
        if not info.get("problems"):
            return None
        return ("Результат записи требует проверки: " + ", ".join(info["problems"]) +
                ". Проверьте строку в карточке сделки; не повторяйте добавление вслепую")

    def _draft_already_done(self, draft: dict[str, Any]) -> bool:
        """Проверка «дело уже сделано» для необязательных шагов черновика."""
        check = draft.get("check") or {}
        if check.get("kind") == "removed_line":
            return self._verify_removed_line(check) is None
        return False

    def _release_after_crash(self, token: str, done: list[dict[str, Any]]) -> None:
        try:
            current = self.drafts.get(token).get("status")
            if current in ("partial", "applied", "expired", "discarded"):
                return                   # шаг уже сам объяснил, что произошло
            if any(d.get("ok") for d in done):
                self.drafts.mark(token, "partial",
                                 failed_at=time.time(),
                                 done_steps={"methods": [d["method"] for d in done if d.get("ok")],
                                             "error": "применение прервано"})
            else:
                self.drafts.release(token, reason="применение прервано, ничего не записано")
        except Exception as exc:          # noqa: BLE001 — уже в аварийном сценарии
            log.error("не удалось вернуть черновик %s в рабочее состояние: %s", token, exc)

    def _verify_after_break(self, draft: dict[str, Any]) -> dict[str, Any] | None:
        """Связь оборвалась во время записи: спрашиваем у панели, записалось ли.

        None — изменений в сделке нет. Иначе — данные проверки, и черновик считается
        выполненным: повторно применять его нельзя.
        """
        check = draft.get("check") or {}
        kind = check.get("kind")
        if kind == "calendar":
            # Создание без полученного CARD_ID нельзя подтвердить по одному совпадению времени.
            if self._verify_calendar(check):
                return None
            return {"kind": "calendar", "verified": True}
        if kind in ("added_note", "updated_note", "tagged", "untagged"):
            verifier = {
                "added_note": self._verify_added_note,
                "updated_note": self._verify_updated_note,
                "tagged": self._verify_tagged,
                "untagged": self._verify_untagged,
            }[str(kind)]
            if verifier(check):
                return None                    # в панели ничего не изменилось
            return {"kind": kind, "verified": True}
        if kind == "added_work":
            try:
                resolved = _resolve_refs(check, [])
            except ValueError:
                resolved = dict(check)
            if self._verify_work_exists(resolved):
                return None                    # работы в сделке не видно — считать записанным нельзя
            return {"kind": "added_work", "verified": True}
        if kind == "added_lines":
            found = [c for c in (check.get("items") or [])
                     if not self._check_added_line(c).get("error")]
            if not found:
                return None                    # ни одной позиции в сделке не видно
            return {"kind": "added_lines", "verified": len(found), "of": len(check.get("items") or [])}
        if kind == "added_line":
            info = self._check_added_line(check)
            if info.get("error"):
                return None                    # позиции в сделке не видно
            return {"kind": "added_line", **info}
        if kind == "removed_line":
            if self._verify_removed_line(check):
                return None                    # строка на месте — удаление не прошло
            return {"kind": "removed_line", "verified": True}
        if kind == "order_price":
            if self._verify_price_change(check):
                return None                    # цена прежняя — изменения нет
            return {"kind": "order_price", "verified": True}
        return None

    PART_WRITE_INTERVAL = 0.25

    def _apply_steps(self, token: str, draft: dict[str, Any], steps: list[dict[str, Any]],
                     results: list[Any], done: list[dict[str, Any]], started: float) -> dict[str, Any]:
        """Выполняет шаги черновика по порядку (после того, как черновик захвачен)."""
        used: list[str] = []
        last_part_write: float | None = None
        for index, step in enumerate(steps):
            if step.get("skip_if_applied") and self._draft_already_done(draft):
                # Первый шаг уже сделал дело (строку удалили) — «пометить неактивной»
                # больше не нужно: иначе панель ответит ошибкой «строки нет» и человек
                # увидит «шаг 2 из 2 не выполнен» при полностью успешном удалении.
                continue
            payload = _resolve_refs(step.get("payload") or {}, results)
            try:
                part_write = step["method"] in {"offer/require/add", "offer/order/add", "offer/order/add_free_order"}
                if part_write and last_part_write is not None:
                    remaining = self.PART_WRITE_INTERVAL - (time.monotonic() - last_part_write)
                    if remaining > 0:
                        time.sleep(remaining)
                self.audit.write("write.step.start", token=token, method=step["method"], step_index=index)
                step_started = time.monotonic()
                try:
                    results.append(self._call_step(step, payload, results))
                finally:
                    # Even a timeout may have changed CRM; no stale read can survive it.
                    self._invalidate_cache()
                if part_write:
                    last_part_write = time.monotonic()
                self.audit.write("write.step.finish", token=token, method=self._last_step_method or step["method"], step_index=index, ms=round((time.monotonic()-step_started)*1000))
                # Человеку показываем метод, который РЕАЛЬНО сработал, а не первый
                # из списка кандидатов: иначе в отчёте «→ внесение оплаты» врёт.
                used.append(self._last_step_method or step["method"])
            except Exception as exc:
                if (step["method"] == "offer/order/add" and draft.get("tool") == "add_offer_part"
                        and (_is_transport_error(exc) or isinstance(exc, StocrmError) and exc.code is None)):
                    confirmed = self._read_back_uncertain_order(draft, payload)
                    self.audit.write("write.uncertain.readback", token=token, method=step["method"], verified=bool(confirmed))
                    if confirmed:
                        results.append(confirmed)
                        used.append(step["method"])
                        done.append({"index": index, "method": step["method"], "payload": payload, "ok": True})
                        last_part_write = time.monotonic()
                        self.audit.write("write.confirmed_by_uid", token=token, method=step["method"], step_index=index)
                        continue
                done.append({"index": index, "method": step["method"], "payload": payload, "ok": False,
                             "error": str(exc)})
                if step.get("optional"):
                    # Необязательный шаг (например «пометить строку неактивной» после
                    # удаления): его отказ не должен превращать черновик в «частично
                    # выполненный» — итог всё равно проверяется перечитыванием сделки.
                    log.info("необязательный шаг %s не выполнился: %s", step["method"], exc)
                    continue
                nothing_done = not any(d["ok"] for d in done)
                # Повторить можно только если панель ЯВНО отказала (ответила с кодом):
                # при таймауте неизвестно, записалось ли действие, и повтор дал бы дубликат.
                refused = isinstance(exc, StocrmError) and (bool(exc.code) or lock_refusal(exc))
                if nothing_done and step["method"] == "contact/create" and (not isinstance(exc, StocrmError) or exc.code is None or exc.code in (429,) or exc.code >= 500):
                    self.drafts.mark(token, "partial", failed_at=time.time(), done_steps={"failed": "contact/create", "error": str(exc)})
                    raise StocrmError("Результат создания контакта неизвестен. Проверьте CRM и /drafts; повтор заблокирован.") from exc
                if nothing_done and not refused and _is_transport_error(exc):
                    # Ответа нет: панель могла успеть записать. Спрашиваем её, а не гадаем.
                    if draft.get("tool") == "add_offer_part":
                        self.drafts.mark(token, "partial", failed_at=time.time(), result=list(results),
                            done_steps={"methods": [d["method"] for d in done if d.get("ok")],
                                        "failed": step["method"], "failed_index": index, "error": str(exc)})
                        raise StocrmError("Связь с STOCRM оборвалась при записи. Результат неизвестен; старое совпадение в сделке не подтверждает новое добавление. Откройте /drafts → «Проверить CRM». Повторное «Применить» запрещено.") from exc
                    landed = self._verify_after_break(draft)
                    if landed:
                        note = (
                            "Связь с STOCRM оборвалась во время записи, но изменение в панели "
                            "УЖЕ ЕСТЬ — я перечитал сделку и подтверждаю. Повторно применять "
                            "не нужно."
                        )
                        self.drafts.mark(token, "applied", applied_at=time.time(),
                                         result=landed, check_warning=note)
                        self.audit.write("write.applied_after_break", tool=draft["tool"],
                                         method=step["method"], payload=payload,
                                         step_index=index, token=token)
                        return {
                            "status": "applied",
                            "token": token,
                            "method": step["method"],
                            "methods": [step["method"]],
                            "result": landed,
                            "warning": note,
                        }
                    if draft.get("check", {}).get("kind") == "calendar":
                        self.drafts.mark(token, "partial", failed_at=time.time(),
                                         done_steps={"error": "Результат календаря неизвестен после обрыва связи"})
                        raise StocrmError("Результат записи в календарь неизвестен. Проверьте STOCRM вручную; повторное применение заблокировано, чтобы не создать дубль.") from exc
                    # Изменений в сделке нет — черновик освобождаем, «Применить» можно снова.
                    self.drafts.release(token, reason="связь прервалась, запись не подтверждена")
                    self.audit.write("write.broken", tool=draft["tool"], method=step["method"],
                                     payload=payload, error=str(exc), step_index=index,
                                     token=token)
                    raise StocrmError(
                        _human_break_error(step["method"], draft.get("tool", "")),
                        method=step["method"],
                    ) from exc
                if nothing_done and refused:
                    # Ни один шаг не выполнился — в STOCRM ничего не изменилось.
                    # Черновик не «сжигаем»: после снятия блокировки человек спокойно
                    # нажмёт «Применить» ещё раз, и дубликатов быть не может.
                    self.drafts.release(token, reason=str(exc))
                    self.audit.write(
                        "write.rejected", tool=draft["tool"], method=step["method"],
                        payload=payload, error=str(exc), step_index=index, token=token,
                    )
                    raise StocrmError(
                        _human_write_error(exc, step["method"], draft.get("tool", "")),
                        method=step["method"],
                    ) from exc
                # Часть изменений уже применена. Помечаем черновик «partial»,
                # чтобы повторное нажатие «Применить» не создало дубликаты.
                self.drafts.mark(
                    token, "partial",
                    failed_at=time.time(),
                    done_steps={"methods": [d["method"] for d in done if d["ok"]],
                                "failed": step["method"], "error": str(exc), "failed_index": index},
                    result=results,
                )
                self.audit.write(
                    "write.partial", tool=draft["tool"], method=step["method"],
                    methods=[d["method"] for d in done], payload=payload, error=str(exc),
                    step_index=index, token=token,
                )
                applied_note = (
                    f"Уже выполнено: {', '.join(d['method'] for d in done if d['ok'])}. "
                    if any(d["ok"] for d in done) else ""
                )
                raise StocrmError(
                    f"Шаг {index + 1} из {len(steps)} ({step['method']}) не выполнен: {exc}. "
                    f"{applied_note}Проверьте результат в STOCRM — повторное применение "
                    "этого черновика заблокировано, чтобы не создать дубликаты",
                    method=step["method"],
                ) from exc
            done.append({"index": index, "method": step["method"], "payload": payload, "ok": True})
            if draft.get("queue_parent"):
                self.drafts.mark(token, "applying", result=list(results),
                                 done_steps={"methods": list(used)})

        result = results[-1] if len(results) == 1 else results
        methods = used or [s["method"] for s in steps]
        self._invalidate_cache()        # данные в панели изменились

        # Если действие нужно проверить по факту (например, изменилась ли цена) —
        # перечитываем сделку. Панель может ответить «ok», но ничего не применить:
        # тогда человек должен узнать правду, а не думать, что цена уже поменялась.
        check = dict(draft.get("check") or {})
        if check.get("kind") == "added_contact":
            try:
                ident = int(result) if isinstance(result, (str, int)) and not isinstance(result, bool) else int(_named_value(result, "CONTACT_ID"))
                matches = self._booking_contacts(str(check["phone"]), ident) if ident > 0 else []
                data = (draft.get("payload") or {}).get("DATA") or {}
                expected_names = {name_key(" ".join(str(data.get(k) or "") for k in order)) for order in
                                  (("SURNAME", "FIRSTNAME", "MIDDLENAME"), ("FIRSTNAME", "SURNAME", "MIDDLENAME"))}
                if not any(str(c["contact_id"]) == str(ident) and name_key(c.get("title")) in expected_names and name_key(c.get("title")) for c in matches):
                    raise ValueError("контакт, имя и телефон не подтверждены")
            except Exception:  # noqa: BLE001 — даже успешный ответ без проверки не означает успех
                contact_problem = "Контакт, имя или телефон не удалось подтвердить в CRM. Проверьте вручную; повторное создание заблокировано."
                self.drafts.mark(token, "partial", failed_at=time.time(), result=result, check_warning=contact_problem)
                return {"status": "partial", "token": token, "method": methods[0], "methods": methods,
                        "result": result, "warning": contact_problem, "verified": False}
            result = {"CONTACT_ID": ident}
        if check.get("kind") == "added_line":
            returned_order = _named_value(result, "ORDER_ID")
            if returned_order is not None and not isinstance(returned_order, bool) and str(returned_order).isdigit():
                check["order_id"] = int(str(returned_order))
        if check.get("kind") == "calendar":
            calendar_result = {"CARD_ID": check["expected"].get("CARD_ID") or _named_value(result, "CARD_ID")}
            problem = self._verify_calendar(check, calendar_result)
            expected = check["expected"]
            result = {**calendar_result, "OFFER_ID": expected["OFFER_ID"],
                      "POST_ID": expected["POST_ID"], "SUMMARY": draft["summary"]}
            if problem:
                self.drafts.mark(token, "partial", failed_at=time.time(), result=result,
                                 check_warning=problem)
                self.audit.write("write.unverified", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                return {"status": "partial", "token": token, "method": methods[0],
                        "methods": methods, "result": result, "warning": problem, "verified": False}
        if check.get("kind") == "added_note":
            note_id = _extract_id(result)
            if isinstance(note_id, int) and not isinstance(note_id, bool):
                check = {**check, "note_id": note_id}
        simple_checks = {
            "added_note": self._verify_added_note,
            "updated_note": self._verify_updated_note,
            "tagged": self._verify_tagged,
            "untagged": self._verify_untagged,
        }
        if check.get("kind") in simple_checks:
            problem = simple_checks[str(check["kind"])](check)
            if check.get("kind") in ("added_note", "updated_note"):
                result = {"OFFER_ID": check.get("offer_id"), "NOTE_ID": check.get("note_id"),
                          "TEXT": check.get("text"), "API_RESPONSE": result}
            if problem:
                self.audit.write("write.unexpected", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                self.drafts.mark(token, "partial" if "offer/order/add_free_order" in methods else "applied", applied_at=time.time(), result=result,
                                 check_warning=problem)
                return {
                    "status": "partial" if "offer/order/add_free_order" in methods else "applied",
                    "verified": False,
                    "token": token,
                    "method": methods[0],
                    "methods": methods,
                    "result": result,
                    "warning": problem,
                }
        if check.get("kind") == "added_work":
            resolved = _resolve_refs(check, results)
            created_work_id = _named_value(result, "WORK_ID")
            if created_work_id:
                resolved["work_id"] = created_work_id
            if isinstance(result, dict):
                # чтобы в отчёте были видны сделка, номер работы и сумма, а не только WORK_ID
                result.setdefault("OFFER_ID", resolved.get("offer_id"))
                if resolved.get("title"):
                    result.setdefault("TITLE", resolved["title"])
            # Сначала сумма: если панель записала другую, человек должен узнать об этом.
            problem = self._verify_work_sum(resolved)
            if isinstance(result, dict) and resolved.get("actual_sum") is not None:
                result["SUM"] = resolved["actual_sum"]
                if resolved.get("rounding_note"):
                    result["ROUNDING_NOTE"] = resolved["rounding_note"]
            if problem:
                self.audit.write("write.unexpected", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                self.drafts.mark(token, "partial" if "offer/order/add_free_order" in methods else "applied", applied_at=time.time(), result=result,
                                 check_warning=problem)
                return {
                    "status": "partial" if "offer/order/add_free_order" in methods else "applied",
                    "verified": False,
                    "token": token,
                    "method": methods[0],
                    "methods": methods,
                    "result": result,
                    "warning": problem,
                }
        if check.get("kind") == "added_lines":
            problem, _details = self._verify_added_lines(list(check.get("items") or []))
            if problem:
                self.audit.write("write.unexpected", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                self.drafts.mark(token, "partial", applied_at=time.time(), result=result,
                                 check_warning=problem)
                return {
                    "status": "partial",
                    "verified": False,
                    "token": token,
                    "method": methods[0],
                    "methods": methods,
                    "result": result,
                    "warning": problem,
                }
        if check.get("kind") == "added_line":
            info = (self._check_added_line(check) if check.get("order_id") else
                    {"error": "CRM не вернула номер созданной строки: нельзя отличить её от старых позиций. Проверьте сделку, не повторяйте заявку."})
            problem = None
            if info.get("error"):
                problem = str(info["error"])
            elif info.get("problems"):
                problem = ("Результат записи требует проверки: " + ", ".join(info["problems"]) +
                           ". Проверьте строку в карточке сделки; не повторяйте заявку вслепую.")
            if problem:
                self.audit.write("write.unexpected", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                self.drafts.mark(token, "partial", applied_at=time.time(), result=result,
                                 check_warning=problem)
                return {
                    "status": "partial",
                    "verified": False,
                    "token": token,
                    "method": methods[0],
                    "methods": methods,
                    "result": result,
                    "warning": problem,
                }
        if check.get("kind") == "removed_line":
            problem = self._verify_removed_line(check)
            if problem:
                self.audit.write("write.unexpected", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                self.drafts.mark(token, "applied", applied_at=time.time(), result=result,
                                 check_warning=problem)
                return {
                    "status": "applied",
                    "token": token,
                    "method": methods[0],
                    "methods": methods,
                    "result": result,
                    "warning": problem,
                }
        if check.get("kind") == "order_price":
            problem = self._verify_price_change(check)
            if problem:
                self.drafts.mark(token, "partial", failed_at=time.time(),
                                 done_steps={"methods": methods, "error": problem},
                                 result=result)
                self.audit.write("write.unverified", tool=draft["tool"], method=methods[0],
                                 payload=draft["payload"], error=problem, token=token)
                raise StocrmError(problem, method=methods[0])
        self.drafts.mark(token, "applied", applied_at=time.time(), result=result)
        self.audit.write(
            "write.applied", tool=draft["tool"], method=methods[0],
            methods=methods, payload=draft["payload"], steps=steps, result=result,
            ms=round((time.monotonic() - started) * 1000), token=token,
        )
        return {
            "status": "applied",
            "token": token,
            "method": methods[0],
            "methods": methods,
            "result": result,
            "verified": bool(check.get("kind")),
        }


__all__ = ["Toolset", "date_range", "to_ts"]
