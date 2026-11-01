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
from .status_view import match_name as match_view_status_name

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


def contact_identity(tools: Any) -> int:
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
    return matches[0]


def identity(tools: Any) -> tuple[int, int, int]:
    return contact_identity(tools), *status_identity(tools)


def status_identity(tools: Any) -> tuple[int, int]:
    """Точный активный статус независимо от системного контакта."""
    tools._invalidate_quick_identity_cache()
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
    return candidates[0]


def quick_status_name(tools: Any, board: Any, name: Any, status_ids: tuple[int, int]) -> str:
    """Проверка по названию, ID статуса из строки сделки не используется."""
    return match_view_status_name(tools, board=board, status_name=name,
                                  expected_board=status_ids[0], expected_name='Быстрые сделки')


def offers(tools: Any, ids: tuple[int, int, int]) -> list[int]:
    _, bid, sid = ids
    found: list[int] = []
    seen: set[int] = set()
    for page in range(1, 21):
        raw = tools._read("quick_offers", "offers/get_from_filter", {"FILTER": {"BOARD_ID": bid}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
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
            status = quick_status_name(tools, row.get('BOARD_ID'), row.get('STATUS_NAME'), (bid, sid))
            if status == 'unknown':
                raise ValueError('Не удалось проверить название статуса сделки; список неполон')
            if status == 'name':
                found.append(oid)
        # Даже короткая страница не доказывает конец: CRM может ограничить LIMIT.
        if not rows:
            return found
    raise ValueError("Список сделок неполный; повторите чтение в STOCRM")


def status_offers(tools: Any, status_ids: tuple[int, int]) -> tuple[dict[int, int | None], bool, bool]:
    """Сделки всех контактов, с локальной проверкой каждой строки.

    Не полагаемся на серверные фильтры: в некоторых панелях они игнорируются.
    Непроверяемая строка или обрыв означает неполный список, а не отсутствие.
    """
    found: dict[int, int | None] = {}
    seen: set[int] = set()
    incomplete = False
    by_name = False
    for page in range(1, 21):
        raw = tools._read('quick_status_offers', 'offers/get_from_filter',
                          {'FILTER': {'BOARD_ID': status_ids[0]}, 'SORT': {'DATE_CREATE': 'DESC'}, 'PAGE': page, 'LIMIT': 500})
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (list, dict)):
            raise ValueError('CRM не подтвердила список сделок статуса')
        rows = _rows(raw)
        if isinstance(raw, dict) and raw and not rows and not any(isinstance(raw.get(k), list) and not raw[k] for k in ('DATA', 'ITEMS', 'LIST', 'ROWS')):
            raise ValueError('Неизвестный формат списка сделок статуса')
        if not rows:
            return found, incomplete, by_name
        for row in rows:
            try:
                oid = _positive(row.get('OFFER_ID'), 'Сделка')
            except ValueError:
                incomplete = True
                continue
            if oid in seen:
                raise ValueError('CRM повторяет строки или страницы сделок; список не подтверждён')
            seen.add(oid)
            matched = quick_status_name(tools, row.get('BOARD_ID'), row.get('STATUS_NAME'), status_ids)
            if matched == 'unknown':
                incomplete = True
            elif matched == 'name':
                try:
                    found[oid] = _positive(row.get('CONTACT_ID'), 'Контакт сделки')
                except ValueError:
                    found[oid] = None
                by_name = True
    return found, True, by_name


def status_detail(tools: Any, status_ids: tuple[int, int], oid: int, contact_id: int | None = None) -> str:
    card = tools.offer_card(oid, with_items=True)
    matched = quick_status_name(tools, card.get('board_id'), card.get('status'), status_ids)
    if card.get('error') or str(card.get('offer_id')) != str(oid) or matched != 'name':
        raise ValueError(f'Сделка №{oid}: номер или статус изменились либо не подтверждены')
    contact = str(card.get('contact') or 'имя не получено')
    current_contact = card.get('contact_id')
    contact_note = f' · контакт №{current_contact}' if str(current_contact or '').isdigit() else ''
    note = '\nℹ️ Статус сверен по названию, а не по ID. Только просмотр.'
    return f'📋 Быстрая сделка №{oid}\nКлиент: {contact}{contact_note}\n' + format_offer_items(card) + note


def status_details(tools: Any, status_ids: tuple[int, int], offers: dict[int, int | None]) -> list[str]:
    def one(item: tuple[int, int | None]) -> str:
        oid, contact_id = item
        try:
            return status_detail(tools, status_ids, oid, contact_id)
        except Exception:  # noqa: BLE001 — чужие детали не показываем
            return f'Сделка №{oid}: статус или подробности не удалось подтвердить; долг неизвестен.'
    items = list(offers.items())
    if len(items) <= 1:
        return [one(item) for item in items]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='quick-status-detail') as pool:
        return [future.result() for future in [pool.submit(copy_context().run, one, item) for item in items]]


def detail(tools: Any, ids: tuple[int, int, int], oid: int) -> str:
    return status_detail(tools, (ids[1], ids[2]), oid, ids[0])


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
