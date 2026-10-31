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
    qty = item.get("qty") or 1
    price = item.get("price")
    line = f"{qty} шт."
    if price:
        line += f" × {_money(price)} ₽"
        line += f" = {_money(float(price) * float(qty))} ₽"
    return line


def format_offer_items(card: dict[str, Any]) -> str:
    """Товары, услуги, оплаты и долг — то, что человек зовёт «подробностями»."""
    lines: list[str] = []
    parts = card.get("parts") or []
    works = card.get("works") or []

    if parts:
        lines.append(f"🧩 Товары ({len(parts)}):")
        for number, item in enumerate(parts[:20], 1):
            head = f"{number}. {item.get('name') or 'без названия'}"
            tail = " · ".join(str(x) for x in (item.get("brand"), item.get("sku")) if x)
            if tail:
                head += f" ({tail})"
            lines.append(f"{head} — {_qty_line(item)}")
        if len(parts) > 20:
            lines.append(f"… и ещё {len(parts) - 20}")
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
    else:
        lines.append("🔧 Услуги: нет")

    payments = card.get("payments") or {}
    paid = card.get("paid")
    if paid is None:
        paid = payments.get("total") or 0
    money = f"💰 Оплачено: {_money(paid)} ₽"
    by_kind = payments.get("by_kind") or {}
    if by_kind:
        money += " (" + ", ".join(f"{kind} {_money(value)} ₽"
                                  for kind, value in by_kind.items()) + ")"
    lines.append(money)
    if card.get("overpaid"):
        lines.append(f"↩️ Переплата: {_money(card['overpaid'])} ₽")
    else:
        lines.append(f"📉 Долг (осталось оплатить): {_money(card.get('debt'))} ₽")
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
        lines.append(f"Воронка и статус: {status}")
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
        head = f"{number}. №{row.get('offer_id')}"
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


def _estimate_offers(tools: Any) -> tuple[str, list[int]]:
    from ..client import read_budget
    try:
        with read_budget(8.0):
            matches = []
            for board in tools.boards():
                for status in tools.statuses(int(board["board_id"])):
                    title = str(status.get("title") or "")
                    if title.lower().replace("ё", "е").startswith("просчет"):
                        matches.append((board, status))
            if not matches:
                return "Статус «Просчёт» в полученном справочнике не найден. Уточните название статуса; все сделки вместо фильтра не показываю.", []
            if len(matches) > 1:
                return "Уточните воронку и статус:\n" + "\n".join(f"• {b['name']} · {s['title']}" for b, s in matches), []
            board, status = matches[0]
            rows = tools.find_offers(board_id=int(board["board_id"]), status_id=int(status["status_id"]), limit=25)
            valid = [r for r in rows if str(r.get("status_id")) == str(status["status_id"]) and str(r.get("board_id")) == str(board["board_id"])]
            if len(valid) != len(rows):
                return "CRM вернула сделки, не соответствующие выбранному статусу. Фильтр не подтверждён; посторонние сделки не показываю.", []
            if not valid:
                return f"В полученной выборке нет сделок в статусе «{status['title']}» ({board['name']}).", []
            return format_offers_list(valid) + "\nПоказано до 25 сделок выбранного статуса.", [int(r["offer_id"]) for r in valid]
    except Exception as exc:
        return f"Не удалось закончить чтение сделок в просчёте: {exc}", []


def try_answer(text: str, tools: Any) -> tuple[str, list[int]] | None:
    """Пытается ответить без модели.

    Возвращает (готовый ответ, номера сделок по порядку) или None — значит,
    нужен обычный путь. Номера нужны, чтобы «3 подробнее» и дальше работало.
    """
    message = (text or "").strip()
    if not message or message.startswith("/"):
        return None
    if re.fullmatch(r"(?:покажи|покажите|найди|список)\s+сделк[иа]\s+(?:в|на)\s+просч[её]т[еа]?[.!?]?", message, re.I):
        return _estimate_offers(tools)
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
        answer = format_offers_list(offers)
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
    return format_offer_card(card, detailed=detailed), [offer_id]
