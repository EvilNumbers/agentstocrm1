"""Безопасная составная запись быстрых сделок после одного подтверждения."""
from __future__ import annotations

import math
import copy
import time
from typing import Any

from .booking import _id_from, key
from .calendar import _positive, _rows
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


def positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    work = payload.get('work')
    return ([{'kind': 'work', 'title': str(work.get('title') or ''), 'price': work.get('price')}] if work else []) + [
        {'kind': 'article', 'title': str(item.get('sku') or ''), 'brand': item.get('brand'),
         'price': item.get('price'), 'qty': item.get('qty')} for item in payload.get('articles') or []]


def summary_for(payload: dict[str, Any]) -> str:
    oid, reason = payload.get('offer_id'), payload.get('reason')
    summary = (f'Создать новую быструю сделку для {quick_read.NAME} ({quick_read.PHONE})' if not oid
               else f'Добавить в быструю сделку №{oid}')
    summary += f'; воронка №{payload["board_id"]}, статус «Быстрые сделки»'
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
    policy = ('Системный контакт передаётся при создании; фактический контакт сделки не сверяется. После перевода статус проверяется по названию; затем позиции добавляются по номеру сделки, даже если статус или контакт изменится.'
              if not oid else 'Статус выбранной сделки проверяется по названию; затем позиции добавляются по её номеру, даже если статус или контакт изменится.')
    return summary + '\n' + '\n'.join(rows) + '\n' + policy + '\nСвободные позиции без заказа поставщику.'


def drop_position(tools: Any, token: str, *, number: int | None, query: str | None) -> dict[str, Any]:
    """Пересоздать ожидающий черновик атомарно; старые кнопки не применяют новую версию."""
    store = tools.drafts
    with store._locked():
        draft = store._verify_unlocked(token)
        if draft.get('tool') != 'quick_deal' or draft.get('method') != 'quick_deal' or draft.get('steps'):
            raise ValueError('Состав черновика не подтверждён')
        payload = copy.deepcopy(draft['payload'])
        items = positions(payload)
        if number is not None:
            index = number - 1 if 1 <= number <= len(items) else None
        else:
            needle = key(query)
            matches = [i for i, item in enumerate(items) if needle and needle in key(item['title'])]
            index = matches[0] if len(matches) == 1 else None
        if index is None:
            raise ValueError('Укажите номер одной позиции из последнего черновика')
        removed = items[index]
        if removed['kind'] == 'work':
            payload['work'] = None
        else:
            del payload['articles'][index - (1 if payload.get('work') else 0)]
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


def prepare(tools: Any, ids: tuple[int, int, int], offer_id: int | None,
            work: dict[str, Any] | None, articles: list[dict[str, Any]], reason_id: int | None = None) -> dict[str, Any]:
    if (quick_read.identity(tools) != ids if not offer_id else quick_read.status_identity(tools)[0] != ids[1]):
        raise ValueError('Контакт создания или воронка статуса изменились')
    if not work and not articles and offer_id:
        raise ValueError('Назовите работу или артикул')
    if work and not str(work.get('title') or '').strip():
        raise ValueError('Название работы не указано')
    if offer_id:
        _assert_offer(tools, ids, offer_id)
    for item in ([work] if work else []) + articles:
        price = item.get('price')
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError('Для каждой работы и запчасти нужна положительная цена')
    for item in articles:
        if not item.get('sku') or not parts.known_brand(item.get('brand')) or type(item.get('qty')) is not int or not 1 <= item['qty'] <= 9999:
            raise ValueError('Артикул, бренд и количество не подтверждены')
    reason = _status_reason(tools, ids, reason_id) if not offer_id else None
    payload = {'contact_id': ids[0], 'board_id': ids[1], 'status_id': ids[2],
               'offer_id': offer_id, 'work': work, 'articles': articles, 'reason': reason,
               'confirmation_policy': 'status_name_offer_number_v110'}
    summary = summary_for(payload)
    # Атомарный запрет на параллельные / непроверенные составные операции.
    store = tools.drafts
    with store._locked():
        store._expire_unlocked()
        for other in store._data.values():
            if other.get('tool') == 'quick_deal' and (other.get('status') in ('pending', 'applying', 'partial') or other.get('archived_partial')):
                raise ValueError('Уже есть незавершённая быстрая сделка в /drafts. Проверьте её; повтор запрещён')
            check = other.get('check') or {}
            if check.get('kind') == 'standalone_offer' and str(check.get('contact_id')) == str(ids[0]) and (other.get('status') in ('pending', 'applying', 'partial') or other.get('archived_partial')):
                raise ValueError('По системному контакту есть незавершённое создание сделки. Проверьте /drafts')
        return dict(store._create_unlocked(tool='quick_deal', method='quick_deal', payload=payload,
                                          summary=summary, risk='high', steps=None, extra={'check': {'kind': 'quick_deal'}}))


def apply(tools: Any, token: str, current: dict[str, Any]) -> dict[str, Any]:
    # validate fingerprint before consulting any payload or doing I/O
    current = tools.drafts.verify(token)
    p = current['payload']
    if p.get('confirmation_policy') != 'status_name_offer_number_v110':
        raise ValueError('Старый черновик не содержит новых условий проверки. Если запись не отправлялась, отмените его и создайте новый; частичную запись не повторяйте')
    if any(not parts.known_brand(item.get('brand')) for item in p.get('articles') or []):
        raise ValueError('В старом черновике не указан бренд. Отмените его, если запись не отправлялась, и укажите бренд вручную; CRM не изменена')
    ids = (int(p['contact_id']), int(p['board_id']), int(p['status_id']))
    oid = p.get('offer_id')
    identity_changed = (quick_read.identity(tools) != ids if not oid else quick_read.status_identity(tools)[0] != ids[1])
    if current.get('method') != 'quick_deal' or current.get('steps') or identity_changed:
        raise ValueError('Контакт создания, воронка или состав черновика изменились; запись не начата')
    prior = set()
    if oid:
        _assert_offer(tools, ids, int(oid))
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
    try:
        def mark(message: str) -> None:
            nonlocal stage
            stage = message
            tools.drafts.mark(token, 'applying', quick_stage=stage, quick_touched_at=time.time(), result=dict(result))
        if not oid:
            mark('создание сделки отправляется')
            response = tools.client.call('offer/new/with_contact', {'PHONE': quick_read.PHONE, 'CONTACT_ID': ids[0], 'BOARD_ID': ids[1]})
            oid = _positive(response, 'Сделка') if isinstance(response, (int, str)) and not isinstance(response, bool) else _id_from(response, 'OFFER_ID')
            result['OFFER_ID'] = oid
            mark('получен номер сделки')
            if oid in prior:
                raise ValueError('CRM вернула ID существовавшей сделки')
            tools._invalidate_cache()
            row, _ = tools._search_offer_row(oid)
            if not row or str(row.get('OFFER_ID')) != str(oid) or str(row.get('BOARD_ID')) != str(ids[1]):
                raise ValueError('Номер или воронка новой сделки не подтверждены')
            if quick_read.status_identity(tools) != ids[1:]:
                raise ValueError('Статус в справочнике изменился')
            result['completed'].append('создание сделки')
            status = {'OFFER_ID': oid, 'BOARD_ID': ids[1], 'STATUS_ID': ids[2]}
            if p.get('reason'):
                status['REASON_ID'] = p['reason']['id']
            if _status_reason(tools, ids, (p.get('reason') or {}).get('id')) != p.get('reason'):
                raise ValueError('Причина статуса изменилась')
            mark('смена статуса отправляется')
            tools.client.call('offer/status/change', status)
            _assert_offer(tools, ids, oid, after_status_change=True)
            result['completed'].append('статус «Быстрые сделки»')
            mark('статус подтверждён')
        oid = int(oid)
        if p.get('work'):
            _assert_offer(tools, ids, oid, number_only=True)
            work = p['work']
            before = tools.offer_works(oid, limit=200)
            if len(before) >= 200:
                raise ValueError('Список работ неполон')
            known = {str(w.get('work_id')) for w in before if w.get('work_id')}
            data = {'OFFER_ID': oid, 'TYPE': tools.free_form_type, 'DESCRIPTION': work['title'], 'TITLE': work['title'], 'WORKING_HOUR': 1.0, 'SUM': float(work['price']), 'PRICE_OF_HOUR': float(work['price'])}
            mark('добавление работы отправляется')
            response = tools.client.call('work/add', {'DATA': data})
            wid = _id_from(response, 'WORK_ID')
            result['WORK_ID'] = wid
            mark('проверка работы')
            if str(wid) in known:
                raise ValueError('CRM вернула номер существовавшей работы')
            # _find_work / _verify_work_sum сами сверяют работу с номером сделки.
            check = {'offer_id': oid, 'work_id': wid, 'title': work['title'], 'sum': work['price']}
            row = tools._find_work(check)
            if not row or key(row.get('title')) != key(work['title']):
                raise ValueError('Название новой работы не подтверждено')
            problem = tools._verify_work_sum(check)
            if problem:
                raise ValueError(problem)
            if check.get('actual_sum') is None or abs(float(check['actual_sum']) - float(work['price'])) >= 0.01:
                raise ValueError('Стоимость работы отличается от подтверждённой суммы')
            result['completed'].append(f'работа «{work["title"]}»')
            mark('работа подтверждена')
        for item in p.get('articles') or []:
            _assert_offer(tools, ids, oid, number_only=True)
            tools.drafts.assert_part_unblocked({'offer_id': oid, 'sku': item['sku'], 'brand': item['brand']})
            before, problem = tools._offer_parts_details(oid)
            if problem:
                raise ValueError('Список товаров до записи неполон')
            known = {str(r.get('order_id')) for r in before if r.get('order_id')}
            mark(f'искомый артикул {item["sku"]}: запрос отправляется')
            response = tools.client.call('offer/require/add', {'OFFER_ID': oid, 'TITLE': item['sku'], 'BRAND': item['brand'], 'SKU': item['sku']})
            rid = _id_from(response, 'REQUIRE_ID')
            result.setdefault('REQUIRE_IDS', []).append(rid)
            mark(f'артикул {item["sku"]}: получен номер искомой позиции')
            # Нельзя отправлять свободную строку в чужую искомую запчасть.
            if _id_from(response, 'OFFER_ID') != oid:
                raise ValueError('CRM не подтвердила, что искомый артикул принадлежит этой сделке')
            mark(f'артикул {item["sku"]}: искомая позиция подтверждена ответом CRM')
            _assert_offer(tools, ids, oid, number_only=True)
            mark(f'свободная позиция {item["sku"]}: запрос отправляется')
            response = tools.client.call('offer/order/add_free_order', {'REQUIRE_ID': rid, 'NAME': item['sku'], 'BRAND': item['brand'], 'SKU': item['sku'], 'PRICE': float(item['price']), 'NEED_MARKUP': False, 'AMOUNT': item['qty'], 'CURRENT_VOLUME': item['qty'], 'MIN_VOLUME': 1})
            order_id = _id_from(response, 'ORDER_ID')
            result.setdefault('ORDER_IDS', []).append(order_id)
            mark(f'проверка позиции {item["sku"]}')
            if str(order_id) in known:
                raise ValueError('CRM вернула номер существовавшей позиции')
            # Проверка добавленной строки ниже сверяет её принадлежность сделке.
            check = {'offer_id': oid, 'sku': item['sku'], 'name': item['sku'], 'brand': item['brand'], 'price': item['price'], 'qty': item['qty'], 'order_id': order_id}
            info = tools._check_added_line(check)
            if info.get('error') or info.get('problems'):
                raise ValueError(str(info.get('error') or info.get('problems')))
            result['completed'].append(f'артикул {item["sku"]}')
            mark(f'позиция {item["sku"]} подтверждена')
        result['SUMMARY'] = f'Быстрая сделка №{oid}: ' + ', '.join(result['completed'])
        tools.drafts.mark(token, 'applied', applied_at=time.time(), result=result)
        return {'status': 'applied', 'token': token, 'method': 'quick_deal', 'result': result, 'verified': True}
    except BaseException as exc:
        warning = (f'Быстрая сделка выполнена не полностью. Этап: {stage}. Известные данные: {result}. '
                   f'Причина: {exc}. Проверьте CRM и /drafts; автоматический повтор запрещён.')
        tools.drafts.mark(token, 'partial', failed_at=time.time(), result=result, check_warning=warning, done_steps={'methods': result['completed'], 'error': str(exc)})
        if not isinstance(exc, Exception):
            raise
        return {'status': 'partial', 'token': token, 'method': 'quick_deal', 'result': result, 'warning': warning, 'verified': False}
