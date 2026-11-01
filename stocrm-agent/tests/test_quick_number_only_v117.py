"""Быстрая сделка: после запроса перевода — только номер, без живой CRM."""
import pytest

from stocrm_agent.bot import quick_parse, quick_read
from stocrm_agent.client import StocrmError
from stocrm_agent.report import format_apply_result
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def test_exact_user_phrase_asks_for_brand_then_creates_work_and_part(env, monkeypatch):
    session, tools, pipe = env
    text = 'Добавь быструю сделку, диагностика 500, артикул 12340987 за 500'
    plan = quick_parse.parse(text)
    assert plan['new'] and plan['work'] == {'title': 'диагностика', 'price': 500}
    assert plan['articles'] == [{'sku': '12340987', 'brand': None, 'qty': 1, 'price': 500}]
    first = send(pipe, text)
    assert not first.drafts and 'бренд' in first.answer.lower() and not writes(session)
    next_result = send(pipe, 'TestBrand')
    assert next_result.drafts and not writes(session)
    draft = next_result.drafts[0]
    assert 'TestBrand' in draft['summary'] and '500' in draft['summary']
    assert 'без проверки' in draft['summary'].lower() and 'фактический перевод' in draft['summary'].lower()
    real_call = tools.client.call
    real_status_identity = quick_read.status_identity
    def status_identity(tools_arg):
        if any(c['method'] == 'offer/status/change' for c in writes(session)):
            pytest.fail('Нельзя проверять статус в справочнике после запроса перевода')
        return real_status_identity(tools_arg)
    monkeypatch.setattr(quick_read, 'status_identity', status_identity)
    def move_after_change(method, payload=None, **kwargs):
        response = real_call(method, payload, **kwargs)
        if method == 'offer/status/change':
            session.data['offers/get_from_filter'][-1].update(
                STATUS_ID=15, STATUS_NAME='Запись', CONTACT_ID=555555, BOARD_ID=9)
        return response
    monkeypatch.setattr(tools.client, 'call', move_after_change)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result['result']['status_not_read_back'] is True
    calls = writes(session)
    assert [c['method'] for c in calls] == ['offer/new/with_contact', 'offer/status/change', 'work/add',
                                             'offer/require/add', 'offer/order/add_free_order']
    oid = result['result']['OFFER_ID']
    assert calls[0]['payload']['CONTACT_ID'] == 5555 and calls[0]['payload']['PHONE'] == '81111111110'
    assert calls[1]['payload']['OFFER_ID'] == oid and calls[1]['payload']['STATUS_ID'] == 51
    assert calls[2]['payload']['DATA']['OFFER_ID'] == oid and calls[2]['payload']['DATA']['SUM'] == 500
    assert calls[3]['payload']['OFFER_ID'] == oid and calls[3]['payload']['SKU'] == '12340987'
    assert calls[4]['payload']['PRICE'] == 500 and calls[4]['payload']['BRAND'] == 'TestBrand'
    assert 'текущий статус не проверялся' in result['result']['SUMMARY']
    rendered = format_apply_result(result['method'], result['result'])
    assert 'текущий статус не проверялся' in rendered.lower()
    assert 'статус подтверждён' not in rendered.lower()


@pytest.mark.parametrize('prefix', ['№', ''])
def test_explicit_number_does_not_load_status_or_contact_and_stays_same(env, monkeypatch, prefix):
    session, tools, pipe = env
    existing(session, 1900)
    row = session.data['offers/get_from_filter'][-1]
    row.update(STATUS_NAME='Запись', STATUS_ID=12, CONTACT_ID=9999, BOARD_ID=20)
    def disallow(_):
        pytest.fail('При работе по указанному номеру не нужен поиск статуса или контакта')
    monkeypatch.setattr(quick_read, 'status_identity', disallow)
    monkeypatch.setattr(quick_read, 'contact_identity', disallow)
    result = send(pipe, f'Добавь в быструю сделку {prefix}1900 диагностика 500 и артикул NGK CR9E за 500')
    assert result.drafts and not writes(session), result.answer
    draft = result.drafts[0]
    assert draft['payload']['offer_id'] == 1900 and 'без проверки' in draft['summary']
    row.update(STATUS_NAME='Мусор', CONTACT_ID=8888, BOARD_ID=21)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert writes(session)[0]['payload']['DATA']['OFFER_ID'] == 1900
    assert writes(session)[1]['payload']['OFFER_ID'] == 1900


def test_list_then_selected_deal_follows_number_even_if_status_reference_changes(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    choice = send(pipe, 'Добавь в быструю сделку диагностика 500').choices[0]
    session.data['offers/get_from_filter'][-1].update(STATUS_NAME='Запись', CONTACT_ID=9000, BOARD_ID=20)
    monkeypatch.setattr(quick_read, 'status_identity', lambda _: pytest.fail('После выбора статус не проверяем'))
    result = pipe.quick_read_choice(choice, 1, 0)
    assert result.drafts and not writes(session), result.answer
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'
    assert writes(session)[0]['payload']['DATA']['OFFER_ID'] == 1900


@pytest.mark.parametrize('reply', [False, 0, {'error': 'rejected'}, {'SUCCESS': False}])
def test_explicit_rejection_of_status_request_stays_partial_without_items_or_retry(env, monkeypatch, reply):
    session, tools, pipe = env
    draft = send(pipe, 'Добавь быструю сделку диагностика 500 и артикул NGK CR9E за 500').drafts[0]
    original = tools.client.call
    def reject(method, payload=None, **kwargs):
        actual = original(method, payload, **kwargs)
        return reply if method == 'offer/status/change' else actual
    monkeypatch.setattr(tools.client, 'call', reject)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and 'отказ' in outcome['warning']
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 2


def test_status_timeout_after_write_stops_and_does_not_retry(env, monkeypatch):
    session, tools, pipe = env
    draft = send(pipe, 'Добавь быструю сделку диагностика 500 и артикул NGK CR9E за 500').drafts[0]
    original = tools.client.call
    def lost(method, payload=None, **kwargs):
        response = original(method, payload, **kwargs)
        if method == 'offer/status/change':
            raise StocrmError('ответ потерян')
        return response
    monkeypatch.setattr(tools.client, 'call', lost)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 2


def test_response_without_details_never_claims_status_verified(env, monkeypatch):
    session, tools, pipe = env
    draft = send(pipe, 'Добавь быструю сделку диагностика 500').drafts[0]
    original = tools.client.call
    def no_details(method, payload=None, **kwargs):
        response = original(method, payload, **kwargs)
        return None if method == 'offer/status/change' else response
    monkeypatch.setattr(tools.client, 'call', no_details)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result['result']['status_not_read_back']
    assert 'запрос перевода' in result['result']['SUMMARY']
    assert 'текущий статус не проверялся' in result['result']['SUMMARY']
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_unknown_number_and_other_number_result_block_before_write(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    real = tools._search_offer_row
    monkeypatch.setattr(tools, '_search_offer_row', lambda oid: ({'OFFER_ID': oid + 1}, 1))
    result = send(pipe, 'Добавь в быструю сделку №1900 диагностика 500')
    assert not result.drafts and not writes(session)
    monkeypatch.setattr(tools, '_search_offer_row', real)
    draft = send(pipe, 'Добавь в быструю сделку №1900 диагностика 500').drafts[0]
    monkeypatch.setattr(tools, '_search_offer_row', lambda oid: (None, 0))
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_v113_pending_policy_requires_new_confirmation(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='quick_deal', method='quick_deal', payload={'contact_id': 5555,
        'board_id': 2, 'status_id': 51, 'offer_id': 1900, 'work': {'title': 'Диагностика', 'price': 500},
        'articles': [], 'confirmation_policy': 'work_title_sum_confirmation_v113'}, summary='Старые условия',
        extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(old['token'])
    assert not writes(session)


def test_zero_number_is_not_misrouted_to_a_choice(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь в быструю сделку №0 диагностика 500')
    assert not result.drafts and not result.choices and not writes(session)
