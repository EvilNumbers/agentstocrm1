"""Проверка статусов по имени; последующие позиции — по номеру сделки."""
import copy

import pytest

from stocrm_agent import quick_deal
from stocrm_agent.bot.fastpath import try_answer
from stocrm_agent.mock import MockResponse
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def hide_all_status_ids(session, monkeypatch):
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if not url.endswith('/offers/get_from_filter'):
            return response
        body = copy.deepcopy(response.json())
        if isinstance(body.get('RESPONSE'), list):
            for row in body['RESPONSE']:
                row.pop('STATUS_ID', None)
        return MockResponse(body)
    monkeypatch.setattr(session, 'post', post)


def draft(pipe):
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300')
    assert len(result.drafts) == 1, result.answer
    assert 'даже если статус или контакт изменится' in result.drafts[0]['summary']
    return result.drafts[0]


def test_name_check_and_all_positions_without_any_row_status_id(env, monkeypatch):
    session, tools, pipe = env
    hide_all_status_ids(session, monkeypatch)
    selected = draft(pipe)
    assert not writes(session)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied' and result['verified']
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert all('REQUIRED_FIELDS' in c['payload'] for c in session.calls if c['method'] == 'offers/get_from_filter')


@pytest.mark.parametrize('name', [None, '', 'Мусор', 'Быстрые сделки другой статус'])
def test_wrong_or_missing_name_after_status_does_not_stop_numbered_items(env, monkeypatch, name):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools._search_offer_row
    def changed(oid):
        row, count = original(oid)
        if any(c['method'] == 'offer/status/change' for c in writes(session)):
            row = {**row, 'STATUS_NAME': name}
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', changed)
    monkeypatch.setattr(quick_deal.time, 'sleep', lambda _: None)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied' and result['result']['status_not_read_back']
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    with pytest.raises(ValueError):
        tools.apply_draft(selected['token'])


def test_name_is_authoritative_even_when_id_mismatches(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools._search_offer_row
    def conflict(oid):
        row, count = original(oid)
        if any(c['method'] == 'offer/status/change' for c in writes(session)):
            row = {**row, 'STATUS_ID': 999}
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', conflict)
    assert tools.apply_draft(selected['token'])['status'] == 'applied'


@pytest.mark.parametrize('when', ['work/add', 'offer/require/add'])
def test_created_deal_can_move_to_other_board_and_status_after_transition(env, monkeypatch, when):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def move(method, payload=None, **kw):
        response = original(method, payload, **kw)
        if method == when:
            session.data['offers/get_from_filter'][-1].update(BOARD_ID=3, STATUS_ID=34, STATUS_NAME='Мусор')
        return response
    monkeypatch.setattr(tools.client, 'call', move)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied'
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


@pytest.mark.parametrize('field,value', [('OFFER_ID', 9999)])
def test_changed_identity_after_creation_blocks_next_write(env, monkeypatch, field, value):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools._search_offer_row
    def wrong(oid):
        row, count = original(oid)
        if any(c['method'] == 'work/add' for c in writes(session)):
            row = {**row, field: value}
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', wrong)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial'
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_existing_selection_without_ids_and_no_new_implicit(env, monkeypatch):
    session, _, pipe = env
    existing(session, 1900); existing(session, 1901)
    hide_all_status_ids(session, monkeypatch)
    result = send(pipe, 'быстрая сделка диагностика 1000 рублей')
    assert not result.drafts and set(result.choices[0]['offers']) == {1900, 1901}
    assert not writes(session)


def test_unconfirmed_existing_deal_name_cannot_create_new(env, monkeypatch):
    session, _, pipe = env
    existing(session, 1900)
    session.data['offers/get_from_filter'][-1].pop('STATUS_NAME')
    hide_all_status_ids(session, monkeypatch)
    result = send(pipe, 'быстрая сделка диагностика 1000 рублей')
    assert not result.drafts and not result.choices and 'неполон' in result.answer
    assert not writes(session)


def test_no_status_id_in_directory_for_general_list(env, monkeypatch):
    session, tools, _ = env
    session.data['offer/all_statuses']['15'].pop('STATUS_ID')
    hide_all_status_ids(session, monkeypatch)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert ids == [1654] and 'по названию' in answer
    assert not writes(session)


def test_exception_from_directory_reports_no_fake_empty(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, 'boards', lambda: (_ for _ in ()).throw(RuntimeError('secret')))
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert not ids and 'не удалось прочитать' in answer.lower() and '/crm_test' in answer
    assert 'secret' not in answer


def test_moves_before_first_item_after_initial_name_confirmation(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools._search_offer_row
    checks = []
    def move(oid):
        row, count = original(oid)
        if any(c['method'] == 'offer/status/change' for c in writes(session)):
            checks.append(oid)
            if len(checks) == 2:
                session.data['offers/get_from_filter'][-1].update(BOARD_ID=3, STATUS_NAME='Мусор')
                row = {**row, 'BOARD_ID': 3, 'STATUS_NAME': 'Мусор'}
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', move)
    assert tools.apply_draft(selected['token'])['status'] == 'applied'
    assert [x['method'] for x in writes(session)][-3:] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_duplicate_name_in_directory_after_status_change_does_not_block_items(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def add_ambiguous(method, payload=None, **kw):
        result = original(method, payload, **kw)
        if method == 'offer/status/change':
            session.data['offer/all_statuses']['52'] = {'STATUS_ID': 52, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'N'}
        return result
    monkeypatch.setattr(tools.client, 'call', add_ambiguous)
    monkeypatch.setattr(quick_deal.time, 'sleep', lambda _: None)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied' and result['result']['status_not_read_back']
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_old_pending_draft_requires_fresh_confirmation(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='quick_deal', method='quick_deal', payload={'contact_id': 5555,
        'board_id': 2, 'status_id': 51, 'offer_id': None, 'work': {'title': 'Диагностика', 'price': 1000},
        'articles': [], 'reason': None}, summary='Старые условия', extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(old['token'])
    assert not writes(session)
