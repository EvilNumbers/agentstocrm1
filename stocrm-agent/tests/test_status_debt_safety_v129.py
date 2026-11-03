"""Регрессии снимков и журнала v128, только MockSession."""
import asyncio
import json
import pytest

from stocrm_agent.bot.fastpath import try_answer, format_offers_list
from stocrm_agent.client import StocrmError
from test_status_lists_v99 import env, send  # noqa: F401
from test_client_booking import env as booking_env, request as booking_request, send as booking_send  # noqa: F401
from test_quick_read import env as quick_env  # noqa: F401


class StatusModel:
    def __init__(self, wrong_offer=1712):
        self.calls = 0
        self.wrong_offer = wrong_offer
    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'x', 'function': {'name': 'draft_change_status',
                'arguments': json.dumps({'offer_id': self.wrong_offer, 'status_id': 70, 'board_id': 2})}}]}
        return {'content': 'Подготовил изменение.'}


@pytest.mark.parametrize('text', [
    'Перемести эту сделку в мусор', 'Переведи в мусор', 'смени статус сделки',
    'перемести эту быструю сделку в мусор', 'перемести из сделки 71120 в сделку 71148 в мусор',
    'перемести как у сделки 71120 номер 71148 в мусор',
])
def test_no_single_explicit_deal_never_uses_stale_context(env, text):
    session, tools, pipe = env
    pipe._last_offer[19] = 71120
    pipe.llm = StatusModel()
    result = send(pipe, text)
    assert not result.drafts and not result.applied and 'номер' in result.answer.lower()
    assert pipe.llm.calls == 0
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)
    assert not tools.drafts.list('all')


@pytest.mark.parametrize('text', ['Перемести в мусор сделку 1654', 'Перемести быструю сделку №1654 в мусор'])
def test_explicit_deal_not_substituted_by_model_in_auto_mode(env, text):
    session, tools, pipe = env
    pipe._last_offer[19] = 1712
    session.data['offer/all_statuses']['70'] = {'STATUS_ID': 70, 'BOARD_ID': 2, 'TITLE': 'Мусор', 'ACTIVE': 'Y'}
    pipe.llm = StatusModel(wrong_offer=1712)
    pipe.router.policy.requires_confirmation = lambda name: False
    result = send(pipe, text)
    assert not result.drafts and result.applied
    assert result.applied[0]['payload']['OFFER_ID'] == 1654
    assert '1654' in result.applied[0]['summary']
    writes = [c for c in session.calls if c['method'] == 'offer/status/change']
    assert len(writes) == 1 and writes[0]['payload']['OFFER_ID'] == 1654


def test_llm_cannot_invent_a_status_change(env):
    session, tools, pipe = env
    pipe.fastpath = False
    pipe.llm = StatusModel()
    result = send(pipe, 'Расскажи про сделку 1654')
    assert not result.drafts and not result.applied
    assert 'номер одной сделки' in result.answer.lower()
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_status_bare_estimate_uses_confirmed_color_and_debt(env, monkeypatch):
    session, tools, pipe = env
    session.data['offer/all_statuses']['14']['FRONT_COLOR'] = '#f0ce22'
    session.data['offers/get_from_filter'][1]['OFFER_SUM'] = '100,50'
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda oid, total: 20.25 if oid == 1712 else None)
    result = send(pipe, 'Просчет')
    line = next(line for line in result.answer.splitlines() if '№1712' in line)
    assert '🟨' in line and 'сумма 100,5 ₽' in line and 'долг: 20,25 ₽' in line
    assert not result.drafts and not result.applied


def test_each_deal_has_its_own_debt_in_non_payment_status(env, monkeypatch):
    session, tools, pipe = env
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2000, 'BOARD_ID': 2, 'STATUS_ID': 41,
        'STATUS_NAME': 'Запись', 'OFFER_SUM': '300.50'})
    values = {1328: 0, 2000: 250.25}
    calls=[]
    def balance(oid, total):
        calls.append((oid, total))
        return values.get(oid)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer = send(pipe, 'Покажи сделки в записи').answer
    assert 'долг: 0 ₽' in next(line for line in answer.splitlines() if '№1328' in line)
    assert 'долг: 250,25 ₽' in next(line for line in answer.splitlines() if '№2000' in line)
    assert {x[0] for x in calls} == {1328, 2000}


def test_missing_balance_or_amount_does_not_become_zero(env, monkeypatch):
    session, tools, pipe = env
    row = next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1328)
    row['OFFER_SUM'] = None
    calls=[]
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda oid, total: calls.append(oid) or 0)
    line = next(x for x in send(pipe, 'покажи сделки в записи').answer.splitlines() if '№1328' in x)
    assert 'сумма не получена' in line and 'долг: не удалось проверить' in line
    assert calls == []
    assert 'долг: не удалось проверить' in format_offers_list([{'offer_id': 1, 'sum': 100, 'sum_known': True}])


def test_balance_error_isolated_from_other_deals(env, monkeypatch):
    session, tools, pipe = env
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2000, 'BOARD_ID': 2, 'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'OFFER_SUM': 100})
    def balance(oid, total):
        if oid == 1328:
            raise StocrmError('недоступен')
        return 25
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer = send(pipe, 'покажи сделки в записи').answer
    assert 'долг: не удалось проверить' in next(x for x in answer.splitlines() if '№1328' in x)
    assert 'долг: 25 ₽' in next(x for x in answer.splitlines() if '№2000' in x)


def test_quick_cards_include_color_and_debt(quick_env):
    session, pipe = quick_env
    session.data['offer/all_statuses']['51']['FRONT_COLOR'] = '#bb22ff'
    result = send(pipe, 'покажи быстрые сделки')
    assert result.choices
    assert '🟪' in result.choices[0]['question'] and 'Долг' in result.choices[0]['question']


def test_booking_choices_include_debt_and_color(booking_env):
    session, _, pipe = booking_env
    session.data['offer/all_statuses']['15']['FRONT_COLOR'] = '#00bb00'
    result = booking_send(pipe, booking_request(), [])
    assert result.pending_booking['field'] == 'offer_choice'
    assert 'долг' in result.pending_booking['options'][1]['label'].lower()
    assert '🟩' in result.pending_booking['options'][1]['label']


def test_llm_wrong_status_does_not_prepare_draft(env):
    session, tools, pipe = env
    session.data['offer/all_statuses']['70'] = {'STATUS_ID': 70, 'BOARD_ID': 2, 'TITLE': 'Заказ', 'ACTIVE': 'Y'}
    pipe.llm = StatusModel()
    result = send(pipe, 'перемести сделку 1654 в мусор')
    assert not result.drafts and not result.applied
    assert 'не удалось подтвердить' in result.answer.lower()
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_old_status_draft_is_blocked_before_write(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='change_status', method='offer/status/change',
        payload={'OFFER_ID': 1712, 'BOARD_ID': 2, 'STATUS_ID': 14}, summary='старый черновик')
    with pytest.raises(ValueError, match='Старый или неподтверждённый'):
        tools.apply_draft(old['token'])
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_safe_status_draft_can_be_applied_after_confirmation(env):
    session, tools, pipe = env
    session.data['offer/all_statuses']['70'] = {'STATUS_ID': 70, 'BOARD_ID': 2, 'TITLE': 'Мусор', 'ACTIVE': 'Y'}
    pipe.llm = StatusModel()
    result = send(pipe, 'перемести сделку 1654 в мусор')
    assert len(result.drafts) == 1
    tools.apply_draft(result.drafts[0]['token'])
    writes = [c for c in session.calls if c['method'] == 'offer/status/change']
    assert len(writes) == 1 and writes[0]['payload']['OFFER_ID'] == 1654


def test_unknown_color_not_fabricated(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['14']['FRONT_COLOR'] = 'unrecognised'
    text = send(pipe, 'Просчёт').answer
    line = next(line for line in text.splitlines() if '№1712' in line)
    assert 'цвет статуса не определён' in line
    assert not any(mark in line for mark in ('🟨', '🟥', '🟩', '🟦', '🟪'))


def test_ambiguous_estimate_refuses_guessing(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['70'] = {'STATUS_ID': 70, 'BOARD_ID': 2, 'TITLE': 'Просчёт', 'ACTIVE': 'Y', 'FRONT_COLOR': '#ff0'}
    answer = send(pipe, 'Просчёт').answer
    assert 'несколько подходящих статусов' in answer and '№1712' not in answer
