"""Правила долга версии 80 в карточках и статусном списке: без живой CRM."""
import pytest

from stocrm_agent.bot.fastpath import format_offer_card
from stocrm_agent.tools import _offer_balance_values
from test_status_lists_v99 import env, send  # noqa: F401
from test_payment_wait_status_v105 import setup_waiting


@pytest.mark.parametrize('identity', [
    {}, {'ENTITY_ID': 2000}, {'ENTITY_TYPE': 'OFFER'},
    {'ENTITY_ID': '2000', 'ENTITY_TYPE': 'OFFER'},
    {'ENTITY_ID': None, 'ENTITY_TYPE': ''},
])
def test_v80_accepts_absent_identifiers_in_card_and_list(env, monkeypatch, identity):
    session, tools, pipe = env
    setup_waiting(session)
    row = next(row for row in session.data['offers/get_from_filter'] if row['OFFER_ID'] == 2000)
    row['OFFER_SUM'] = 56950
    calls = []
    original = tools._read

    def read(tool, method, payload=None, **kwargs):
        if method == 'payment/get_balance':
            calls.append(payload)
            return {'RUB_CASH': {'CURRENCY_VALUE': 9500, **identity}}
        return original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)
    card = tools.offer_card(2000, with_items=True)
    text = format_offer_card(card, detailed=True)
    assert 'Оплачено: 9 500 ₽ (наличные 9 500 ₽)' in text
    assert 'Долг (осталось оплатить): 47 450 ₽' in text
    answer = send(pipe, 'покажи сделки в статусе ОЖИДАЕТ ОПЛАТЫ').answer
    assert '№2000' in answer and 'сумма 56 950 ₽ · долг: 47 450 ₽' in answer
    assert calls and all(call == {'ENTITY_ID': 2000, 'ENTITY_TYPE': 'OFFER'} for call in calls)


@pytest.mark.parametrize('balance', [
    None, {}, [], {'RUB_CASH': None}, {'RUB_CASH': {'CURRENCY_VALUE': 'bad'}},
    {'RUB_CASH': {'CURRENCY_VALUE': 100, 'ENTITY_ID': 2001}},
    {'RUB_CASH': {'CURRENCY_VALUE': 100, 'ENTITY_TYPE': 'CONTACT'}},
    {'RUB_CASH': {'CURRENCY_VALUE': 100}, 'RUB_BANK_CARD': {'CURRENCY_VALUE': 10, 'ENTITY_ID': 2001}},
    {'RUB_CASH': {'CURRENCY_VALUE': 100}, 'RUB_BANK_CARD': {'CURRENCY_VALUE': 'bad'}},
])
def test_wrong_or_missing_balance_never_becomes_zero(balance):
    assert _offer_balance_values(balance, 2000, 1000) == (None, None, None)


def test_multiple_payment_types_and_unknown_sum():
    balance = {'RUB_CASH': {'CURRENCY_VALUE': '25,25'},
               'RUB_BANK_CARD': {'CURRENCY_VALUE': 10, 'ENTITY_ID': 2000}}
    assert _offer_balance_values(balance, 2000, 100) == (35.25, 64.75, 0)
    assert _offer_balance_values(balance, 2000, None) == (None, None, None)


def test_overpayment_and_explicit_zero_are_distinguished():
    assert _offer_balance_values({'RUB_CASH': {'CURRENCY_VALUE': 150}}, 2000, 100) == (150, 0, 50)
    assert _offer_balance_values({'RUB_CASH': {'CURRENCY_VALUE': 0}}, 2000, 100) == (0, 100, 0)


def test_quick_deal_card_accepts_v80_balance_without_identity(monkeypatch, tmp_path):
    from stocrm_agent.bot import quick_read
    from stocrm_agent.audit import AuditLog
    from stocrm_agent.client import StocrmClient
    from stocrm_agent.drafts import DraftStore
    from stocrm_agent.mock import MockSession
    from stocrm_agent.tools import Toolset
    session = MockSession()
    session.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2,
        'STATUS_ID': 51, 'STATUS_NAME': 'Быстрые сделки', 'OFFER_SUM': 6000})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    original = tools._read
    def read(tool, method, payload=None, **kwargs):
        if method == 'payment/get_balance':
            return {'RUB_CASH': {'CURRENCY_VALUE': 1500}}
        return original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)
    detail = quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert '4 500 ₽' in detail and 'Долг: не удалось проверить' not in detail
