"""Контекст последней показанной сделки и политика подтверждения; только MockSession."""
import asyncio
import json

import pytest

from test_quick_read import env as quick_env, send as quick_send  # noqa: F401
from test_status_lists_v99 import env, send  # noqa: F401
from test_client_booking import env as booking_env, request as booking_request, send as booking_send  # noqa: F401
from test_status_debt_safety_v129 import StatusModel
from stocrm_agent.bot import quick_read


def status_model(session, pipe, *, auto=True):
    session.data['offer/all_statuses']['70'] = {'STATUS_ID': 70, 'BOARD_ID': 2, 'TITLE': 'Мусор', 'ACTIVE': 'Y'}
    pipe.llm = StatusModel(wrong_offer=1654)
    pipe.router.policy.requires_confirmation = lambda name: not auto


def writes(session):
    return [c for c in session.calls if c['method'] == 'offer/status/change']


def test_quick_list_last_card_auto_once(quick_env):
    session, pipe = quick_env
    listing = quick_send(pipe, 'покажи быстрые сделки')
    ids = listing.choices[0]['offers']
    assert len(ids) == 2 and listing.shown_offers[-1] == ids[-1]
    status_model(session, pipe)
    result = quick_send(pipe, 'перемести эту сделку в мусор')
    assert not result.drafts and len(result.applied) == 1
    assert result.applied[0]['payload']['OFFER_ID'] == ids[-1]
    assert len(writes(session)) == 1 and writes(session)[0]['payload']['OFFER_ID'] == ids[-1]
    assert result.applied[0].get('warning')  # результат статуса не перечитывался


def test_confirm_mode_and_selected_card(quick_env):
    session, pipe = quick_env
    listing = quick_send(pipe, 'покажи быстрые сделки')
    choice = listing.choices[0]
    first, last = choice['offers']
    assert first != last
    opened = pipe.quick_read_choice(choice, 1, 0)
    assert f'№{first}' in opened.answer
    status_model(session, pipe, auto=False)
    result = quick_send(pipe, 'перемести эту быструю сделку в мусор')
    assert len(result.drafts) == 1 and not result.applied and not writes(session)
    assert result.drafts[0]['payload']['OFFER_ID'] == first


def test_other_user_cannot_use_context(quick_env):
    session, pipe = quick_env
    quick_send(pipe, 'покажи быстрые сделки', user=1)
    status_model(session, pipe)
    result = quick_send(pipe, 'перемести эту сделку в мусор', user=2)
    assert 'номер' in result.answer.lower() and not result.applied and not writes(session)


def test_failing_last_quick_card_invalidates_old_context(quick_env, monkeypatch):
    session, pipe = quick_env
    quick_send(pipe, 'покажи быстрые сделки')
    original = quick_read.status_detail
    def fail_last(tools, status_ids, oid, *args):
        if oid == 1901:
            raise ValueError('не проверено')
        return original(tools, status_ids, oid, *args)
    monkeypatch.setattr(quick_read, 'status_detail', fail_last)
    listing = quick_send(pipe, 'покажи быстрые сделки')
    assert listing.shown_offers == []
    status_model(session, pipe)
    result = quick_send(pipe, 'перемести эту сделку в мусор')
    assert 'номер' in result.answer.lower() and not writes(session)


def test_failed_quick_selection_clears_context(quick_env):
    session, pipe = quick_env
    choice = quick_send(pipe, 'покажи быстрые сделки').choices[0]
    target = choice['offers'][0]
    for row in session.data['offers/get_from_filter']:
        if row['OFFER_ID'] == target:
            row.update(STATUS_ID=15, STATUS_NAME='В работе')
    assert 'не удалось подтвердить' in pipe.quick_read_choice(choice, 1, 0).answer.lower()
    status_model(session, pipe)
    assert 'номер' in quick_send(pipe, 'перемести эту сделку в мусор').answer.lower()
    assert not writes(session)


def test_list_then_card_and_explicit_priority(env):
    session, _, pipe = env
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2000, 'BOARD_ID': 2, 'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'OFFER_SUM': 100})
    listing = send(pipe, 'покажи сделки в записи')
    assert listing.shown_offers[-1] == 2000
    card = send(pipe, 'покажи сделку 1654')
    assert card.shown_offers == [1654]
    status_model(session, pipe, auto=False)
    result = send(pipe, 'перемести эту сделку в мусор')
    assert result.drafts[0]['payload']['OFFER_ID'] == 1654
    status_model(session, pipe, auto=False)
    result = send(pipe, 'перемести эту сделку 1712 в мусор')
    assert result.drafts[0]['payload']['OFFER_ID'] == 1712
    assert not writes(session)


def test_ambiguous_status_numbers_never_write(env):
    session, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    status_model(session, pipe)
    result = send(pipe, 'перемести эту сделку как сделку 1320 и сделку 1712 в мусор')
    assert not result.applied and not result.drafts and not writes(session)


def test_wrong_board_still_refused_in_auto(env):
    session, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    status_model(session, pipe)
    session.data['offer/all_statuses']['70']['BOARD_ID'] = 99
    result = send(pipe, 'перемести эту сделку в мусор')
    assert not result.applied and not result.drafts and not writes(session)


def test_booking_options_last_is_remembered(booking_env):
    session, _, pipe = booking_env
    result = booking_send(pipe, booking_request(), [])
    options = result.pending_booking['options']
    last = [int(o['value']) for o in options if str(o['value']).isdigit()][-1]
    # booking_send's fixture uses user id 1
    assert last in pipe._last_shown_offer.values()


def test_this_deal_for_card_read_and_work(env):
    session, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    read = send(pipe, 'покажи эту сделку')
    assert read.shown_offers == [1654]
    pipe.router.policy.requires_confirmation = lambda name: True
    work = send(pipe, 'добавь в эту сделку услугу диагностика за 500')
    assert work.drafts and work.drafts[0]['payload']['DATA']['OFFER_ID'] == 1654


class ReadModel:
    def __init__(self, answer):
        self.calls = 0
        self.answer = answer
    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'x', 'function': {'name': 'offer_card',
                     'arguments': json.dumps({'offer_id': 1712})}}]}
        return {'content': self.answer}


@pytest.mark.parametrize('answer,expected', [('Сделка №1712, данные получены.', 1712),
                                              ('Сделка №9999, данные получены.', None)])
def test_model_card_number_must_be_backed_by_read(env, answer, expected):
    _, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    pipe.fastpath = False
    pipe.llm = ReadModel(answer)
    result = send(pipe, 'расскажи о состоянии')
    assert result.shown_offers == ([expected] if expected else [])
    assert pipe._last_shown_offer.get(19) == expected


class DuplicateStatusModel(StatusModel):
    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [
                {'id': str(i), 'function': {'name': 'draft_change_status',
                    'arguments': json.dumps({'offer_id': oid, 'status_id': 70, 'board_id': 2})}}
                for i, oid in enumerate((1712, 1328))]}
        return {'content': 'Готово'}


def test_repeated_model_status_calls_never_repeat_write(env):
    session, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    status_model(session, pipe)
    pipe.llm = DuplicateStatusModel()
    result = send(pipe, 'перемести эту сделку в мусор')
    assert len(result.applied) == 1 and not result.drafts
    assert len(writes(session)) == 1 and writes(session)[0]['payload']['OFFER_ID'] == 1654


class WrongCommentModel:
    def __init__(self):
        self.calls = 0
    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'x', 'function': {'name': 'draft_update_offer_field',
                     'arguments': json.dumps({'offer_id': 1712, 'field': 'COMMENT', 'value': 'тест'})}}]}
        return {'content': 'Подготовлено.'}


def test_this_deal_overrides_model_for_other_write(env):
    _, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    pipe.llm = WrongCommentModel()
    pipe.router.policy.requires_confirmation = lambda name: True
    result = send(pipe, 'измени комментарий в этой сделке на тест')
    assert result.drafts and result.drafts[0]['payload']['OFFER_ID'] == 1654


def test_model_extra_unverified_number_clears_context(env):
    _, _, pipe = env
    send(pipe, 'покажи сделку 1654')
    pipe.fastpath = False
    pipe.llm = ReadModel('Сделка №1712, а также №9999.')
    result = send(pipe, 'расскажи о состоянии')
    assert result.shown_offers == [] and 19 not in pipe._last_shown_offer


def test_ten_card_limit_selects_tenth_not_unshown(quick_env):
    session, pipe = quick_env
    template = session.data['offers/get_from_filter'][-1]
    for i in range(11):
        session.data['offers/get_from_filter'].append({**template, 'OFFER_ID': 2000 + i})
    result = quick_send(pipe, 'покажи быстрые сделки')
    ids = result.choices[0]['offers']
    assert len(ids) == 10 and result.shown_offers[-1] == ids[-1]
    status_model(session, pipe)
    changed = quick_send(pipe, 'перемести эту сделку в мусор')
    assert changed.applied[0]['payload']['OFFER_ID'] == ids[-1]
    assert len(writes(session)) == 1
