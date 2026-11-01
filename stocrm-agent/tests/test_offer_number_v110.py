"""Проверка быстрых сделок по номеру, без зависимости от контакта; fake CRM."""
import pytest

from stocrm_agent.bot import quick_read
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def reject_scans(monkeypatch, tools):
    original = tools._read
    def read(tool, method, payload=None):
        if tool in ('quick_offers', 'quick_status_offers', 'quick_contacts'):
            raise AssertionError('Поиск по номеру не должен обходить сделки или контакты')
        return original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)


def test_explicit_number_uses_only_deal_and_works_without_contact(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offers/get_from_filter'][-1].pop('CONTACT_ID')
    reject_scans(monkeypatch, tools)
    result = send(pipe, 'быстрая сделка №1900 диагностика 1000 рублей')
    assert len(result.drafts) == 1, result.answer
    draft = result.drafts[0]
    assert '№1900' in draft['summary'] and 'контакт изменится' in draft['summary']
    assert not writes(session)
    assert tools.apply_draft(draft['token'])['status'] == 'applied'
    assert [x['payload']['DATA']['OFFER_ID'] for x in writes(session)] == [1900]
    queries = [c['payload']['FILTER'] for c in session.calls if c['method'] == 'offers/get_from_filter']
    assert queries and all(query == {'OFFER_ID': 1900} for query in queries)


def test_selection_across_contacts_then_direct_check_and_change_of_contact(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900); existing(session, 1901)
    first, second = session.data['offers/get_from_filter'][-2:]
    second.update(CONTACT_ID=8123, CONTACT_TITLE='Клиент Б')
    result = send(pipe, 'быстрая сделка диагностика 1000 рублей')
    choice = result.choices[0]
    assert set(choice['offers']) == {1900, 1901}
    assert 'Клиент Б' in choice['question']
    first['STATUS_NAME'] = 'Мусор'  # другая сделка не должна мешать выбранной
    second['CONTACT_ID'] = 9999
    reject_scans(monkeypatch, tools)
    selection = pipe.quick_read_choice(choice, 1, choice['offers'].index(1901))
    assert len(selection.drafts) == 1, selection.answer
    second.pop('CONTACT_ID')
    assert tools.apply_draft(selection.drafts[0]['token'])['status'] == 'applied'
    assert writes(session)[0]['payload']['DATA']['OFFER_ID'] == 1901


@pytest.mark.parametrize('stage', ['offer/status/change', 'work/add', 'offer/require/add'])
def test_contact_changes_after_creation_but_number_is_followed(env, monkeypatch, stage):
    session, tools, pipe = env
    selection = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    original = tools.client.call
    def change(method, payload=None, **kw):
        response = original(method, payload, **kw)
        if method == stage:
            session.data['offers/get_from_filter'][-1]['CONTACT_ID'] = 9999
        return response
    monkeypatch.setattr(tools.client, 'call', change)
    result = tools.apply_draft(selection['token'])
    assert result['status'] == 'applied', result
    oid = result['result']['OFFER_ID']
    assert writes(session)[2]['payload']['DATA']['OFFER_ID'] == oid
    assert writes(session)[3]['payload']['OFFER_ID'] == oid


def test_new_creation_uses_system_contact_but_does_not_verify_row_contact(env, monkeypatch):
    session, tools, pipe = env
    selection = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    assert 'фактический контакт сделки не сверяется' in selection['summary']
    original = tools._search_offer_row
    def wrong_initial(oid):
        row, n = original(oid)
        return ({**row, 'CONTACT_ID': 9999} if row else row), n
    monkeypatch.setattr(tools, '_search_offer_row', wrong_initial)
    result = tools.apply_draft(selection['token'])
    assert result['status'] == 'applied'
    assert writes(session)[0]['payload']['PHONE'] == '81111111110'
    assert writes(session)[0]['payload']['CONTACT_ID'] == 5555
    assert result['result']['REQUESTED_CONTACT_ID'] == 5555


def test_wrong_number_at_apply_blocks_write(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    result = send(pipe, 'быстрая сделка №1900 диагностика 1000 рублей')
    assert result.drafts, result.answer
    monkeypatch.setattr(tools, '_search_offer_row', lambda oid: ({'OFFER_ID': 9999, 'BOARD_ID': 2, 'STATUS_NAME': 'Быстрые сделки'}, 1))
    with pytest.raises(ValueError, match='номер не совпал'):
        tools.apply_draft(result.drafts[0]['token'])
    assert not writes(session)


def test_old_v109_draft_does_not_apply_under_new_rules(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='quick_deal', method='quick_deal', payload={'contact_id': 5555,
        'board_id': 2, 'status_id': 51, 'offer_id': 1900, 'work': {'title': 'Диагностика', 'price': 1000},
        'articles': [], 'confirmation_policy': 'status_name_follow_created_v109'}, summary='Старые условия',
        extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(old['token'])
    assert not writes(session)


def test_list_and_card_do_not_require_contact_id(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offers/get_from_filter'][-1].pop('CONTACT_ID')
    result = send(pipe, 'Покажи быстрые сделки')
    assert result.choices and 1900 in result.choices[0]['offers']
    reject_scans(monkeypatch, tools)
    detail = pipe.quick_read_choice(result.choices[0], 1, result.choices[0]['offers'].index(1900))
    assert '№1900' in detail.answer and 'Контакт или статус' not in detail.answer
    assert not writes(session)


def test_selected_status_must_match_before_confirmation_and_apply(env):
    session, tools, pipe = env
    existing(session, 1900)
    row = session.data['offers/get_from_filter'][-1]
    row['STATUS_NAME'] = 'Мусор'
    assert not send(pipe, 'быстрая сделка №1900 диагностика 1000 рублей').drafts
    row['STATUS_NAME'] = 'Быстрые сделки'
    draft = send(pipe, 'быстрая сделка №1900 диагностика 1000 рублей').drafts[0]
    row['STATUS_NAME'] = 'Мусор'
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_existing_foreign_number_from_creation_is_rejected(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offers/get_from_filter'][-1]['CONTACT_ID'] = 8123
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    original = tools.client.call
    def incorrect(method, payload=None, **kw):
        response = original(method, payload, **kw)
        return {'OFFER_ID': 1900} if method == 'offer/new/with_contact' else response
    monkeypatch.setattr(tools.client, 'call', incorrect)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial'
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact']
