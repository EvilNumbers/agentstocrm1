"""Смена статуса по названию статуса без слова «статус» (волна 137).

Случай из журнала: «Сделку 1359 переведи с успешно реализовано» — номер сделки
назван, статус назван, но слова «статус» нет, поэтому просьба не распознавалась
как смена статуса: модель пыталась сменить статус, а бот отвечал «укажите номер
одной сделки». Проверки ниже фиксируют, что такой запрос работает, а защита от
подмены сделки и статуса моделью сохраняется.
"""
import json

import pytest

from stocrm_agent.bot.llm_tools import status_titles_match
from test_status_lists_v99 import env, send  # noqa: F401


STATUS_ID = 34
STATUS_TITLE = 'Успешно реализовано'


def add_status(session, status_id=STATUS_ID, title=STATUS_TITLE, board_id=2):
    session.data['offer/all_statuses'][str(status_id)] = {
        'STATUS_ID': status_id, 'BOARD_ID': board_id, 'TITLE': title, 'ACTIVE': 'Y',
    }


class StatusModel:
    """Модель, которая пытается сменить статус указанной сделки."""

    def __init__(self, offer_id=1654, status_id=STATUS_ID):
        self.calls = 0
        self.offer_id = offer_id
        self.status_id = status_id

    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'x', 'function': {
                'name': 'draft_change_status',
                'arguments': json.dumps({'offer_id': self.offer_id,
                                         'status_id': self.status_id, 'board_id': 2})}}]}
        return {'content': 'Подготовил изменение.'}


class ChatOnlyModel:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        return {'content': 'Понял, ничего не меняю.'}


def writes(session, method='offer/status/change'):
    return [call for call in session.calls if call['method'] == method]


@pytest.mark.parametrize('text', [
    'Сделку 1654 переведи с успешно реализовано',
    'Сделку 1654 переведи в успешно реализовано',
    'переведи сделку 1654 в успешно реализовано',
    'Сделка №1654 — переведи в успешно реализовано',
])
def test_named_status_without_word_status_is_a_status_change(env, text):
    session, _, pipe = env
    add_status(session)
    model = pipe.llm = StatusModel()
    result = send(pipe, text)
    assert pipe.router.status_authorized == 1654
    assert model.calls >= 1
    assert result.drafts, result.answer
    summary = result.drafts[0]['summary']
    assert '1654' in summary and STATUS_TITLE in summary
    assert not writes(session), 'до подтверждения в CRM ничего не пишем'


@pytest.mark.parametrize('text,status_id,status_title', [
    ('Сделку 1654 переведи в оплачено', 35, 'Оплачено'),
    ('Сделку 1654 переведи в ожидает оплаты', 36, 'Ожидает оплаты'),
    ('Сделку 1654 переведи в оплату', 36, 'Ожидает оплаты'),
])
def test_named_payment_status_resolves_and_applies(env, text, status_id, status_title):
    session, _, pipe = env
    add_status(session, status_id, status_title)
    pipe.llm = StatusModel(status_id=status_id)
    pipe.router.policy.requires_confirmation = lambda name: False
    result = send(pipe, text)
    assert result.applied, result.answer
    assert str(result.applied[0]['payload']['OFFER_ID']) == '1654'
    sent = writes(session)
    assert len(sent) == 1
    assert str(sent[0]['payload']['STATUS_ID']) == str(status_id)
    assert str(session.data['offers/get_from_filter'][-1].get('OFFER_ID')) != '1654'  # чужие не тронуты


def test_named_status_applies_exactly_to_the_named_deal(env):
    session, _, pipe = env
    add_status(session)
    pipe.llm = StatusModel()
    pipe.router.policy.requires_confirmation = lambda name: False
    result = send(pipe, 'Сделку 1654 переведи в успешно реализовано')
    assert result.applied, result.answer
    row = next(o for o in session.data['offers/get_from_filter'] if o['OFFER_ID'] == 1654)
    assert str(row['STATUS_ID']) == str(STATUS_ID)
    others = [o for o in session.data['offers/get_from_filter'] if o['OFFER_ID'] != 1654]
    assert all(str(o.get('STATUS_ID')) != str(STATUS_ID) for o in others)


def test_status_without_number_refuses_and_names_the_last_shown_deal(env):
    session, _, pipe = env
    add_status(session)
    pipe._last_shown_offer[19] = 1654
    model = pipe.llm = StatusModel()
    result = send(pipe, 'Смени статус на успешно реализовано')
    assert model.calls == 0, 'без номера сделки модель не зовём'
    assert 'номер' in result.answer.lower()
    assert '1654' in result.answer
    assert not result.drafts and not writes(session)


def test_no_last_deal_still_asks_for_a_number(env):
    session, _, pipe = env
    add_status(session)
    pipe.llm = StatusModel()
    result = send(pipe, 'Смени статус на успешно реализовано')
    assert 'номер' in result.answer.lower()
    assert 'Например' in result.answer
    assert not result.drafts and not writes(session)


@pytest.mark.parametrize('text', [
    'Отправь счёт на оплату',
    'Напомни про оплату',
    'Покажи сделки в работе',
])
def test_title_words_alone_are_not_a_status_change(env, text):
    session, _, pipe = env
    pipe.llm = ChatOnlyModel()
    result = send(pipe, text)
    assert pipe.router.status_authorized is None
    assert pipe.router.status_target_name is None
    assert 'Смену статуса' not in result.answer
    assert not writes(session)


def test_model_cannot_substitute_the_named_status(env):
    session, _, pipe = env
    add_status(session)
    add_status(session, 41, 'Запись')
    pipe.llm = StatusModel(status_id=41)     # модель выбрала «Запись» вместо названного
    result = send(pipe, 'Сделку 1654 переведи в успешно реализовано')
    assert not result.drafts and not result.applied
    assert 'не удалось подтвердить' in result.answer.lower()
    assert 'реализовано' in result.answer.lower()   # человек видит, какой статус назвал
    assert 'Запись' in result.answer                # и какой выбрала модель
    assert not writes(session)


def test_this_deal_with_named_status_uses_the_shown_card(env):
    session, _, pipe = env
    add_status(session)
    pipe._last_shown_offer[19] = 1654
    model = pipe.llm = StatusModel()
    result = send(pipe, 'Переведи эту сделку в успешно реализовано')
    assert model.calls >= 1
    assert result.drafts and '1654' in result.drafts[0]['summary']
    assert not writes(session)


@pytest.mark.parametrize('wanted,title,expected', [
    ('успешно реализовано', 'Успешно реализовано', True),
    ('реализовано', 'Успешно реализовано', True),
    ('оплата', 'Ожидает оплаты', True),
    ('оплачено', 'Ожидает оплаты', True),
    ('в работе', 'В работе на сервисе', True),
    ('мусор', 'Мусор', True),
    ('мусор', 'Заказ', False),
    ('успешно реализовано', '', False),
    ('', 'Мусор', False),
])
def test_status_titles_match_is_soft_but_not_blind(wanted, title, expected):
    assert status_titles_match(wanted, title) is expected
