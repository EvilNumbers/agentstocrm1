"""Очередь Telegram и долг: изолированные тесты, без живого Telegram и CRM."""
import asyncio

import pytest

from stocrm_agent.bot.app import start_fresh_polling
from stocrm_agent.bot.fastpath import try_answer
from test_status_lists_v99 import env, send  # noqa: F401
from test_quick_deal_v100 import QuickSession
from test_payment_wait_status_v105 import setup_waiting


def test_start_drops_offline_messages_and_callbacks_before_polling():
    calls = []
    class FakeBot:
        queue = ['старое сообщение', 'старая кнопка']
        async def delete_webhook(self, **kwargs):
            calls.append(('delete', kwargs))
            self.queue.clear()
            return True
    class FakeDispatcher:
        async def start_polling(self, bot):
            calls.append(('polling', None))
            bot.queue.append('новое сообщение после запуска')
            return list(bot.queue)
    bot = FakeBot()
    asyncio.run(start_fresh_polling(bot, FakeDispatcher()))
    assert calls == [('delete', {'drop_pending_updates': True}), ('polling', None)]
    assert bot.queue == ['новое сообщение после запуска']


@pytest.mark.parametrize('failure', [False, RuntimeError('telegram unavailable')])
def test_failed_queue_clear_never_starts_polling(failure):
    calls = []
    class FakeBot:
        async def delete_webhook(self, **kwargs):
            if isinstance(failure, Exception):
                raise failure
            return failure
    class FakeDispatcher:
        async def start_polling(self, bot):
            calls.append('polling')
    with pytest.raises(RuntimeError):
        asyncio.run(start_fresh_polling(FakeBot(), FakeDispatcher()))
    assert calls == []


def test_waiting_payment_displays_own_checked_debt_and_preserves_list(env, monkeypatch):
    session, tools, pipe = env
    setup_waiting(session)
    session.data['offers/get_from_filter'][-2]['OFFER_SUM'] = '100,50'
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2002, 'BOARD_ID': 2, 'STATUS_ID':55,
        'STATUS_NAME':'ОЖИДАЕТ ОПЛАТЫ', 'CONTACT_TITLE':'Другой', 'OFFER_SUM': 200})
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2003, 'BOARD_ID': 2, 'STATUS_ID':55,
        'STATUS_NAME':'ОЖИДАЕТ ОПЛАТЫ', 'CONTACT_TITLE':'Неизвестный баланс', 'OFFER_SUM': 300})
    original = tools._read
    def read(tool, method, payload=None, **kwargs):
        if method == 'payment/get_balance':
            oid = payload['ENTITY_ID']
            if oid == 2000:
                return {'RUB_CASH': {'CURRENCY_VALUE': '20,25', 'ENTITY_ID': oid, 'ENTITY_TYPE':'OFFER'}}
            if oid == 2002:
                return {'RUB_CASH': {'CURRENCY_VALUE': '200', 'ENTITY_ID': oid, 'ENTITY_TYPE':'OFFER'}}
            return {'RUB_CASH': {'CURRENCY_VALUE': '300', 'ENTITY_ID': 999, 'ENTITY_TYPE':'OFFER'}}
        return original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)
    result = send(pipe, 'покажи сделки которые ожидают оплаты')
    lines = result.answer.splitlines()
    first = next(line for line in lines if '№2000' in line)
    second = next(line for line in lines if '№2002' in line)
    third = next(line for line in lines if '№2003' in line)
    assert 'сумма 100,5 ₽' in first and 'долг: 80,25 ₽' in first
    assert 'сумма 200 ₽' in second and 'долг: 0 ₽' in second
    assert 'долг: не удалось проверить' in third and 'долг: 0' not in third
    assert '№2001' not in result.answer and not result.drafts and not result.applied
    assert pipe._lists[19] == [2000, 2002, 2003]
    assert 'сверены по названию' not in result.answer


@pytest.mark.parametrize('balance', [None, {}, {'RUB_CASH': {'CURRENCY_VALUE': 'bad', 'ENTITY_ID':2000, 'ENTITY_TYPE':'OFFER'}},
    {'RUB_CASH': {'CURRENCY_VALUE': '25', 'ENTITY_ID':2000, 'ENTITY_TYPE':'CONTACT'}}])
def test_unknown_balance_never_fabricates_debt(env, monkeypatch, balance):
    session, tools, pipe = env
    setup_waiting(session)
    original = tools._read
    def read(tool, method, payload=None, **kwargs):
        return balance if method == 'payment/get_balance' else original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)
    result = send(pipe, 'покажи сделки в статусе ОЖИДАЕТ ОПЛАТЫ')
    assert '№2000' in result.answer and 'долг: не удалось проверить' in result.answer
    assert 'долг: 0 ₽' not in result.answer


def test_balance_error_is_isolated_and_missing_total_not_treated_as_zero(env, monkeypatch):
    session, tools, pipe = env
    setup_waiting(session)
    session.data['offers/get_from_filter'][-2].pop('OFFER_SUM')
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2002, 'BOARD_ID': 2, 'STATUS_ID':55,
        'STATUS_NAME':'ОЖИДАЕТ ОПЛАТЫ', 'OFFER_SUM': 200})
    original = tools._read
    calls = []
    def read(tool, method, payload=None, **kwargs):
        if method == 'payment/get_balance':
            calls.append(payload['ENTITY_ID'])
            raise RuntimeError('private error text')
        return original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)
    answer = send(pipe, 'покажи сделки в ожидании оплаты').answer
    lines = answer.splitlines()
    assert 'сумма не получена' in next(line for line in lines if '№2000' in line)
    assert all('долг: не удалось проверить' in next(line for line in lines if f'№{n}' in line) for n in (2000,2002))
    assert calls == [2002] and 'private error text' not in answer


def test_other_statuses_and_incomplete_warning_remain_unchanged(env, monkeypatch):
    session, tools, pipe = env
    answer = send(pipe, 'покажи сделки в записи').answer
    assert '№1328' in answer and 'сверены по названию' not in answer
    assert any(c['method'] == 'payment/get_balance' for c in session.calls)
    assert 'долг:' in answer.lower()
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: [dict(offer_id=1001, board_id=2, status='Запись'),
        dict(offer_id=1002, board_id=None, status='Запись')])
    answer, ids = try_answer('покажи сделки в записи', tools)
    assert ids == [1001] and 'может быть неполным' in answer
    assert 'сверены по названию' not in answer
