"""Всё, что бот говорит человеку, — по-русски и без техники.

Зачем отдельный модуль: сообщения собирались в разных местах, и в чат попадали
внутренние подробности — имена методов панели (`offer/require/add`), ответы API
в виде словаря, тексты исключений (`HTTP 500 для ...`, `KeyError: 'CONTACT_ID'`),
иногда целые трейсбеки. Читать это менеджеру невозможно.

Правила, которые здесь закреплены:
  * ошибка всегда объясняет **что случилось** и **что сделать**;
  * названия методов панели заменяются человеческими фразами;
  * JSON, трейсбеки, английские служебные слова в ответ не попадают;
  * длина сообщения укладывается в лимит Телеграма.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .client import StocrmError

#: Человеческие названия действий (совпадают с report.METHOD_TITLES по смыслу).
METHOD_TITLES: dict[str, str] = {
    "existing_booking": "Существующая сделка записана в календарь, выбранный статус проверен",
    "booking": "Клиент записан: контакт, новая сделка и календарь",
    "calendar/card/new": "создание записи календаря",
    "calendar/card/edit": "изменение записи календаря",
    "calendar/card/get": "просмотр календаря",
    "calendar/post/get": "список постов календаря",
    "calendar/post/get_filtered": "чтение записи календаря",
    "customers/get_filtered": "список подразделений",

    "offer/require/add": "добавление позиции в сделку",
    "offer/order/add": "добавление позиции в сделку",
    "offer/order/add_free_order": "добавление позиции в сделку",
    "offer/order/update": "изменение позиции в сделке",
    "offer/order/edit": "изменение позиции в сделке",
    "offer/order/change": "изменение позиции в сделке",
    "offer/order/update_price": "изменение цены позиции",
    "offer/order/delete": "удаление позиции из сделки",
    "offer/order/remove": "удаление позиции из сделки",
    "offer/update_field": "изменение данных сделки",
    "offer/status/change": "смена статуса сделки",
    "work/add": "добавление работы в сделку",
    "task/create": "создание задачи",
    "task/mark_member_as_responsible": "назначение исполнителя задачи",
    "note/create": "добавление заметки",
    "contact/create": "создание клиента",
    "contact/update": "изменение клиента",
    "car/edit": "изменение автомобиля",
    "car/add": "добавление автомобиля",
    "products/get_filtered_orders": "чтение товаров сделки",
    "wms/get_filtered_stocks": "чтение склада",
    "wms/product_cards/get_filtered": "чтение номенклатуры",
    "offers/get_from_filter": "чтение списка сделок",
    "car_profile/get_filtered_profiles": "чтение автомобилей",
    "payment/get_balance": "чтение баланса сделки",
}

#: Что делать человеку при типовой проблеме.
_ADVICE: list[tuple[tuple[str, ...], str]] = [
    (("сетевая ошибка", "connection aborted", "connectionreseterror", "оборвалась",
      "remote end closed", "remotedisconnected"),
     ("Связь с STOCRM оборвалась, ответ от панели не дошёл. Если это была запись — "
      "сначала проверьте сделку в STOCRM и /drafts; не повторяйте запись, пока результат неизвестен.")),
    (("заблокирован", "blocked", "locked"),
     ("Карточку прямо сейчас держит открытой другой сотрудник — попросите закрыть её "
     "(или закройте свою вкладку в браузере) и повторите.")),
    (("авториз", "301", "redirect", "sid", "ключ"),
     ("Проверьте ключ доступа STOCRM (Настройки → API ключи) и домен в файле .env, "
     "затем перезапустите бота.")),
    (("403", "доступ", "прав", "forbidden", "permission"),
     ("У ключа нет прав на это действие. Попросите администратора STOCRM выдать доступ "
     "или выполните действие в самой панели.")),
    (("404", "не найден", "not found", "не знает метод"),
     ("Такого в панели нет — уточните номер/название и повторите. Если это действие "
     "недоступно через API, сделайте его в карточке STOCRM вручную.")),
    (("таймаут", "timeout", "не ответила вовремя", "timed out"),
     ("Панель не ответила за отведённое время. Подождите минуту и повторите; "
     "если действие меняло данные — сначала проверьте результат в STOCRM.")),
    (("сетев", "нет связи", "proxy", "прокси", "dns"),
     ("Связь с STOCRM прервалась на середине. Повторите просьбу — бот проверит по "
      "панели, записалось ли действие, и не создаст дубликат.")),
    (("сервер", "500", "502", "503", "504"),
     "Похоже, проблема на стороне STOCRM. Повторите через минуту."),
    (("не указан", "не заполнен", "уточн", "нужен", "нужно", "нужна", "не хватает"),
     "Уточните недостающие данные одним сообщением — и я повторю."),
]

_JSON_START = re.compile(r"^\s*[\[{]", re.MULTILINE)
#: Разметка Markdown: бот пишет в чат обычным текстом (parse_mode=None), поэтому
#: «**жирный**» человек видит вместе со звёздочками — их надо убрать.
_MARKDOWN = re.compile(r"(\*\*|__|~~|`)")
#: Целиком JSON-объект/массив (модель иногда отвечает дампом ответа инструмента).
_JSON_WHOLE = re.compile(r"^\s*[\[{].*[\]}]\s*$", re.DOTALL)
_METHOD_TOKEN = re.compile(r"\b([a-z_]+(?:/[a-z_]+){1,3})\b")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)
_TECH_WORDS = re.compile(
    r"\b(traceback|payload|json|tool_call|tool_id|kwargs|NoneType|KeyError|ValueError|"
    r"TypeError|AttributeError|Exception|RuntimeError|Stack trace)\b",
    re.IGNORECASE,
)


def pretty_method(method: str | None) -> str:
    """`offer/require/add` → «добавление позиции в сделку»."""
    text = str(method or "").strip()
    if not text:
        return "изменение"
    return METHOD_TITLES.get(text, "изменение в STOCRM")


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        index = cut.rfind(sep)
        if index > 60:
            return cut[: index + 1].rstrip()
    return cut.rstrip() + "…"


def _http_code(text: str) -> int | None:
    match = re.search(r"\bHTTP\s*(\d{3})\b|\b(4\d\d|5\d\d)\b", text)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _describe_by_type(error: Any, method: str) -> str:
    """Понятное описание по типу ошибки — без «HTTP 403 для offer/...»."""
    if isinstance(error, KeyError):
        field = str(error).strip("'\"")
        return f"в ответе STOCRM не оказалось нужного поля ({field})"
    if isinstance(error, (ValueError, TypeError)):
        text = " ".join(str(error).split())
        return text or "не хватает данных для этого действия"
    text = " ".join(str(error or "").split())
    code = _http_code(text)
    if code == 403:
        return f"нет прав на действие «{pretty_method(method)}»"
    if code == 404:
        return f"STOCRM не знает действие «{pretty_method(method)}»"
    if code and code >= 500:
        return f"ошибка на стороне STOCRM при действии «{pretty_method(method)}»"
    if code == 423:
        return "карточка заблокирована другим сотрудником"
    return text


def humanize_error(error: Any, *, context: str = "") -> str:
    """Из любого исключения делает понятное сообщение: что случилось + что делать.

    Никогда не показывает трейсбек, JSON и имена методов без перевода.
    """
    method = getattr(error, "method", "") or ""
    raw = clean_html(str(error or "")).strip()
    raw = _TRACEBACK.sub("", raw).strip()
    low = raw.lower()

    if "справочник поставщиков пуст" in low:
        return "В CRM нет доступного поставщика свободной позиции. Администратору нужно настроить справочник поставщиков в STOCRM. Товар и сделка сохранены; запись не начата."
    if "поставщик" in low and any(k in low for k in ("не выбран", "однознач", "справочник", "виртуальн")):
        return raw + "\n\nЧто сделать: выберите поставщика из справочника CRM. Товар и сделку искать заново не нужно; запись не начата."
    if "wrong uri" in low or "no api method available" in low:
        return "CRM не поддерживает этот метод обращения. Покажите администратору свежий support_report.txt для проверки совместимости API. Повторная отправка той же заявки причину не устранит."
    if "ответ не json" in low:
        head = f"{context}: " if context else ""
        return (head + "CRM вернула нечитаемый ответ при выполнении действия. Запись могла сохраниться; повторное добавление остановлено."
                "\n\nЧто сделать: Откройте /drafts → «Проверить CRM». Не повторяйте исходную просьбу и не подтверждайте результат, пока цены и количества не совпадут. Подробности ответа сохранены в диагностике без его содержимого.")
    if "повторное добавление запрещено" in low or "черновик не закрыт" in low:
        head = f"{context}: " if context else ""
        return head + raw + "\n\nЧто сделать: Откройте /drafts → «Проверить CRM». Не повторяйте исходную просьбу; проверьте строки, цены и количества в сделке."
    if any(word in low for word in ("выполнен частично", "выполнено частично", "дубликат", "результат неизвестен")):
        head = f"{context}: " if context else ""
        return head + raw + "\n\nЧто сделать: Откройте /drafts и нажмите «Проверить CRM». Не повторяйте добавление вслепую."
    advice = ""
    for keys, hint in _ADVICE:
        if any(key in low for key in keys):
            advice = hint
            break

    # метод панели в тексте ошибки переводим на человеческий
    def _swap(match: re.Match[str]) -> str:
        return METHOD_TITLES.get(match.group(1), match.group(1))

    detail = _METHOD_TOKEN.sub(_swap, raw)
    detail = _JSON_START.sub("", detail)
    detail = _first_sentence(detail)
    if any(k in low for k in ("сетевая ошибка", "connection aborted", "connectionreseterror",
                              "оборвалась", "remote end closed")):
        # «ConnectionResetError(10054, …)» человеку ничего не объясняет
        detail = "связь с STOCRM прервалась, ответ от панели не дошёл"
    # «HTTP 403 для …» и прочие служебные формулировки заменяем понятными
    if re.search(r"HTTP|\{'", raw) or isinstance(error, KeyError):
        detail = _describe_by_type(error, str(method))

    if not detail or detail.strip() in {"''", '""'}:
        detail = "STOCRM не принял запрос"
    if _TECH_WORDS.search(detail) and not advice:
        # служебные слова без объяснения человеку не помогают
        detail = "внутренняя ошибка при обращении к STOCRM"
        advice = advice or "Повторите просьбу. Если повторится — покажите это сообщение администратору."

    if "что сделать" in low:
        # В тексте ошибки УЖЕ есть готовые шаги (так бот объясняет удаление позиции
        # и правку цены). Раньше к ним приклеивалось «Повторите просьбу. Если ошибка
        # повторится…» — и человек получал противоречивые советы вместо одного.
        return raw.strip()
    head = f"{context}: {detail}" if context else detail
    if not advice:
        advice = "Повторите просьбу. Если ошибка повторится — покажите это сообщение администратору."
    return f"{head.rstrip('.')}.\n\nЧто сделать: {advice}"


#: Панель любит HTML: «заблокирована. Её держит: <br> Заремук …». В чат такое отдавать нельзя.
_HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div)\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?\s*[a-z][a-z0-9]*(?:\s[^<>]*)?/?>", re.IGNORECASE)
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&apos;": "'",
             "&lt;": "<", "&gt;": ">", "&mdash;": "—", "&ndash;": "–"}


def clean_html(text: str) -> str:
    """Убирает разметку панели: <br> → перенос строки, остальные теги — вон."""
    result = _HTML_BREAK.sub("\n", str(text or ""))
    result = _HTML_TAG.sub(" ", result)
    for entity, char in _ENTITIES.items():
        result = result.replace(entity, char)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r" *\n *", "\n", result)
    return result.strip()


def polish(text: str, *, limit: int = 3800, preserve_numbered: bool = False) -> str:
    """Финальная причёска любого ответа человеку.

    * имена методов панели → человеческие фразы;
    * трейсбеки и «сырой» JSON вырезаются;
    * убираются пустые строки-дубли и лишние пробелы;
    * длина ограничивается аргументом limit; отправитель разбивает длинные ответы.
    """
    if not text:
        return ""
    result = clean_html(str(text))
    result = _TRACEBACK.sub("", result)
    if _JSON_WHOLE.match(result):
        # целиком дамп JSON — человеку показывать нечего
        human = _payload_text(result)
        if human:
            return polish(human, limit=limit, preserve_numbered=preserve_numbered)
    result = _METHOD_TOKEN.sub(lambda m: METHOD_TITLES.get(m.group(1), m.group(1)), result)
    result = _MARKDOWN.sub("", result)
    lines = []
    for line in result.splitlines():
        stripped = line.strip()
        # строка-дамп JSON/словаря: выбрасываем и с одинарными, и с двойными кавычками
        if (_JSON_START.match(stripped) and ("{" in stripped or "[" in stripped)
                and (stripped.endswith(("}", "]", "}…", "]…")) or ":" in stripped)):
            continue
        if (_TECH_WORDS.search(stripped) and "Что сделать" not in stripped
                and not (preserve_numbered and re.match(r"^\d+\.\s", stripped))):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


#: Что бот говорит, когда инструмент вернул ошибку, а не результат.
_PAYLOAD_ERRORS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("справочник поставщиков пуст",),
        "Справочник поставщиков пуст или не содержит корректных ID. Товар и сделка не потеряны.",
        "Администратору нужно настроить поставщика свободной позиции в STOCRM; запись не начата.",
    ),
    (
        ("не выбран поставщик", "справочник поставщиков", "однозначный виртуальный поставщик"),
        "Не выбран поставщик для свободной позиции. Товар и сделка не потеряны.",
        "Выберите поставщика из справочника CRM; запись не начата.",
    ),
    (
        ("не найдено", "не найден", "not found"),
        "STOCRM не нашёл эту запись — возможно, позицию уже удалили или номер сделки другой.",
        "Проверьте номер сделки и позицию в панели и повторите. Ничего не изменено.",
    ),
    (
        ("не указана цена", "цена продажи 0", "price"),
        "у выбранной позиции не указана цена продажи, а без цены добавлять нельзя.",
        "Назовите цену продажи — например «поставь 5200» — и я подготовлю добавление.",
    ),
    (
        ("нужно название", "name"),
        "у выбранной позиции в панели нет названия, поэтому добавить её нельзя.",
        "Пришлите бренд и артикул этой позиции — подберу заново.",
    ),
    (
        ("неверные аргументы", "неизвестный инструмент", "не реализован"),
        "я не смог разобрать, что именно нужно сделать.",
        "Напишите просьбу одной короткой фразой, например «добавь фильтр W 914/2 в сделку 1312».",
    ),
    (
        ("нет прав", "403", "запрещ", "forbidden"),
        "у ключа STOCRM нет прав на это действие.",
        "Попросите администратора выдать доступ или сделайте это в самой панели.",
    ),
)


def _payload_text(data: Any) -> str | None:
    """Человеческий текст из ответа инструмента (JSON-словарь), если он есть."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    if isinstance(data, list):
        # [{"error": ...}] — ответ-обёртка из одного элемента
        return _payload_text(data[0]) if len(data) == 1 else None
    if not isinstance(data, dict):
        return None
    for key in ("question", "summary", "message", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            low = value.lower()
            if key != "error":
                return value.strip()
            for keys, what, advice in _PAYLOAD_ERRORS:
                if any(_mentions(low, word) for word in keys):
                    return f"{what}\n\nЧто сделать: {advice}"
            # Незнакомая ошибка. Раньше она превращалась в «Готово.» — то есть бот
            # говорил «всё сделано», хотя не сделано ничего. Теперь честно.
            return (f"не получилось: {_short_reason(value)}\n\n"
                    "Что сделать: повторите просьбу. Если повторится — пришлите это "
                    "сообщение администратору. Ничего не изменено.")
    return None


def _mentions(text: str, word: str) -> bool:
    """Ищет слово целиком. Иначе «price» находился внутри «PRICE_SUPP_HUM»."""
    if word.isascii() and word.isalpha():
        return re.search(rf"\b{re.escape(word)}\b", text) is not None
    return word in text


def _short_reason(value: str, limit: int = 160) -> str:
    """Обрывок ошибки панели без техники: без JSON, путей и HTML."""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "причина не сообщена"


def payload_text(payload: Any, *, default: str = "Готово.") -> str:
    """Текст для человека из «сырого» ответа инструмента.

    Раньше, если инструмент падал (например, панель ответила «Не найдено»), бот
    отправлял в чат сам JSON: `{"error": "Не найдено"}`. Теперь такой ответ
    превращается в объяснение, а если объяснять нечего — в короткое «Готово».
    """
    raw = str(payload or "").strip()
    if not raw:
        return default
    text = _payload_text(raw)
    if text:
        return polish(text)
    # JSON без понятного текста: показывать его человеку бессмысленно
    if _JSON_WHOLE.match(raw) or _JSON_START.match(raw):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return default
        if isinstance(data, list) and data:
            return default
        return default
    return polish(raw) or default


def describe_exception(error: BaseException) -> str:
    """Короткое описание исключения для логов и ответа (без трейсбека)."""
    if isinstance(error, StocrmError):
        method = f" ({pretty_method(error.method)})" if error.method else ""
        return f"{_first_sentence(str(error), 300)}{method}"
    return _first_sentence(str(error) or error.__class__.__name__, 300)
