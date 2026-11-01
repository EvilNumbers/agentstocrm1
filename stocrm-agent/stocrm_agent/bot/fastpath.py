"""Быстрые ответы на простые просьбы — без обращения к модели.

Зачем: каждый шаг «мозга» — это 1–3 секунды ожидания. На типовых вопросах («покажи
сделку 1309», «какие последние сделки») модель не нужна вовсе: данные берутся из
панели и сразу оформляются в ответ.

Безопасность:
  * обрабатываются только ЧТЕНИЯ (сделка, список сделок) — ни одного изменения,
    ни одного черновика;
  * если во фразе есть хоть намёк на действие (собери, добавь, измени, привяжи…)
    или на сценарий ТО, быстрый путь молчит и всё идёт как раньше — через модель;
  * если данных нет или API ответил ошибкой, тоже возвращаем None: пусть ответит
    обычный путь, он объяснит причину.
"""

from __future__ import annotations

import re
from typing import Any

from .status_colors import with_status_colors

#: Слова, при которых быстрый путь НЕ вмешивается: это уже действие или сценарий.
_ACTION_WORDS = (
    "собер", "собери", "добав", "измен", "помен", "поставь", "созда", "назнач",
    "удал", "привяж", "привяз", "перевед", "запиш", "заметк", "задач", "цен",
    "масл", "фильтр", "исполнител", "позвони", "отправ", "подтверд", "примен",
    "отмен", "статус", "закрой", "открой заявк",
)

#: «ТО» проверяем отдельно: как часть других слов («что», «это», «авто») оно
#: встречается постоянно, а как отдельное слово — всегда означает сценарий ТО.
_TO_WORD = re.compile(r"\bто\b|\bто-?\d|\bтехобслуж", re.IGNORECASE)

_ASK_WORDS = (
    "покаж", "что по", "карточк", "подробн", "инфо", "расскаж", "открой", "сделк",
    "заказ-наряд", "заказ наряд", "#", "номер", "товар", "позиц", "услуг", "работ",
    "оплат", "долг", "итог", "сколько",
)

#: Просьбы, на которые человек ждёт ПОДРОБНОСТИ: товары, услуги, оплаты, долг.
_DETAIL_WORDS = (
    "подробн", "товар", "позиц", "услуг", "работ", "оплат", "долг", "итог", "сколько",
)

_NUMBER = re.compile(r"(?:#|№|сделк[аиуеой]*\s*#?|offer\s*#?)?\s*(\d{2,6})(?![\d])", re.IGNORECASE)
#: Слова, из которых может состоять просьба «покажи список сделок». Если после их
#: удаления в просьбе осталось хоть одно слово — значит человек просит выборку по
#: условию (статус, клиент, дата), и быстрый путь не вмешивается: иначе бот показал
#: бы последние сделки вместо отфильтрованных.
_LIST_FILLERS = frozenset((
    "покажи", "покажите", "какие", "что", "дай", "дайте", "открой", "откройте",
    "список", "последние", "последних", "свежие", "новые", "сделки", "сделок",
    "сделку", "заказ", "наряды", "наряд", "заказ-наряды", "все", "всё", "мне",
    "нам", "пожалуйста", "ещё", "еще", "там", "эти",
))
_LIST_REQUIRED = ("сделки", "сделок", "сделку", "список", "наряды")


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    low = " " + text.strip().lower().replace("ё", "е") + " "
    return any(word in low for word in words)


def _is_action(text: str) -> bool:
    """Есть ли в просьбе действие (собрать, добавить, поменять…) или сценарий ТО."""
    return _has_any(text, _ACTION_WORDS) or bool(_TO_WORD.search(text))


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _qty_line(item: dict[str, Any]) -> str:
    """«1 шт. × 6 000 ₽ = 6 000 ₽» — без выдуманных чисел."""
    qty = item.get("qty")
    price = item.get("price")
    if qty is None or qty == "":
        return "количество не получено" + (f" · цена {_money(price)} ₽" if price else "")
    try:
        number = float(qty)
        if not (number >= 0 and number < 1e9):
            raise ValueError("invalid quantity")
    except (ValueError, TypeError, OverflowError):
        return "количество не получено" + (f" · цена {_money(price)} ₽" if price else "")
    line = f"{qty} шт."
    if price:
        line += f" × {_money(price)} ₽"
        line += f" = {_money(float(price) * number)} ₽"
    return line


def format_offer_items(card: dict[str, Any]) -> str:
    """Товары, услуги, оплаты и долг — то, что человек зовёт «подробностями»."""
    lines: list[str] = []
    parts = card.get("parts") or []
    works = card.get("works") or []

    if parts:
        lines.append(f"🧩 Товары ({len(parts)}):")
        for number, item in enumerate(parts, 1):
            head = f"{number}. {item.get('name') or 'без названия'}"
            tail = " · ".join(str(x) for x in (item.get("brand"), item.get("sku")) if x)
            if tail:
                head += f" ({tail})"
            lines.append(f"{head} — {_qty_line(item)}")
    elif card.get("parts_error"):
        lines.append("🧩 Товары: не удалось проверить")
    else:
        lines.append("🧩 Товары: нет")

    if works:
        lines.append(f"🔧 Услуги ({len(works)}):")
        for number, work in enumerate(works[:20], 1):
            amount = f"{_money(work['sum'])} ₽" if work.get("sum") is not None else "сумма не получена"
            line = f"{number}. {work.get('title') or 'без названия'} — {amount}"
            performers = [p.get("name") for p in (work.get("performers") or []) if p.get("name")]
            if performers:
                line += " · исполнитель: " + ", ".join(performers[:3])
            lines.append(line)
        if len(works) > 20:
            lines.append(f"… и ещё {len(works) - 20}")
    elif card.get("works_error"):
        lines.append("🔧 Услуги: не удалось проверить")
    else:
        lines.append("🔧 Услуги: нет")

    payments = card.get("payments") or {}
    paid = card.get("paid")
    money = f"💰 Оплачено: {_money(paid)} ₽" if paid is not None else "💰 Оплачено: не удалось проверить"
    by_kind = payments.get("by_kind") or {}
    if by_kind:
        money += " (" + ", ".join(f"{kind} {_money(value)} ₽"
                                  for kind, value in by_kind.items()) + ")"
    lines.append(money)
    if card.get("overpaid"):
        lines.append(f"↩️ Переплата: {_money(card['overpaid'])} ₽")
    elif card.get("debt") is not None:
        lines.append(f"📉 Долг (осталось оплатить): {_money(card['debt'])} ₽")
    else:
        lines.append("⚠️ Долг: не удалось проверить баланс")
    if card.get("items_error"):
        lines.append(f"⚠️ Часть данных не прочиталась: {card['items_error']}")
    return "\n".join(lines)


def format_offer_card(card: dict[str, Any], *, detailed: bool = False) -> str:
    """Карточка сделки человеческим языком — то же, что раньше говорила модель."""
    lines = [f"📋 Сделка №{card.get('offer_id')}"]
    who = card.get("contact") or "клиент не указан"
    line = f"Клиент: {who}"
    if card.get("car"):
        line += f" · {card['car']}"
    if card.get("plate"):
        line += f" ({card['plate']})"
    lines.append(line)
    if card.get("vin"):
        lines.append(f"VIN: {card['vin']}")
    status = " · ".join(
        str(x) for x in (card.get("board"), card.get("status")) if x
    )
    if status:
        marker = card.get("status_marker")
        lines.append(f"Воронка и статус: {marker + ' ' if marker else ''}{status}")
    sums = f"Сумма: {_money(card.get('sum'))} ₽"
    parts_sum = card.get("parts_sum")
    works_sum = card.get("works_sum")
    if parts_sum or works_sum:
        sums += f" (работы {_money(works_sum)} ₽, запчасти {_money(parts_sum)} ₽)"
    lines.append(sums)
    if card.get("payment_status") and not detailed:
        # в подробном виде ниже будут реальные суммы оплат — «Нет оплаты» из карточки
        # рядом с «Оплачено: 5 000 ₽» только путает
        lines.append(f"Оплата: {card['payment_status']}")
    if not detailed:
        if card.get("debt") is not None:
            lines.append(f"🔴 ДОЛГ: {_money(card['debt'])} ₽" if card['debt'] else "✅ Долг: 0 ₽")
            if card.get("overpaid"):
                lines.append(f"↩️ Переплата: {_money(card['overpaid'])} ₽")
        else:
            lines.append("⚠️ Долг: не удалось проверить баланс")
    if card.get("responsible"):
        lines.append(f"Ответственный: {card['responsible']}")
    if detailed:
        lines.append("")
        lines.append(format_offer_items(card))
        lines.append("")
        lines.append("Могу добавить работу или запчасть, сменить статус — скажите, что нужно.")
    else:
        lines.append("")
        lines.append("Могу собрать ТО, добавить работу или запчасть, сменить статус — "
                     "скажите, что нужно. Скажите «подробно» — покажу товары, услуги и оплаты.")
    return "\n".join(lines)


def format_offers_list(rows: list[dict[str, Any]]) -> str:
    """Список сделок — нумерация, чтобы человек мог сказать «3 подробнее»."""
    if not rows:
        return ""
    lines = [f"📋 Последние сделки ({len(rows)}):"]
    for number, row in enumerate(rows, 1):
        marker = row.get("status_marker")
        head = f"{number}. №{row.get('offer_id')}" + (f" {marker}" if marker else "")
        if row.get("contact"):
            head += f" {row['contact']}"
        if row.get("car"):
            head += f" · {row['car']}"
        head += f" — {_money(row.get('sum'))} ₽"
        if row.get("status"):
            head += f", {row['status']}"
        lines.append(head)
    lines.append("")
    lines.append("Скажите номер в списке («3 подробнее») — открою сделку.")
    return "\n".join(lines)


# Только самостоятельная просьба о списке: не перехватываем изменения или другие
# фильтры (клиент, дата, исполнитель), которые потребовали бы отдельного разбора.
_WORKING_OFFERS = re.compile(
    r"(?:(?:покажи|покажите|найди|найдите|дай|дайте|какие|выведи|выведите)\s+)?"
    r"(?:(?:мне|нам|все|всё|список|последние|сейчас|у нас)\s+)*"
    r"(?:сделки|сделок|сделку|заказы|заказов|заказ-наряды|заказ наряды|наряды|что)(?:\s+(?:сейчас|у нас|которые))?\s+в\s+работе"
    r"(?:\s+(?:на|в)\s+сервисе)?(?:\s+сейчас)?"
    r"(?:\s+в\s+воронк[еи]\s+(?P<board>[^!?]{1,100}?))?\s*[.!?]*",
    re.IGNORECASE,
)


def _working_request(text: str) -> re.Match[str] | None:
    return _WORKING_OFFERS.fullmatch((text or "").strip())


def is_working_offers_request(text: str) -> bool:
    return _working_request(text) is not None


# Статусы не определяются по подстроке: «Заказ» не включает «Заказ собран».
_STATUS_TITLES: dict[str, set[str]] = {
    "record": {"запись", "в записи"},
    "order": {"заказ", "в заказе"},
    "estimate": {"просчет", "в просчете", "просчет запчастей", "проценка", "проценка запчастей"},
    "estimate_parts": {"просчет запчастей", "проценка запчастей"},
    "assembled": {"заказ собран", "заказы собраны", "собранный заказ", "собранные заказы", "собрано", "собран"},
}
_PREFIX = r"(?:(?:покажи|покажите|найди|найдите|дай|дайте|какие|выведи|выведите|список)\s+)?(?:(?:мне|нам|все|всё|последние|сейчас|у нас|список)\s+)*"
_SUBJECT = r"(?:сделки|сделок|сделку|заказы|заказов|заказ-наряды|заказ наряды|наряды|что)"
_BOARD_SUFFIX = r"(?:\s+в\s+воронк[еи]\s+(?P<board>[^!?]{1,100}?))?\s*[.!?]*"
_CATEGORY_REQUEST = re.compile(
    _PREFIX + _SUBJECT + r"(?:\s+(?:сейчас|у нас|которые))?\s+(?:в|на)\s+"
    r"(?P<stage>записи|заказе|просч[её]те(?:\s+запчаст(?:ей|и|ями))?|проценке(?:\s+запчаст(?:ей|и|ями))?)"
    r"(?:\s+сейчас)?" + _BOARD_SUFFIX, re.I)
_ASSEMBLED_REQUEST = re.compile(
    _PREFIX + r"(?:(?:собранные|собранных)\s+(?:заказы|заказов|сделки|сделок|заказ-наряды)|"
    r"(?:заказы|сделки|заказ-наряды)\s+(?:собраны|собранные))"
    r"(?:\s+сейчас)?" + _BOARD_SUFFIX, re.I)
_EXPLICIT_REQUEST = re.compile(
    _PREFIX + _SUBJECT + r"\s+(?:в\s+статусе|со\s+статусом|по\s+статусу)\s+"
    r"(?P<title>[^!?]{1,100}?)" + _BOARD_SUFFIX, re.I)
_STATUS_MARKERS = re.compile(
    r"\b(?:в|на)\s+(?:работе|записи|заказе|просч[её]те|проценке)\b|"
    r"\bсобранн\w*\b|\bсобраны\b|\b(?:статусе|статусом|статусу)\b", re.I)
_LIST_SUBJECT = re.compile(r"\b(?:сделки|сделок|заказы|заказов|наряды|заказ-наряды)\b", re.I)
_WRITE_ACTION = re.compile(r"\b(?:добав\w*|созда\w*|измен\w*|помен\w*|удал\w*|перевед\w*|"
                           r"перевод\w*|смени\w*|назнач\w*|запиш\w*|записать|собери\w*|собрать|отмен\w*)\b", re.I)
_READ_START = re.compile(r"^\s*(?:покажи|покажите|найди|найдите|дай|дайте|какие|что|сколько|список|выведи|выведите|сделки|сделок|заказы|собранные)\b", re.I)
# Неизвестные состояния тоже не превращаем в список активных. Исключаем
# формулировки с воронкой и названиями месяцев/относительными датами.
_UNKNOWN_STAGE = re.compile(
    r"\b(?:сделки|сделок|заказы|заказов|наряды)(?:\s+(?:сейчас|которые))?\s+(?:в|на)\s+"
    r"(?!(?:воронк\w*|январ\w*|феврал\w*|март\w*|апрел\w*|ма[ейя]\w*|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|сегодня|завтра|вчера|этой|этот|прошл\w*)\b)"
    r"[а-яё][а-яё-]*", re.I)


def _norm_status(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("ё", "е").split()).strip(' «»"')


def _status_request(text: str) -> dict[str, str | None] | None:
    message = (text or "").strip()
    if not message or message.startswith("/"):
        return None
    working = _working_request(message)
    if working:
        return {"kind": "working", "board": working.group("board")}
    explicit = _EXPLICIT_REQUEST.fullmatch(message)
    if explicit:
        return {"kind": "explicit", "board": explicit.group("board"),
                "title": explicit.group("title").strip().rstrip(".").strip(' «»"')}
    assembled = _ASSEMBLED_REQUEST.fullmatch(message)
    if assembled:
        return {"kind": "assembled", "board": assembled.group("board")}
    category = _CATEGORY_REQUEST.fullmatch(message)
    if category:
        stage = _norm_status(category.group("stage"))
        if stage == "записи":
            kind = "record"
        elif stage == "заказе":
            kind = "order"
        else:
            kind = "estimate_parts" if "запчаст" in stage else "estimate"
        return {"kind": kind, "board": category.group("board")}
    # Если к статусу прибавили ещё условия, не передаём неоднозначную выборку
    # модели как обычный список активных сделок: просим сформулировать отдельно.
    if (_READ_START.search(message) and _LIST_SUBJECT.search(message)
            and (_STATUS_MARKERS.search(message) or _UNKNOWN_STAGE.search(message)) and not _WRITE_ACTION.search(message)):
        return {"kind": "clarify"}
    return None


def is_status_offers_request(text: str) -> bool:
    return _status_request(text) is not None


def _status_offers(tools: Any, request: dict[str, str | None]) -> tuple[str, list[int]]:
    """Список только для проверенной пары (воронка, точный статус)."""
    from ..client import read_budget

    kind = str(request.get("kind") or "")
    board_name = request.get("board")
    if kind == "working":
        specific, generic = {"в работе на сервисе", "в работе в сервисе"}, {"в работе"}
    elif kind == "explicit":
        specific, generic = {_norm_status(request.get("title"))}, set()
    else:
        specific, generic = _STATUS_TITLES[kind], set()
    targets = specific | generic
    try:
        with read_budget(8.0):
            boards = tools.boards()
            if board_name:
                name = _norm_status(board_name).rstrip(".")
                boards = [b for b in boards if _norm_status(b.get("name")) == name
                          or (name.lstrip("№#").strip().isdigit() and str(b.get("board_id")) == name.lstrip("№#").strip())]
                if len(boards) != 1:
                    return "Не удалось однозначно найти указанную воронку в CRM. Уточните её название или номер.", []
            precise: list[tuple[dict[str, Any], dict[str, Any]]] = []
            fallback: list[tuple[dict[str, Any], dict[str, Any]]] = []
            unresolved = False
            for board in boards:
                try:
                    board_id = int(str(board.get("board_id")))
                    if board_id <= 0 or not str(board.get("name") or "").strip():
                        raise ValueError("invalid board")
                except (TypeError, ValueError):
                    unresolved = True
                    continue
                for status in tools.statuses(board_id):
                    title = _norm_status(status.get("title"))
                    if title not in targets:
                        if not title:
                            unresolved = True
                        continue
                    status_board = status.get("board_id")
                    if status_board not in (None, "") and str(status_board) != str(board_id):
                        continue  # справочник иногда возвращает статусы других воронок
                    try:
                        status_id = int(str(status.get("status_id")))
                        if status_board in (None, "") or status_id <= 0:
                            raise ValueError("invalid status")
                    except (TypeError, ValueError):
                        unresolved = True
                        continue
                    (precise if title in specific else fallback).append((board, status))
            if unresolved:
                return "Не удалось надёжно проверить справочник воронок или статусов. Список сделок не подтверждён.", []
            matches = precise or fallback
            if not matches:
                if kind == "working":
                    return "В CRM не найден точный статус «В работе на сервисе» или «В работе». Проверьте название статуса в воронке.", []
                return "В CRM не найден подходящий точный статус. Уточните название из CRM: «покажи сделки в статусе НАЗВАНИЕ».", []
            if len(matches) > 1:
                labels = "\n".join(f"• {b['name']} (№{b['board_id']}) · {s['title']}" for b, s in matches)
                return ("Нашлось несколько подходящих статусов. Уточните статус и воронку:\n" + labels +
                        "\nНапример: «покажи сделки в статусе НАЗВАНИЕ в воронке НАЗВАНИЕ».", [])
            board, status = matches[0]
            valid: list[dict[str, Any]] = []
            seen: set[str] = set()
            complete = False
            uncertain = False
            page_size = 100
            for page in range(1, 6):
                try:
                    rows = tools.find_offers(board_id=int(board["board_id"]),
                        status_id=int(status["status_id"]), limit=page_size, page=page, fresh=True)
                except Exception:
                    uncertain = True
                    break
                if rows and all(str(r.get("offer_id") or "") in seen for r in rows):
                    uncertain = True
                    break
                for row in rows:
                    ident = str(row.get("offer_id") or "")
                    if not ident.isdigit() or int(ident) <= 0:
                        uncertain = True
                        continue
                    if ident in seen:
                        continue
                    seen.add(ident)
                    if row.get("board_id") in (None, "") or row.get("status_id") in (None, ""):
                        uncertain = True
                        continue
                    if str(row["status_id"]) == str(status["status_id"]) and str(row["board_id"]) == str(board["board_id"]):
                        # Наименование статуса берём из проверенного справочника ID, а не из чужой строки.
                        valid.append({**row, "status": status["title"]})
                if len(valid) > 25:
                    uncertain = True
                    break
                if len(rows) < page_size:
                    complete = True
                    break
            if not complete:
                uncertain = True
            valid = valid[:25]
            qualifier = "\n⚠️ Проверка не завершена или список ограничен; он может быть неполным." if uncertain else ""
            label = f"«{status['title']}» · воронка «{board['name']}»"
            if not valid:
                return f"В полученной выдаче среди просмотренных сделок не найдено подтверждённых сделок со статусом {label}." + qualifier, []
            result = format_offers_list(with_status_colors(tools, valid))
            result = result.replace(f"📋 Последние сделки ({len(valid)}):", f"📋 Сделки в статусе {label}:", 1)
            return result + qualifier, [int(r["offer_id"]) for r in valid]
    except Exception:
        return "Не удалось прочитать воронки или статусы CRM. Список сделок не подтверждён.", []


def try_answer(text: str, tools: Any) -> tuple[str, list[int]] | None:
    """Пытается ответить без модели.

    Возвращает (готовый ответ, номера сделок по порядку) или None — значит,
    нужен обычный путь. Номера нужны, чтобы «3 подробнее» и дальше работало.
    """
    message = (text or "").strip()
    if not message or message.startswith("/"):
        return None
    status_request = _status_request(message)
    if status_request:
        if status_request["kind"] == "clarify":
            return ("Не удалось надёжно разделить условия. Уточните один статус и воронку отдельной фразой; "
                    "Клиента или дату в таком запросе пока нельзя надёжно совместить: отправьте запросы отдельно. "
                    "Общий список активных сделок не подменяю.", [])
        return _status_offers(tools, status_request)
    if _is_action(message):
        return None

    low = message.lower().replace("ё", "е")

    if re.search(r"телефон|номер\s+(?:клиента|клиентки)|как\s+связаться", low):
        explicit = re.search(r"сделк\w*\s*(?:№|#)?\s*(\d+)\b", low)
        if explicit:
            offer_id = int(explicit[1])
            try:
                result = tools.offer_contact_phones(offer_id)
            except Exception as exc:  # noqa: BLE001 — не заменяем ответ общей карточкой
                return f"Не удалось проверить телефон в сделке №{offer_id}: {exc}", [offer_id]
            who = result.get("contact") or "клиент"
            phones = result["phones"]
            answer = ("Телефоны: " + "; ".join(phones)) if phones else "В карточке контакта CRM телефоны не указаны."
            return f"Сделка №{offer_id} · {who}\n{answer}", [offer_id]

    # 1) список последних сделок — только если просьба целиком про список
    words = [w for w in re.findall(r"[\w-]+", low) if w]
    if (words
            and any(word in words for word in _LIST_REQUIRED)
            and all(word in _LIST_FILLERS for word in words)):
        try:
            rows = tools.find_offers(limit=10)
        except Exception:      # noqa: BLE001 — не смогли — пусть отвечает обычный путь
            return None
        offers = rows or []
        ids = [int(row["offer_id"]) for row in offers if row.get("offer_id")]
        answer = format_offers_list(with_status_colors(tools, offers))
        return (answer, ids) if answer else None

    # 2) карточка конкретной сделки
    if not _has_any(message, _ASK_WORDS):
        return None
    if re.search(r"(?<!\d)[+\d ()-]{10,}(?!\d)", message) and not re.search(r"сделк\w*\s*(?:№|#)?\s*\d{1,7}\b", low):
        return None
    match = _NUMBER.search(message)
    if not match:
        return None
    offer_id = int(match.group(1))
    # «покажи подробно», «какие товары», «сколько оплачено», «какой долг» — читаем
    # товары, услуги и оплаты сразу: человек именно это и просит.
    detailed = _has_any(message, _DETAIL_WORDS)
    try:
        card = tools.offer_card(offer_id, with_items=detailed)
    except Exception:          # noqa: BLE001
        return None
    if not isinstance(card, dict) or card.get("error") or not card.get("offer_id"):
        return None            # пусть обычный путь объяснит, почему не нашлась
    return format_offer_card(with_status_colors(tools, [card])[0], detailed=detailed), [offer_id]
