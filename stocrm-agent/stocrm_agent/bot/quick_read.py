"""Безопасное чтение системного контакта и статуса быстрых сделок."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from ..booking import key
from ..calendar import _positive, _rows
from ..contact_search import phone_key, detail_phones
from .fastpath import format_offer_items

PHONE = "81111111110"
NAME = "1111 быстрые сделки"


def is_quick_request(text: str) -> bool:
    return bool(re.search(r"\bбыстр\w*\s+сделк\w*", text, re.I))


def is_read_request(text: str) -> bool:
    # Любая инструкция о записи блокируется, даже если рядом есть «покажи».
    return bool(re.search(r"\b(?:покаж\w*|список|какие|сколько|посмотр\w*|найди|вывед\w*)\b", text, re.I)) and not bool(
        re.search(r"\b(?:созда\w*|нов\w*|добав\w*|внес\w*|запиш\w*|удал\w*|убер\w*|измен\w*|помен\w*|перевед\w*)\b", text, re.I))


def _inactive(value: Any) -> bool:
    return value is False or value == 0 or (isinstance(value, str) and value.strip().upper() in {'N', 'NO', 'FALSE', '0'})


def identity(tools: Any) -> tuple[int, int, int]:
    tools._invalidate_quick_identity_cache()
    # Ищем только контакт с ТОЧНЫМ именем. Чужие контакты не требуют чтения
    # дополнительных телефонов и не делают обычную большую базу неполной.
    matches: list[int] = []
    seen: set[int] = set()
    for page in range(1, 21):
        raw = tools._read("quick_contacts", "contacts/get_from_filter", {"FILTER": {}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (list, dict)):
            raise ValueError("Не удалось подтвердить список контактов")
        rows = _rows(raw)
        if isinstance(raw, dict) and raw and not rows and not any(isinstance(raw.get(k), list) and not raw[k] for k in ("DATA", "ITEMS", "LIST", "ROWS")):
            raise ValueError("Неизвестный формат списка контактов; поиск не подтверждён")
        if not rows:
            break
        for contact in rows:
            ident = _positive(contact.get("CONTACT_ID"), "Контакт")
            if ident in seen:
                raise ValueError("CRM повторяет страницы контактов; поиск не подтверждён")
            seen.add(ident)
            if not str(contact.get("TITLE") or "").strip():
                raise ValueError("CRM не вернула имя одного из контактов; поиск не подтверждён")
            if key(contact.get("TITLE")) != NAME:
                continue
            phones = {phone_key(contact.get("MAIN_PHONE"))}
            if phone_key(PHONE) not in phones:
                # Другой контакт с тем же именем может иметь наш номер дополнительным.
                # Ошибка чтения не означает отсутствия этого номера.
                raw_detail = tools._read("quick_contact_phones", "contact/get_info", {"CONTACT_ID": ident})
                phones.update(phone_key(value) for value in detail_phones(raw_detail, ident))
            if phone_key(PHONE) in phones:
                matches.append(ident)
    else:
        raise ValueError("Поиск системного контакта неполон")
    if len(matches) != 1:
        raise ValueError("Системный контакт не подтверждён однозначно по имени и телефону. Проверьте контакт в STOCRM; другой контакт не выбираю")
    cid = matches[0]
    candidates = []
    for board in tools.boards():
        if _inactive(board.get("active")):
            continue
        bid = _positive(board.get("board_id"), "Воронка")
        for status in tools.statuses(bid):
            if key(status.get("title")) == "быстрые сделки" and not _inactive(status.get("active")):
                if not status.get("board_id"):
                    raise ValueError("Воронка статуса не подтверждена")
                if str(status.get("board_id")) != str(bid):
                    continue  # API может вернуть статусы других воронок
                candidates.append((bid, _positive(status.get("status_id"), "Статус")))
    if len(candidates) != 1:
        raise ValueError("Точный активный статус «Быстрые сделки» и его воронка не найдены однозначно. Проверьте справочник STOCRM")
    return cid, *candidates[0]


def offers(tools: Any, ids: tuple[int, int, int]) -> list[int]:
    cid, bid, sid = ids
    found: list[int] = []
    seen: set[int] = set()
    for page in range(1, 21):
        raw = tools._read("quick_offers", "offers/get_from_filter", {"FILTER": {"CONTACT_ID": cid, "BOARD_ID": bid, "STATUS_ID": sid}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (dict, list)):
            raise ValueError("CRM не подтвердила список быстрых сделок")
        rows = _rows(raw)
        if isinstance(raw, dict) and not rows and raw and not any(isinstance(raw.get(k), list) and not raw[k] for k in ("DATA", "ITEMS", "LIST", "ROWS")):
            raise ValueError("Неизвестный формат списка быстрых сделок")
        for row in rows:
            oid = _positive(row.get("OFFER_ID"), "Сделка")
            if oid in seen:
                raise ValueError("CRM повторяет страницы сделок; список не подтверждён")
            seen.add(oid)
            owner = _positive(row.get("CONTACT_ID"), "Контакт сделки")
            board = _positive(row.get("BOARD_ID"), "Воронка сделки")
            status = _positive(row.get("STATUS_ID"), "Статус сделки")
            if (owner, board, status) == ids:
                found.append(oid)
        # Даже короткая страница не доказывает конец: CRM может ограничить LIMIT.
        if not rows:
            return found
    raise ValueError("Список сделок неполный; повторите чтение в STOCRM")


def detail(tools: Any, ids: tuple[int, int, int], oid: int) -> str:
    card = tools.offer_card(oid, with_items=True)
    if card.get("error") or (str(card.get("contact_id")), str(card.get("board_id")), str(card.get("status_id"))) != tuple(map(str, ids)):
        raise ValueError(f"Сделка №{oid} больше не подтверждена для нужного контакта и статуса")
    return f"📋 Быстрая сделка №{oid}\n" + format_offer_items(card)


def details(tools: Any, ids: tuple[int, int, int], offers: list[int]) -> list[str]:
    """Параллельно читаем максимум две карточки, сохраняя порядок и предупреждения.

    Число CRM-запросов не увеличивается; ошибки одной карточки не скрывают остальные.
    """
    def one(oid: int) -> str:
        try:
            return detail(tools, ids, oid)
        except Exception:  # noqa: BLE001 — неполное чтение не означает нулевой долг
            return f"Сделка №{oid}: подробности не удалось подтвердить; не считаю долг нулевым."
    if len(offers) <= 1:
        return [one(oid) for oid in offers]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quick-detail") as pool:
        futures = [pool.submit(copy_context().run, one, oid) for oid in offers]
        return [future.result() for future in futures]
