"""Формат реального ответа без валютного префикса; данные вымышленные."""
import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.bot.fastpath import format_offer_card
from stocrm_agent.tools import _offer_balance_values, _usable_rub_balances
from test_quick_read import env as quick_env  # noqa: F401
from test_status_lists_v99 import env as waiting_env, send  # noqa: F401
from test_payment_wait_status_v105 import setup_waiting

KINDS = ('CASH', 'BANK_CARD', 'BLACK_CASH', 'BLACK_BANK_CARD', 'CASHLESS', 'SBP')


def balance(oid=1900):
    return {k: {'ENTITY_ID': oid, 'ENTITY_TYPE': 'OFFER', 'CURRENCY_VALUE': 0,
                'CURRENCY_NAME': '', 'CURRENCY_TYPE': k, 'CURRENCY_TYPE_HUMAN': 'название'} for k in KINDS}


def setup_balance(monkeypatch, tools, amount):
    original = tools._read
    def read(tool, method, payload=None, **kwargs):
        if method == 'payment/get_balance':
            return amount
        return original(tool, method, payload, **kwargs)
    monkeypatch.setattr(tools, '_read', read)


def test_user_example_in_ordinary_quick_card_and_offer_money(quick_env, monkeypatch):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'RUB')
    session, pipe = quick_env
    tools = pipe.tools
    row = next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)
    row['OFFER_SUM'] = 2000
    row['ORDERS_SUM'] = 2000
    session.data['payment/get_balance'] = balance()  # идём через настоящий клиент и разворачивание RESPONSE
    card = tools.offer_card(1900, with_items=True)
    assert card['paid'] == 0 and card['debt'] == 2000
    assert 'Оплачено: 0 ₽' in format_offer_card(card, detailed=True)
    assert 'Долг (осталось оплатить): 2 000 ₽' in format_offer_card(card, detailed=True)
    assert 'Долг (осталось оплатить): 2 000 ₽' in quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert tools.offer_money(1900)['paid'] == 0 and tools.offer_money(1900)['debt'] == 2000


def test_payment_kinds_cents_and_overpay(quick_env, monkeypatch):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'RUB')
    _, pipe = quick_env
    tools = pipe.tools
    b = balance()
    b['BLACK_CASH']['CURRENCY_VALUE'] = '1500.0000'
    b['SBP']['CURRENCY_VALUE'] = '2.25'
    b['BANK_CARD']['CURRENCY_VALUE'] = '50,50'
    setup_balance(monkeypatch, tools, b)
    card = tools.offer_card(1900, with_items=True)
    assert card['paid'] == 1552.75 and card['debt'] == 4447.25
    assert card['payments']['by_kind'] == {'наличные (НФ)': 1500, 'карта': 50.5, 'СБП': 2.25}
    assert _offer_balance_values(b, 1900, 1000) == (1552.75, 0, 552.75)
    money = tools.offer_money(1900)
    assert money['by_kind'] == card['payments']['by_kind'] and money['paid'] == card['paid']


def test_payment_waiting_list_uses_this_balance(waiting_env, monkeypatch):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'RUB')
    session, tools, pipe = waiting_env
    setup_waiting(session)
    next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 2000)['OFFER_SUM'] = 2000
    setup_balance(monkeypatch, tools, balance(2000))
    answer = send(pipe, 'покажи сделки в статусе ОЖИДАЕТ ОПЛАТЫ').answer
    assert '№2000' in answer and 'сумма 2 000 ₽ · долг: 2 000 ₽' in answer


@pytest.mark.parametrize('mutation', [
    lambda b: b.pop('SBP'),
    lambda b: b.update(NEW_KIND={'CURRENCY_VALUE': 5}),
    lambda b: b['CASH'].update(CURRENCY_NAME='USD'),
    lambda b: b['CASH'].pop('CURRENCY_NAME'),
    lambda b: b['CASH'].update(CURRENCY_TYPE='CASHLESS'),
    lambda b: b['CASH'].update(ENTITY_ID=1901),
    lambda b: b['CASH'].pop('ENTITY_ID'),
    lambda b: b['CASH'].update(ENTITY_TYPE='CONTACT'),
    lambda b: b['CASH'].update(CURRENCY_VALUE='unknown'),
    lambda b: b.update(RUB_CASH={'CURRENCY_VALUE': 0}),
    lambda b: b.update(USD_CASH={'CURRENCY_VALUE': 10}),
])
def test_partial_mixed_or_unverifiable_response_never_used(monkeypatch, mutation):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'RUB')
    b = balance()
    mutation(b)
    assert _usable_rub_balances(b, 1900) is None
    assert _offer_balance_values(b, 1900, 2000) == (None, None, None)


def test_disable_assumption_and_keep_v80_named_rub(monkeypatch):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'STRICT')
    assert _offer_balance_values(balance(), 1900, 2000) == (None, None, None)
    assert _offer_balance_values({'RUB_CASH': {'CURRENCY_VALUE': 100}}, 1900, 2000) == (100, 1900, 0)


def test_unknown_amount_not_returned_as_zero_in_offer_money(quick_env, monkeypatch):
    _, pipe = quick_env
    setup_balance(monkeypatch, pipe.tools, {})
    assert pipe.tools.offer_money(1900)['paid'] is None
    assert pipe.tools.offer_money(1900)['debt'] is None


def test_diagnostics_recognizes_shape_but_hides_sums_and_keys(quick_env, monkeypatch):
    monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'RUB')
    _, pipe = quick_env
    setup_balance(monkeypatch, pipe.tools, balance())
    diag = pipe.tools.balance_diagnostic(1900)
    assert diag['field_count'] == 6 and diag['rub_count'] == 0
    assert diag['unnamed_count'] == 6 and diag['unnamed_usable']
    assert 'CURRENCY_VALUE' not in str(diag) and 'CASH' not in str(diag)
