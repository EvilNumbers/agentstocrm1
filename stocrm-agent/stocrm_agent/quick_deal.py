"""Безопасная составная запись быстрых сделок после одного подтверждения."""
from __future__ import annotations

import math
import copy
import time
from collections import Counter
from typing import Any

from .booking import _id_from, key
from .calendar import _positive, _rows
from .client import _as_items
from .bot import quick_read
from . import parts


def status_reasons(tools: Any, ids: tuple[int, int, int]) -> list[dict[str, Any]]:
    found = [s for s in tools.statuses(ids[1]) if str(s.get('status_id')) == str(ids[2]) and str(s.get('board_id')) == str(ids[1])]
    if len(found) != 1:
        raise ValueError('Статус больше не подтверждён')
    reasons = found[0].get('reasons') or []
    result = [{'id': _positive(r.get('reason_id'), 'Причина'), 'title': str(r.get('title') or r.get('reason_id'))} for r in reasons]
    if len({r['id'] for r in result}) != len(result):
        raise ValueError('Причины статуса неоднозначны')
    return result


def _status_reason(tools: Any, ids: tuple[int, int, int], selected: int | None = None) -> dict[str, Any] | None:
    reasons = status_reasons(tools, ids)
    if not reasons and selected is None:
        return None
    if len(reasons) == 1 and selected is None:
        return reasons[0]
    matches = [r for r in reasons if str(r['id']) == str(selected)]
    if len(matches) != 1:
        raise ValueError('Выберите причину перехода в статус')
    return matches[0]


def _all_offer_ids(tools: Any) -> set[int]:
    seen: set[int] = set()
    for page in range(1, 21):
        raw = tools._read('quick_prior_offers', 'offers/get_from_filter', {'FILTER': {}, 'SORT': {'DATE_CREATE': 'DESC'}, 'PAGE': page, 'LIMIT': 500})
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (list, dict)):
            raise ValueError('Не удалось проверить прежние сделки')
        rows = _rows(raw)
        if isinstance(raw, dict) and raw and not rows and not any(isinstance(raw.get(k), list) and not raw[k] for k in ('DATA', 'ITEMS', 'LIST', 'ROWS')):
            raise ValueError('Неизвестный формат списка прежних сделок; запись не начата')
        if not rows:
            return seen
        for row in rows:
            oid = _positive(row.get('OFFER_ID'), 'Сделка')
            if oid in seen:
                raise ValueError('CRM повторяет страницы сделок')
            seen.add(oid)
    raise ValueError('Список прежних сделок неполный')


def _assert_offer(tools: Any, ids: tuple[int, int, int], oid: int, *,
                  after_status_change: bool = False, number_only: bool = False) -> None:
    """Проверяет номер; при выборе и переводе дополнительно — название статуса."""
    delays = (0.0, 0.5, 1.0, 2.0) if after_status_change else (0.0,)
    problem = ''
    for delay in delays:
        if delay:
            time.sleep(delay)
        tools._invalidate_cache()
        if not number_only and quick_read.status_identity(tools)[0] != ids[1]:
            raise ValueError('Воронка статуса в справочнике изменилась')
        row, _ = tools._search_offer_row(oid)
        if not row or str(row.get('OFFER_ID')) != str(oid):
            problem = 'сделка не найдена или номер не совпал'
        elif number_only:
            return
        elif quick_read.quick_status_name(tools, row.get('BOARD_ID'), row.get('STATUS_NAME'), ids[1:]) == 'name':
            return
        else:
            problem = 'воронка или название статуса «Быстрые сделки» не подтверждены'
    raise ValueError(f'Сделка №{oid}: {problem}')


def _work_snapshot(tools: Any, oid: int) -> Counter[tuple[str, float]]:
    """Список работ по номеру сделки; номера самих работ не нужны."""
    from .tools import _known_work_sum
    found: Counter[tuple[str, float]] = Counter()
    seen_on_previous_pages: set[tuple[int, str, float]] = set()
    seen_count = 0
    for page in range(1, 21):
        raw = tools._read('quick_work_snapshot', 'work/get_filtered',
                          {'FILTER': {'OFFER_ID': oid}, 'SORT': {'WORK_ID': 'ASC'}, 'PAGE': page, 'LIMIT': 200})
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (dict, list)) or raw == {}:
            raise ValueError('CRM не подтвердила полный список работ')
        rows = _as_items(raw)
        if not rows:
            return found
        page_signatures: set[tuple[int, str, float]] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Неизвестный формат списка работ')
            owner = _positive(row.get('OFFER_ID'), 'Сделка работы')
            title = key(row.get('TITLE') or row.get('DOCUMENT_TITLE'))
            amount = _known_work_sum(row.get('SUM'))
            if not title or amount is None:
                raise ValueError('Название или сумма работы не получены; список не подтверждён')
            signature = (owner, title, amount)
            # Без номера работы одинаковые строки на разных страницах нельзя отличить
            # от повторной страницы: останавливаемся, не угадываем.
            if signature in seen_on_previous_pages:
                raise ValueError('Строки работ повторяются на разных страницах; список не подтверждён')
            page_signatures.add(signature)
            seen_count += 1
            if seen_count > 2000:
                raise ValueError('Список работ слишком большой; проверка не завершена')
            if owner == oid:
                found[(title, amount)] += 1
        seen_on_previous_pages.update(page_signatures)
    raise ValueError('Список работ неполный; проверка не завершена')


def _confirm_new_work(tools: Any, oid: int, before: Counter[tuple[str, float]],
                      work: dict[str, Any]) -> None:
    """Повторяет только чтение. Сравнение не доказывает авторство при параллельной записи."""
    wanted = (key(work['title']), round(float(work['price']), 2))
    for attempt, delay in enumerate((0.0, 0.5, 1.0, 2.0)):
        if delay:
            time.sleep(delay)
        after = _work_snapshot(tools, oid)
        added = after - before
        _diagnose(tools, 'quick.check', phase='work_check', offer_id=oid, attempt=attempt,
                  before_count=sum(before.values()), after_count=sum(after.values()),
                  added_count=sum(added.values()), removed_count=sum((before - after).values()),
                  verified=added == Counter({wanted: 1}) and not (before - after))
        if before - after:
            raise ValueError('Прежний список работ изменился; результат не подтверждён')
        if not added:
            continue
        if sum(added.values()) != 1:
            raise ValueError('Обнаружено несколько новых работ; результат неоднозначен')
        if added != Counter({wanted: 1}):
            raise ValueError('Название или сумма новой работы не подтверждены')
        return
    raise ValueError('Новая работа не обнаружена при контрольном чтении')


def _works(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return ([payload['work']] if payload.get('work') else []) + list(payload.get('extra_works') or [])


def positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{'kind': 'work', 'title': str(work.get('title') or ''), 'price': work.get('price')} for work in _works(payload)] + [
        {'kind': 'article', 'title': str(item.get('sku') or ''), 'brand': item.get('brand'),
         'price': item.get('price'), 'qty': item.get('qty'), 'product_hint': item.get('product_hint')} for item in payload.get('articles') or []]


def summary_for(payload: dict[str, Any]) -> str:
    oid, reason = payload.get('offer_id'), payload.get('reason')
    summary = (f'Создать новую быструю сделку для {quick_read.NAME} ({quick_read.PHONE})' if not oid
               else f'Добавить в сделку №{oid} по указанному номеру')
    if not oid:
        summary += f'; воронка №{payload["board_id"]}, перевод в статус «Быстрые сделки»'
    if reason:
        summary += f'; причина перехода «{reason["title"]}»'
    rows = []
    for i, item in enumerate(positions(payload), 1):
        if item['kind'] == 'work':
            rows.append(f'{i}. Работа «{item["title"]}» — {item["price"]:g} ₽')
        else:
            rows.append(f'{i}. Артикул {item["title"]}, бренд {item["brand"]} — {item["price"]:g} ₽ × {item["qty"]}')
    if not rows:
        rows.append('Без услуг и товаров.')
    policy = ('Системный контакт передаётся при создании; после ответа CRM на перевод позиции добавляются по номеру сделки без проверки текущего статуса, контакта и воронки, даже если статус или контакт изменится. Фактический перевод и контакт не сверяются.'
              if not oid else 'Проверьте номер: позиции добавляются в указанную сделку без проверки её текущего статуса, контакта и воронки, даже если они изменились.')
    if _works(payload):
        policy += (' Бот сравнит работы сделки по названию и сумме до и после записи; номер работы не требуется. '
                   'Не меняйте работы сделки одновременно: совпадающую чужую работу невозможно отличить.')
    return summary + '\n' + '\n'.join(rows) + '\n' + policy + '\n🔎 Перед подтверждением проверьте номер сделки, каждый артикул, бренд, количество и цену: распознавание речи может ошибаться.\nСвободные позиции без заказа поставщику.'



def _position_index(items: list[dict[str, Any]], *, number: int | None, query: str | None) -> int:
    if number is not None:
        if 1 <= number <= len(items):
            return number - 1
        raise ValueError('Укажите существующий номер позиции в черновике')
    needle = key(query)
    if not needle:
        raise ValueError('Укажите номер или название одной позиции')
    if needle in {'товар', 'товара', 'товару', 'товары', 'артикул', 'артикула', 'артикулу', 'запчасть', 'запчасти'}:
        candidates = [i for i, item in enumerate(items) if item['kind'] == 'article']
    elif needle in {'фильтр', 'фильтра', 'фильтру', 'фильтров', 'фильтры'}:
        candidates = [i for i, item in enumerate(items) if item['kind'] == 'article' and str(item.get('product_hint') or '').startswith('фильтр')]
    elif needle in {'услуга', 'услугу', 'услуги', 'работа', 'работу', 'работы'}:
        candidates = [i for i, item in enumerate(items) if item['kind'] == 'work']
    else:
        candidates = [i for i, item in enumerate(items) if needle in key(item['title'])]
        # Русские окончания в устной форме: диагностика / диагностику.
        if not candidates and len(needle) >= 5 and needle[-1] in 'ауыеоиюя':
            stem = needle[:-1]
            candidates = [i for i, item in enumerate(items) if key(item['title']).startswith(stem)]
    if len(candidates) != 1:
        raise ValueError('Позиция не найдена или их несколько; назовите её номер или точный артикул')
    return candidates[0]


def edit_position(tools: Any, token: str, *, number: int | None = None, query: str | None = None,
                  price: float | None = None, qty: int | None = None) -> dict[str, Any]:
    """Только ожидающий составной черновик: атомарно заменить, старую кнопку отключить."""
    if (price is None) == (qty is None):
        raise ValueError('Укажите одну новую цену или количество')
    if price is not None and (isinstance(price, bool) or not math.isfinite(price) or price <= 0):
        raise ValueError('Цена должна быть положительной')
    if qty is not None and (type(qty) is not int or not 1 <= qty <= 9999):
        raise ValueError('Количество должно быть целым от 1 до 9999')
    store = tools.drafts
    with store._locked():
        draft = store._verify_unlocked(token)
        if draft.get('tool') != 'quick_deal' or draft.get('method') != 'quick_deal' or draft.get('steps'):
            raise ValueError('Изменить можно только ожидающий составной черновик')
        payload = copy.deepcopy(draft['payload'])
        items = positions(payload)
        index = _position_index(items, number=number, query=query)
        payload['confirmation_policy'] = 'multi_work_v119'
        all_works = _works(payload)
        if index < len(all_works):
            if qty is not None:
                raise ValueError('Количество можно изменить только у товара')
            target = payload['work'] if index == 0 and payload.get('work') else payload['extra_works'][index - (1 if payload.get('work') else 0)]
        else:
            target = payload['articles'][index - len(all_works)]
        if price is not None:
            target['price'] = float(price)
        else:
            target['qty'] = qty
        if any(not parts.sku_is_latin(item.get('sku')) for item in payload.get('articles') or []):
            raise ValueError('В старом черновике артикул не латиницей; отмените его без попыток записи и подготовьте заново')
        # _create_unlocked сохраняет файл: старый черновик закрываем ДО его сохранения.
        draft.update(status='discarded', reason='состав изменён человеком; старая кнопка недействительна')
        updated = store._create_unlocked(tool='quick_deal', method='quick_deal', payload=payload,
                    summary=summary_for(payload), risk='high', steps=None, extra={'check': {'kind': 'quick_deal'}})
        draft['replacement_token'] = updated['token']
        store._save()
        return {'status': 'pending', 'token': updated['token'], 'draft': dict(updated),
                'position': items[index]['title'], 'number': index + 1}

def drop_position(tools: Any, token: str, *, number: int | None, query: str | None) -> dict[str, Any]:
    """Пересоздать ожидающий черновик атомарно; старые кнопки не применяют новую версию."""
    store = tools.drafts
    with store._locked():
        draft = store._verify_unlocked(token)
        if draft.get('tool') != 'quick_deal' or draft.get('method') != 'quick_deal' or draft.get('steps'):
            raise ValueError('Состав черновика не подтверждён')
        payload = copy.deepcopy(draft['payload'])
        items = positions(payload)
        index = _position_index(items, number=number, query=query)
        removed = items[index]
        all_works = _works(payload)
        if removed['kind'] == 'work':
            del all_works[index]
            payload['work'] = all_works[0] if all_works else None
            payload['extra_works'] = all_works[1:]
        else:
            del payload['articles'][index - len(all_works)]
        if any(not parts.sku_is_latin(item.get('sku')) for item in payload.get('articles') or []):
            raise ValueError('В старом черновике артикул не латиницей; отмените его без попыток записи и подготовьте заново')
        draft.update(status='discarded', reason='состав изменён человеком; старая кнопка недействительна')
        if not positions(payload):
            store._save()
            return {'status': 'discarded', 'token': token, 'left': 0, 'removed': removed['title']}
        updated = store._create_unlocked(tool='quick_deal', method='quick_deal', payload=payload,
                    summary=summary_for(payload), risk='high', steps=None, extra={'check': {'kind': 'quick_deal'}})
        draft['replacement_token'] = updated['token']
        store._save()
        return {'status': 'pending', 'token': updated['token'], 'left': len(positions(payload)),
                'removed': removed['title'], 'draft': dict(updated)}


def _diagnose(tools: Any, event: str, **fields: Any) -> None:
    """Только диагностический журнал; отказ записи не влияет на сделку."""
    try:
        diagnostic = getattr(tools.audit, 'diagnostic', None)
        if diagnostic is not None:
            diagnostic.audit(event, fields)
    except Exception:
        pass


def prepare(tools: Any, ids: tuple[int, int, int], offer_id: int | None,
            work: dict[str, Any] | None, articles: list[dict[str, Any]], reason_id: int | None = None,
            extra_works: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not offer_id and quick_read.identity(tools) != ids:
        raise ValueError('Контакт создания или воронка статуса изменились')
    all_works = ([work] if work else []) + list(extra_works or [])
    if len(all_works) > 10:
        raise ValueError('Слишком много услуг в одном запросе')
    if not all_works and not articles and offer_id:
        raise ValueError('Назовите работу или артикул')
    if any(not str(item.get('title') or '').strip() for item in all_works):
        raise ValueError('Название работы не указано')
    if offer_id:
        _assert_offer(tools, ids, offer_id, number_only=True)
    for item in all_works + articles:
        price = item.get('price')
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError('Для каждой работы и запчасти нужна положительная цена')
    articles = [dict(item, sku=parts.sku_for_write(item.get('sku'))) for item in articles]
    for item in articles:
        if not item.get('sku') or not parts.known_brand(item.get('brand')) or type(item.get('qty')) is not int or not 1 <= item['qty'] <= 9999:
            raise ValueError('Артикул, бренд и количество не подтверждены')
    reason = _status_reason(tools, ids, reason_id) if not offer_id else None
    payload = {'contact_id': ids[0], 'board_id': ids[1], 'status_id': ids[2],
               'offer_id': offer_id, 'work': work, 'extra_works': list(extra_works or []), 'articles': articles, 'reason': reason,
               'confirmation_policy': 'multi_work_v119'}
    summary = summary_for(payload)
    # Атомарный запрет на параллельные / непроверенные составные операции.
    store = tools.drafts
    with store._locked():
        store._expire_unlocked()
        for other in store._data.values():
            if other.get('tool') == 'quick_deal' and (other.get('status') in ('pending', 'applying', 'partial') or other.get('archived_partial')):
                raise ValueError('Уже есть незавершённая составная операция в /drafts. Проверьте её; повтор запрещён')
            check = other.get('check') or {}
            if check.get('kind') == 'standalone_offer' and str(check.get('contact_id')) == str(ids[0]) and (other.get('status') in ('pending', 'applying', 'partial') or other.get('archived_partial')):
                raise ValueError('По системному контакту есть незавершённое создание сделки. Проверьте /drafts')
        draft = dict(store._create_unlocked(tool='quick_deal', method='quick_deal', payload=payload,
                                          summary=summary, risk='high', steps=None, extra={'check': {'kind': 'quick_deal'}}))
    _diagnose(tools, 'quick.draft', token=draft['token'], offer_id=offer_id,
              source='existing' if offer_id else 'new', draft_count=len(positions(payload)), tool='quick_deal')
    return draft


def apply(tools: Any, token: str, current: dict[str, Any]) -> dict[str, Any]:
    # validate fingerprint before consulting any payload or doing I/O
    current = tools.drafts.verify(token)
    p = current['payload']
    if (p.get('confirmation_policy') not in {'deal_number_only_v117', 'multi_work_v119'} or
            (p.get('confirmation_policy') == 'deal_number_only_v117' and p.get('extra_works'))):
        raise ValueError('Старый черновик не содержит новых условий проверки. Если запись не отправлялась, отмените его и создайте новый; частичную запись не повторяйте')
    if any(not parts.known_brand(item.get('brand')) for item in p.get('articles') or []):
        raise ValueError('В старом черновике не указан бренд. Отмените его, если запись не отправлялась, и укажите бренд вручную; CRM не изменена')
    if any(not parts.sku_is_latin(item.get('sku')) for item in p.get('articles') or []):
        raise ValueError('В старом черновике артикул не латиницей. Если запись не отправлялась, отмените его и создайте новый; частичную запись не повторяйте')
    ids = (int(p['contact_id']), int(p['board_id']), int(p['status_id']))
    oid = p.get('offer_id')
    identity_changed = quick_read.identity(tools) != ids if not oid else False
    if current.get('method') != 'quick_deal' or current.get('steps') or identity_changed:
        raise ValueError('Контакт создания, воронка или состав черновика изменились; запись не начата')
    prior = set()
    if oid:
        _assert_offer(tools, ids, int(oid), number_only=True)
        tools._booking_uncertain_offer(int(oid))
    else:
        if _status_reason(tools, ids, (p.get('reason') or {}).get('id')) != p.get('reason'):
            raise ValueError('Причина перехода изменилась; подготовьте черновик заново')
        tools._booking_uncertain_client(quick_read.PHONE, ids[0])
        prior = _all_offer_ids(tools)
    claimed = tools.drafts.claim(token)
    if claimed.get('payload') != p or claimed.get('method') != 'quick_deal' or claimed.get('steps'):
        tools.drafts.release(token, reason='Черновик изменился во время проверки')
        raise ValueError('Черновик изменился во время проверки; запись не начата')
    result: dict[str, Any] = {'OFFER_ID': oid, 'completed': []}
    if not oid:
        result['REQUESTED_CONTACT_ID'] = ids[0]
    stage = 'подготовка'
    phase = 'prepare'
    try:
        def mark(message: str, code: str) -> None:
            nonlocal stage, phase
            stage, phase = message, code
            tools.drafts.mark(token, 'applying', quick_stage=stage, quick_touched_at=time.time(), result=dict(result))
            _diagnose(tools, 'quick.stage', token=token, phase=phase, offer_id=oid,
                      completed_count=len(result['completed']), result=result)
        if not oid:
            mark('создание сделки отправляется', 'create_sent')
            response = tools.client.call('offer/new/with_contact', {'PHONE': quick_read.PHONE, 'CONTACT_ID': ids[0], 'BOARD_ID': ids[1]})
            oid = _positive(response, 'Сделка') if isinstance(response, (int, str)) and not isinstance(response, bool) else _id_from(response, 'OFFER_ID')
            result['OFFER_ID'] = oid
            mark('получен номер сделки', 'created')
            if oid in prior:
                raise ValueError('CRM вернула ID существовавшей сделки')
            tools._invalidate_cache()
            row, _ = tools._search_offer_row(oid)
            if not row or str(row.get('OFFER_ID')) != str(oid):
                raise ValueError('Номер новой сделки не подтверждён')
            if quick_read.status_identity(tools) != ids[1:]:
                raise ValueError('Статус в справочнике изменился')
            result['completed'].append('создание сделки')
            status = {'OFFER_ID': oid, 'BOARD_ID': ids[1], 'STATUS_ID': ids[2]}
            if p.get('reason'):
                status['REASON_ID'] = p['reason']['id']
            if _status_reason(tools, ids, (p.get('reason') or {}).get('id')) != p.get('reason'):
                raise ValueError('Причина статуса изменилась')
            mark('смена статуса отправляется', 'status_sent')
            status_response = tools.client.call('offer/status/change', status)
            if (status_response is False or
                    (isinstance(status_response, (int, float)) and not isinstance(status_response, bool)
                     and status_response <= 0) or
                    (isinstance(status_response, dict) and
                     (status_response.get('ERROR') or status_response.get('error') or
                      status_response.get('success') is False or status_response.get('SUCCESS') is False))):
                raise ValueError('CRM вернула отказ на запрос перевода статуса')
            # Ответ без ошибки не доказывает текущий статус: его могли сразу изменить.
            # Продолжение привязано исключительно к номеру, как согласовано с пользователем.
            result['completed'].append('запрос перевода в «Быстрые сделки» отправлен, ответ получен; текущий статус не проверялся')
            result['status_not_read_back'] = True
            mark('ответ на запрос перевода получен; текущий статус не проверяется', 'status_response')
        oid = int(oid)
        for work in _works(p):
            _assert_offer(tools, ids, oid, number_only=True)
            mark('проверка исходного списка работ', 'work_before')
            before = _work_snapshot(tools, oid)
            data = {'OFFER_ID': oid, 'TYPE': tools.free_form_type, 'DESCRIPTION': work['title'], 'TITLE': work['title'], 'WORKING_HOUR': 1.0, 'SUM': float(work['price']), 'PRICE_OF_HOUR': float(work['price'])}
            mark('добавление работы отправляется', 'work_sent')
            response = tools.client.call('work/add', {'DATA': data})
            if response is False or not (response is None or response is True or isinstance(response, (dict, list)) or
                                         (isinstance(response, (int, float)) and not isinstance(response, bool) and response > 0)):
                raise ValueError('Ответ CRM на добавление работы не подтверждён')
            if isinstance(response, dict) and 'OFFER_ID' in response and str(response['OFFER_ID']) != str(oid):
                raise ValueError('Ответ CRM указывает на другую сделку')
            mark('проверка работы по названию и сумме', 'work_check')
            _confirm_new_work(tools, oid, before, work)
            result['completed'].append(f'работа «{work["title"]}»')
            mark('работа подтверждена', 'work_confirmed')
        for item in p.get('articles') or []:
            _assert_offer(tools, ids, oid, number_only=True)
            tools.drafts.assert_part_unblocked({'offer_id': oid, 'sku': item['sku'], 'brand': item['brand']})
            before, problem = tools._offer_parts_details(oid)
            if problem:
                raise ValueError('Список товаров до записи неполон')
            known = {str(r.get('order_id')) for r in before if r.get('order_id')}
            mark(f'искомый артикул {item["sku"]}: запрос отправляется', 'require_sent')
            response = tools.client.call('offer/require/add', {'OFFER_ID': oid, 'TITLE': item['sku'], 'BRAND': item['brand'], 'SKU': item['sku']})
            rid = _id_from(response, 'REQUIRE_ID')
            result.setdefault('REQUIRE_IDS', []).append(rid)
            mark(f'артикул {item["sku"]}: получен номер искомой позиции', 'require_received')
            # Нельзя отправлять свободную строку в чужую искомую запчасть.
            if _id_from(response, 'OFFER_ID') != oid:
                raise ValueError('CRM не подтвердила, что искомый артикул принадлежит этой сделке')
            mark(f'артикул {item["sku"]}: искомая позиция подтверждена ответом CRM', 'require_confirmed')
            _assert_offer(tools, ids, oid, number_only=True)
            mark(f'свободная позиция {item["sku"]}: запрос отправляется', 'order_sent')
            response = tools.client.call('offer/order/add_free_order', {'REQUIRE_ID': rid, 'NAME': item['sku'], 'BRAND': item['brand'], 'SKU': item['sku'], 'PRICE': float(item['price']), 'NEED_MARKUP': False, 'AMOUNT': item['qty'], 'CURRENT_VOLUME': item['qty'], 'MIN_VOLUME': 1})
            order_id = _id_from(response, 'ORDER_ID')
            result.setdefault('ORDER_IDS', []).append(order_id)
            mark(f'проверка позиции {item["sku"]}', 'order_check')
            if str(order_id) in known:
                raise ValueError('CRM вернула номер существовавшей позиции')
            # Проверка добавленной строки ниже сверяет её принадлежность сделке.
            check = {'offer_id': oid, 'sku': item['sku'], 'name': item['sku'], 'brand': item['brand'], 'price': item['price'], 'qty': item['qty'], 'order_id': order_id}
            info = tools._check_added_line(check)
            _diagnose(tools, 'quick.check', token=token, phase='order_check', offer_id=oid,
                      ORDER_ID=order_id, problem_count=len(info.get('problems') or []),
                      verified=not bool(info.get('error') or info.get('problems')),
                      error=str(info.get('error') or ''))
            if info.get('error') or info.get('problems'):
                raise ValueError(str(info.get('error') or info.get('problems')))
            result['completed'].append(f'артикул {item["sku"]}')
            mark(f'позиция {item["sku"]} подтверждена', 'order_confirmed')
        result['SUMMARY'] = f'Сделка №{oid}: ' + ', '.join(result['completed'])
        tools.drafts.mark(token, 'applied', applied_at=time.time(), result=result)
        _diagnose(tools, 'quick.success', token=token, phase=phase, offer_id=oid, status='applied', completed_count=len(result['completed']), result=result)
        return {'status': 'applied', 'token': token, 'method': 'quick_deal', 'result': result, 'verified': True}
    except BaseException as exc:
        warning = (f'Составная операция по сделке выполнена не полностью. Этап: {stage}. Известные данные: {result}. '
                   f'Причина: {exc}. Проверьте CRM и /drafts; автоматический повтор запрещён.')
        tools.drafts.mark(token, 'partial', failed_at=time.time(), result=result, check_warning=warning, done_steps={'methods': result['completed'], 'error': str(exc)})
        _diagnose(tools, 'quick.partial', token=token, phase=phase, offer_id=oid, status='partial', error=str(exc), error_class=type(exc).__name__, completed_count=len(result['completed']), result=result)
        if not isinstance(exc, Exception):
            raise
        return {'status': 'partial', 'token': token, 'method': 'quick_deal', 'result': result, 'warning': warning, 'verified': False}
