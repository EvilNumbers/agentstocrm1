"""Просмотр по имени при отсутствующем ID; записи и блокировки не изменяются."""
import copy

import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.bot.fastpath import try_answer
from stocrm_agent.mock import MockResponse
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def omit_status_ids(session, monkeypatch):
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if not url.endswith('/offers/get_from_filter'):
            return response
        body = copy.deepcopy(response.json())
        rows = body.get('RESPONSE')
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    row.pop('STATUS_ID', None)
        return MockResponse(body)
    monkeypatch.setattr(session, 'post', post)


def test_work_status_list_with_missing_id_only_reads(env, monkeypatch):
    session, tools, pipe = env
    omit_status_ids(session, monkeypatch)
    result = send(pipe, 'Покажи сделки в работе')
    assert '№1654' in result.answer
    assert 'сверена по названию' in result.answer and 'только просмотр' in result.answer
    assert not result.drafts and not writes(session)


def test_quick_list_and_button_check_own_data_and_status(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    existing(session, 1901)
    session.data['offers/get_from_filter'][-1].update(CONTACT_ID=8888, CONTACT_TITLE='Клиент Б')
    omit_status_ids(session, monkeypatch)
    choice = send(pipe, 'Покажи быстрые сделки').choices[0]
    assert set(choice['offers']) == {1900, 1901}
    assert 'сверена по названию' in choice['question']
    assert 'Клиент Б' not in choice['question'].split('📋 Быстрая сделка №1900')[1].split('📋 Быстрая сделка №1901')[0]
    index = choice['offers'].index(1901)
    card = pipe.quick_read_choice(choice, 1, index)
    assert 'Клиент Б' in card.answer and 'сверен по названию' in card.answer
    assert not writes(session)


def test_button_rechecks_changed_status_and_contact(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    omit_status_ids(session, monkeypatch)
    choice = send(pipe, 'Покажи быстрые сделки').choices[0]
    session.data['offers/get_from_filter'][-1]['STATUS_NAME'] = 'Заказ'
    answer = pipe.quick_read_choice(choice, 1, 0).answer.lower()
    assert 'не удалось подтвердить' in answer
    assert not writes(session)


def test_ignored_filters_do_not_mix_other_statuses(env, monkeypatch):
    _, tools, _ = env
    rows = [dict(offer_id=1001, board_id=2, status_id_missing=True, status='В работе'),
            dict(offer_id=1002, board_id=2, status_id_missing=True, status='Заказ'),
            dict(offer_id=1003, board_id=3, status_id_missing=True, status='В работе'),
            dict(offer_id=1004, board_id=2, status_id=14, status_id_missing=False, status='В работе'),
            dict(offer_id=1005, board_id=2, status_id=None, status_id_missing=False, status='В работе')]
    monkeypatch.setattr(tools, 'find_offers', lambda **kwargs: rows)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert ids == [1001]
    assert all('№' + str(i) not in answer for i in (1002, 1003, 1004, 1005))
    assert 'может быть неполным' in answer


def test_duplicate_status_names_never_inferred(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offer/all_statuses']['52'] = {'STATUS_ID': 52, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'N'}
    omit_status_ids(session, monkeypatch)
    result = send(pipe, 'Покажи быстрые сделки')
    assert not result.choices and 'неполон' in result.answer
    assert not writes(session)


def test_unavailable_or_incomplete_directory_does_not_assume_name(env, monkeypatch):
    _, tools, _ = env
    from stocrm_agent.bot.status_view import match
    kwargs = dict(board=2, status_id=None, status_id_missing=True, status_name='Быстрые сделки',
                  expected_board=2, expected_status=51, expected_name='Быстрые сделки')
    monkeypatch.setattr(tools, 'statuses', lambda _: [{'board_id': 2, 'status_id': 51, 'title': 'Быстрые сделки'},
                                                      {'board_id': 2, 'status_id': 52, 'title': None}])
    assert match(tools, **kwargs) == 'unknown'
    monkeypatch.setattr(tools, 'statuses', lambda _: (_ for _ in ()).throw(RuntimeError('down')))
    assert match(tools, **kwargs) == 'unknown'


def test_write_preflight_remains_strict_when_id_is_missing(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    omit_status_ids(session, monkeypatch)
    ids = quick_read.identity(tools)
    with pytest.raises(ValueError):
        quick_read.offers(tools, ids)
    assert not writes(session)
