"""Составная запись в существующую сделку с любым статусом: только имитация CRM."""
import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.client import StocrmError
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize('status', ['Заказ', 'Запись', 'Мусор', 'Быстрые сделки'])
def test_one_confirmation_separate_writes_and_no_status_or_contact_check(env, monkeypatch, status):
    session, tools, pipe = env
    existing(session, 1900)
    row = session.data['offers/get_from_filter'][-1]
    row.update(STATUS_NAME=status, STATUS_ID=12, CONTACT_ID=777, BOARD_ID=20)
    monkeypatch.setattr(quick_read, 'status_identity', lambda _: pytest.fail('Не проверять статус сделки'))
    monkeypatch.setattr(quick_read, 'contact_identity', lambda _: pytest.fail('Не проверять контакт сделки'))
    result = send(pipe, 'Добавь в сделку №1900 услугу диагностика 500 и товар артикул NGK CR9E за 300')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    draft = result.drafts[0]
    assert '№1900' in draft['summary'] and 'диагностика' in draft['summary']
    assert 'CR9E' in draft['summary'] and 'NGK' in draft['summary'] and 'без проверки' in draft['summary']
    row.update(STATUS_NAME='Мусор', CONTACT_ID=888, BOARD_ID=22)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'applied' and outcome['verified'], outcome
    assert 'Сделка №1900:' in outcome['result']['SUMMARY']
    calls = writes(session)
    assert [c['method'] for c in calls] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert calls[0]['payload']['DATA']['OFFER_ID'] == 1900 and calls[0]['payload']['DATA']['SUM'] == 500
    assert calls[1]['payload']['OFFER_ID'] == 1900 and calls[1]['payload']['SKU'] == 'CR9E'
    assert calls[2]['payload']['PRICE'] == 300 and calls[2]['payload']['BRAND'] == 'NGK'
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 3


def test_unrecognized_name_still_triggers_composite(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь в сделку 1900 прокачка тормозов 1000 и артикул NGK CR9E за 300')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    assert result.drafts[0]['payload']['work'] == {'title': 'прокачка тормозов', 'price': 1000}


def test_missing_price_brand_and_number_asked_without_guessing(env):
    session, tools, pipe = env
    existing(session, 1900)
    pipe._last_offer[1] = 1901
    result = send(pipe, 'Добавь услугу диагностика и артикул 12340987')
    assert 'цен' in result.answer.lower() and 'работ' in result.answer.lower() and not writes(session)
    result = send(pipe, '500')
    assert 'цен' in result.answer.lower() and '12340987' in result.answer and not writes(session)
    result = send(pipe, '700')
    assert 'бренд' in result.answer.lower() and not writes(session)
    result = send(pipe, 'TestBrand')
    assert 'номер сделки' in result.answer.lower() and not writes(session)
    result = send(pipe, '№1900')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    payload = result.drafts[0]['payload']
    assert payload['offer_id'] == 1900 and payload['work']['price'] == 500
    assert payload['articles'][0]['price'] == 700 and payload['articles'][0]['brand'] == 'TestBrand'
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'


@pytest.mark.parametrize('text', [
    'не добавь в сделку 1900 услугу диагностика 500 и товар NGK CR9E за 300',
    'добавь в сделку 1900 услугу диагностика 500 и товар NGK CR9E за 300 и оплати 500',
    'добавь в сделку 1900 услугу диагностика 500 и товар NGK CR9E за 300 и сделай звонок',
    'добавь в сделку 1900 услугу диагностика 500 и товар NGK CR9E за 300 и доставку 400',
    'добавь в сделку 1900 услугу диагностика 500 и замена масла 900 и артикул NGK CR9E за 300',
    'добавь в сделку 1900 услугу диагностика 500 и товар NGK CR9E за 300 со склада',
    'добавь в сделку 1900 услугу диагностика 500 и закажи артикул NGK CR9E за 300 у поставщика',
    'добавь услугу диагностика 500 в сделку №1900 и артикул NGK CR9E за 300 в сделку №1901',
    'добавь в сделку №0 услугу диагностика 500 и товар NGK CR9E за 300',
])
def test_other_actions_ambiguous_or_bad_number_never_partially_prepare(env, text):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, text)
    assert not result.drafts and not result.applied and not result.choices and not writes(session), result.answer


@pytest.mark.parametrize('failed_method', ['work/add', 'offer/require/add', 'offer/order/add_free_order'])
def test_lost_response_stops_without_repeating_any_write(env, monkeypatch, failed_method):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 300').drafts[0]
    call = tools.client.call
    def lost(method, payload=None, **kwargs):
        result = call(method, payload, **kwargs)
        if method == failed_method:
            raise StocrmError('ответ потерян')
        return result
    monkeypatch.setattr(tools.client, 'call', lost)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    calls = writes(session)
    expected = ['work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert [c['method'] for c in calls] == expected[:expected.index(failed_method) + 1]
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == len(calls)
    assert not send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 300').drafts


def test_number_mismatch_blocks_before_writes(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, '_search_offer_row', lambda oid: ({'OFFER_ID': oid + 1}, 1))
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и товар NGK CR9E за 300')
    assert not result.drafts and not writes(session)

@pytest.mark.parametrize('kind', ['work', 'part'])
def test_composite_partial_blocks_separate_position_apply(env, monkeypatch, kind):
    session, tools, pipe = env
    existing(session, 1900)
    if kind == 'work':
        separate = tools.draft_add_offer_work(offer_id=1900, title='диагностика', sum_value=500)
    else:
        separate = tools.draft_add_offer_part(offer_id=1900, name='CR9E', sku='CR9E', brand='NGK', price=300)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 300').drafts[0]
    call = tools.client.call
    def lost(method, payload=None, **kwargs):
        result = call(method, payload, **kwargs)
        if method == 'work/add':
            raise StocrmError('ответ потерян')
        return result
    monkeypatch.setattr(tools.client, 'call', lost)
    assert tools.apply_draft(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError, match='непроверенная запись'):
        tools.apply_draft(separate['token'])
    assert [c['method'] for c in writes(session)] == ['work/add']


@pytest.mark.parametrize('kind', ['work', 'part'])
def test_separate_uncertain_operation_blocks_composite_apply(env, kind):
    session, tools, pipe = env
    existing(session, 1900)
    if kind == 'work':
        separate = tools.draft_add_offer_work(offer_id=1900, title='диагностика', sum_value=500)
    else:
        separate = tools.draft_add_offer_part(offer_id=1900, name='CR9E', sku='CR9E', brand='NGK', price=300)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 300').drafts[0]
    tools.drafts.mark(separate['token'], 'partial', check_warning='ответ потерян')
    with pytest.raises(ValueError, match='непроверенная запись'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_unfamiliar_work_without_price_is_not_dropped(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь в сделку 1900 прокачку тормозов и артикул NGK CR9E за 300')
    assert not result.drafts and 'цен' in result.answer.lower() and not writes(session), result.answer
    result = send(pipe, '1000')
    assert len(result.drafts) == 1 and result.drafts[0]['payload']['work']['title'] == 'прокачку тормозов'
    assert result.drafts[0]['payload']['articles'][0]['sku'] == 'CR9E'
    assert not writes(session)
