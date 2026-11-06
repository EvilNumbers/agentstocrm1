"""Быстрый и безопасный просмотр календаря без шагов языковой модели."""
from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from ..calendar import _zone
from ..client import read_budget

_REQUEST = re.compile(
    r"\s*(?:(?:покажи|покажите|выведи|выведите|дай|дайте)\s+)?"
    r"(?:календарь|расписание|запись|записи)(?:\s+в\s+календаре)?\s+"
    r"(?:на\s+)?(?P<day>сегодня|завтра|послезавтра|вчера|\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})\s*[.!?]*\s*", re.I)


def request(text: str) -> str | None:
    match = _REQUEST.fullmatch(text or '')
    return match.group('day').lower() if match else None


def _day(word: str, zone: dt.tzinfo) -> dt.date:
    offsets = {'вчера': -1, 'сегодня': 0, 'завтра': 1, 'послезавтра': 2}
    if word in offsets:
        return dt.datetime.now(zone).date() + dt.timedelta(days=offsets[word])
    if '.' in word:
        return dt.datetime.strptime(word, '%d.%m.%Y').date()
    return dt.date.fromisoformat(word)


def _clock(value: Any) -> str:
    if not value:
        return 'время не получено'
    try:
        return dt.datetime.fromisoformat(str(value)).strftime('%d.%m %H:%M')
    except ValueError:
        return 'время не получено'


def render(tools: Any, word: str) -> str:
    try:
        with read_budget(20):
            customers = tools.calendar_customers()
            customers = [c for c in customers if c.get('active') != 'N']
            if not customers:
                return 'Активные подразделения календаря не получены. Проверьте доступ в CRM.'
            if len(customers) > 20:
                return 'Слишком много подразделений для одного просмотра. Уточните подразделение календаря.'
            jobs: list[tuple[dict[str, Any], str, Any, Any]] = []
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='calendar-read') as pool:
                for customer in customers:
                    try:
                        day = _day(word, _zone(str(customer.get('timezone') or ''))).isoformat()
                        cid = int(customer['customer_id'])
                    except (ValueError, TypeError, KeyError):
                        jobs.append((customer, '', None, None))
                        continue
                    entries = pool.submit(copy_context().run, tools.calendar_entries, cid, day)
                    posts = pool.submit(copy_context().run, tools.calendar_posts, cid)
                    jobs.append((customer, day, entries, posts))
                lines = ['🗓 Календарь:']
                for customer, day, entries_future, posts_future in jobs:
                    label = str(customer.get('name') or f"подразделение №{customer.get('customer_id')}")
                    if entries_future is None:
                        lines.append(f'\n{label}: не удалось определить дату и часовой пояс подразделения.')
                        continue
                    lines.append(f'\n{label} · {day} ({customer.get("timezone")}):')
                    try:
                        data = entries_future.result()
                        entries = data['entries']
                    except Exception:  # ошибка не означает пустой календарь
                        lines.append('⚠️ Записи не удалось получить или проверить. Проверьте календарь в CRM.')
                        continue
                    try:
                        posts = posts_future.result()
                        names = {str(p['post_id']): str(p.get('name') or '') for p in posts if p.get('post_id')}
                    except Exception:
                        names = {}
                        lines.append('⚠️ Названия постов не получены; показаны номера.')
                    if not entries:
                        lines.append('В полученном ответе записей нет.')
                        continue
                    for row in sorted(entries, key=lambda r: (r.get('start') is None, r.get('start') or 0, r['card_id'])):
                        post = names.get(str(row['post_id'])) or f"пост №{row['post_id']}"
                        # Номер календарной записи в обычном просмотре не показываем:
                        # человеку он не нужен для решения. Номер записи остаётся в
                        # данных и используется для действий, номер СДЕЛКИ сохранён —
                        # по нему менеджер и открывает карточку.
                        bits = [f"{_clock(row.get('start_local'))} — {_clock(row.get('end_local'))}", post]
                        if row.get('offer_id'):
                            bits.append(f"сделка №{row['offer_id']}")
                        for field in ('client_name', 'car_title', 'comment'):
                            if row.get(field):
                                bits.append(' '.join(str(row[field]).split())[:300])
                        if row.get('deactivate') == 'Y':
                            bits.append('деактивирована')
                        elif row.get('deactivate') not in ('N', 'Y'):
                            bits.append('активность не подтверждена')
                        lines.append('• ' + ' · '.join(bits))
                lines.append('\nПоказаны данные, возвращённые API за выбранную дату; полнота не гарантирована.')
                return '\n'.join(lines)
    except Exception:
        return 'Не удалось получить подразделения или записи календаря. Проверьте календарь в CRM; пустым его не считаю.'
