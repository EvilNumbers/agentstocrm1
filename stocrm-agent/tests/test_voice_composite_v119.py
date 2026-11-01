"""Голосовой сценарий проверяется по распознанному тексту, без живой CRM и STT."""
import pytest
from stocrm_agent import quick_deal
from stocrm_agent.bot import quick_parse
from stocrm_agent.client import StocrmError
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize('phrase', [
    'Добавь в сделку №1900 услугу диагностика за 500, замену масла за 1000 и артикул NGK CR9E за 700',
    'В сделку 1900 добавь услугу диагностика 500 и услугу замена масла 1000 и товар NGK CR9E за 700',
    'Добавь товар NGK CR9E за 700 и услугу диагностика 500 и услугу замена масла 1000 в сделку 1900',
])
def test_multiple_work_one_draft_and_sequential_writes(env, phrase):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offers/get_from_filter'][-1].update(STATUS_NAME='Заказ', BOARD_ID=20, CONTACT_ID=123)
    result = send(pipe, phrase)
    assert len(result.drafts) == 1 and not writes(session), result.answer
    draft = result.drafts[0]
    positions = quick_deal.positions(draft['payload'])
    assert len(positions) == 3 and [p['price'] for p in positions] == [500, 1000, 700]
    assert '№1900' in draft['summary'] and 'CR9E' in draft['summary'] and 'распознавание речи' in draft['summary']
    assert tools.apply_draft(draft['token'])['status'] == 'applied'
    calls = writes(session)
    assert [c['method'] for c in calls] == ['work/add', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert [c['payload']['DATA']['SUM'] for c in calls[:2]] == [500, 1000]
    assert all(c['payload']['DATA']['OFFER_ID'] == 1900 for c in calls[:2])


def test_multi_new_deal_with_legacy_quick_scenario(env):
    session, tools, pipe = env
    result = send(pipe, 'новая быстрая сделка диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    outcome = tools.apply_draft(result.drafts[0]['token'])
    assert outcome['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_second_work_timeout_blocks_later_positions_and_retries(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700').drafts[0]
    original = tools.client.call
    count = 0
    def lost(method, payload=None, **kwargs):
        nonlocal count
        result = original(method, payload, **kwargs)
        if method == 'work/add':
            count += 1
            if count == 2:
                raise StocrmError('ответ потерян')
        return result
    monkeypatch.setattr(tools.client, 'call', lost)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and 'диагностика' in result['warning']
    assert [c['method'] for c in writes(session)] == ['work/add', 'work/add']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 2


def test_voice_price_quantity_and_removal_rebuilds_confirmation(env):
    session, tools, pipe = env
    existing(session, 1900)
    first = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и фильтр MANN W914/2 за 700').drafts[0]
    second_result = send(pipe, 'У фильтра цена 800')
    assert len(second_result.drafts) == 1, second_result.answer
    second = second_result.drafts[0]
    assert second['payload']['articles'][0]['price'] == 800
    assert 'W914/2' in second_result.answer and not writes(session)
    with pytest.raises(ValueError):
        tools.apply_draft(first['token'])
    third_result = send(pipe, 'Количество товара два')
    assert len(third_result.drafts) == 1, third_result.answer
    third = third_result.drafts[0]
    assert third['payload']['articles'][0]['qty'] == 2
    with pytest.raises(ValueError):
        tools.apply_draft(second['token'])
    fourth_result = send(pipe, 'Убери диагностику')
    assert len(fourth_result.drafts) == 1, fourth_result.answer
    fourth = fourth_result.drafts[0]
    assert [p['title'] for p in quick_deal.positions(fourth['payload'])] == ['замена масла', 'W914/2']
    assert not writes(session)
    with pytest.raises(ValueError):
        tools.apply_draft(third['token'])
    assert tools.apply_draft(fourth['token'])['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_edit_price_by_number_and_unique_work_name(env):
    session, tools, pipe = env
    existing(session, 1900)
    first = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700').drafts[0]
    changed = send(pipe, 'Цена позиции 2 1200')
    assert len(changed.drafts) == 1, changed.answer
    assert changed.drafts[0]['payload']['extra_works'][0]['price'] == 1200
    changed2 = send(pipe, 'У диагностики цена 600')
    assert len(changed2.drafts) == 1, changed2.answer
    assert changed2.drafts[0]['payload']['work']['price'] == 600
    with pytest.raises(ValueError):
        tools.apply_draft(first['token'])
    assert not writes(session)


def test_ambiguous_selection_and_wrong_values_leave_draft_unchanged(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700').drafts[0]
    for message in ('У услуги цена 800', 'Количество услуги два', 'Цена позиции 2 0', 'Количество товара ноль'):
        result = send(pipe, message)
        assert not result.drafts and not writes(session), result.answer
        assert tools.drafts.get(draft['token'])['status'] == 'pending'


@pytest.mark.parametrize('phrase', [
    'в сделку 13 25 добавь услугу диагностика 500 и товар NGK CR9E за 700',
    'в сделку 1900 добавь услугу диагностика 500 и артикул 123 456 за 700',
    'в сделку 1900 добавь диагностика и замена масла 1000 и артикул NGK CR9E за 700',
    'в сделку 1900 добавь диагностика, замена масла 1000 и артикул NGK CR9E за 700',
])
def test_uncertain_voice_numbers_and_ambiguous_services_do_not_create_draft(env, phrase):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, phrase)
    assert not result.drafts and not result.applied and not writes(session), result.answer


def test_wrong_user_and_stale_or_applied_draft_cannot_be_edited(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 700').drafts[0]
    assert pipe._edit_pending_composite('У товара цена 800', 2) is None
    tools.drafts.mark(draft['token'], 'partial')
    result = send(pipe, 'У товара цена 800')
    assert not result.drafts and 'не изменён' in result.answer.lower()
    assert tools.drafts.get(draft['token'])['status'] == 'partial' and not writes(session)


def test_existing_v118_draft_without_extra_works_remains_valid(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = quick_deal.prepare(tools, (0, 0, 0), 1900, {'title': 'диагностика', 'price': 500}, [])
    # Симулируем неизменённый payload v118 и его прежний fingerprint.
    old = tools.drafts.get(draft['token'])
    from stocrm_agent.drafts import _fingerprint
    with tools.drafts._locked():
        current = tools.drafts._data[old['token']]
        current['payload'].pop('extra_works')
        current['payload']['confirmation_policy'] = 'deal_number_only_v117'
        current['fingerprint'] = _fingerprint(current['method'], current['payload'], current['steps'])
        tools.drafts._save()
    assert tools.apply_draft(old['token'])['status'] == 'applied'


def test_multiple_named_works_and_filter_natural_example(env):
    session, tools, pipe = env
    existing(session, 1900)
    text = 'В сделку 1900 добавь диагностику за 500, замену масла за 1000 и фильтр MANN W914/2 за 700'
    result = send(pipe, text)
    assert len(result.drafts) == 1 and not writes(session), result.answer
    p = result.drafts[0]['payload']
    assert p['work']['price'] == 500 and p['extra_works'][0]['price'] == 1000
    assert p['articles'] == [{'sku': 'W914/2', 'brand': 'MANN', 'qty': 1, 'price': 700, 'product_hint': 'фильтр'}]
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'


def test_multi_articles_brands_stay_with_their_skus(env):
    session, tools, pipe = env
    existing(session, 1900)
    text = 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700 и товар MANN W914/2 за 300'
    result = send(pipe, text)
    assert len(result.drafts) == 1 and not writes(session), result.answer
    articles = result.drafts[0]['payload']['articles']
    assert [(x['sku'], x['brand'], x['price']) for x in articles] == [('CR9E', 'NGK', 700), ('W914/2', 'MANN', 300)]
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'
    assert [c['payload']['BRAND'] for c in writes(session) if c['method'] == 'offer/order/add_free_order'] == ['NGK', 'MANN']


def test_filter_word_inside_service_does_not_become_product(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу замена фильтра 1000 и артикул NGK CR9E за 300')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    assert result.drafts[0]['payload']['work']['title'] == 'замена фильтра'


def test_generic_filter_edit_never_changes_filter_replacement_work(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу замена фильтра 1000 и фильтр MANN W914/2 за 300')
    assert len(result.drafts) == 1, result.answer
    changed = send(pipe, 'У фильтра цена 800')
    assert changed.drafts[0]['payload']['work']['price'] == 1000
    assert changed.drafts[0]['payload']['articles'][0]['price'] == 800 and not writes(session)


def test_missing_price_on_second_work_is_asked_without_borrowing(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла и артикул NGK CR9E за 700')
    assert not result.drafts and 'цен' in result.answer.lower() and 'замена масла' in result.answer.lower()
    result = send(pipe, '1000')
    assert len(result.drafts) == 1 and result.drafts[0]['payload']['extra_works'][0]['price'] == 1000
    assert not writes(session)


def test_quick_extra_actions_are_not_misparsed_as_services(env):
    session, _, pipe = env
    for text in ['новая быстрая сделка диагностика 500 и позвони клиенту 1000',
                 'новая быстрая сделка диагностика 500 и создай задачу 1000']:
        result = send(pipe, text)
        assert not result.drafts and not writes(session), result.answer


def test_multi_work_policy_is_not_accepted_by_previous_version(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и замена масла 1000 и артикул NGK CR9E за 700').drafts[0]
    assert draft['payload']['confirmation_policy'] == 'multi_work_v119'
    assert draft['payload']['confirmation_policy'] != 'deal_number_only_v117'
    assert not writes(session)


def test_unknown_filter_type_does_not_guess_a_product(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу замена фильтра 1000 и артикул NGK CR9E за 300').drafts[0]
    changed = send(pipe, 'У фильтра цена 800')
    assert not changed.drafts and 'не изменён' in changed.answer.lower()
    assert tools.drafts.get(draft['token'])['status'] == 'pending' and not writes(session)


def test_filter_with_sku_and_missing_work_price_is_not_dropped(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь диагностику и фильтр MANN W914/2 за 700')
    assert not result.drafts and 'цен' in result.answer.lower() and not writes(session)
    result = send(pipe, '500')
    assert len(result.drafts) == 1 and result.drafts[0]['payload']['work']['price'] == 500
    assert result.drafts[0]['payload']['articles'][0]['sku'] == 'W914/2'


def test_old_draft_cannot_be_changed_after_application_started(env):
    session, tools, pipe = env
    existing(session, 1900)
    draft = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и артикул NGK CR9E за 700').drafts[0]
    tools.drafts.claim(draft['token'])
    result = send(pipe, 'У товара цена 800')
    assert not result.drafts and 'не изменён' in result.answer.lower()
    assert tools.drafts.get(draft['token'])['status'] == 'applying' and not writes(session)


def test_count_of_services_must_match_recognized_positions(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь в сделку 1900 две услуги диагностика 500 и замена масла 1000 и фильтр MANN W914/2 за 700')
    assert len(result.drafts) == 1 and result.drafts[0]['payload']['work']['title'] == 'диагностика'
    assert not writes(session)


def test_mismatched_service_count_does_not_make_partial_draft(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь в сделку 1900 три услуги диагностика 500 и замена масла 1000 и фильтр MANN W914/2 за 700')
    assert not result.drafts and 'количество' in result.answer.lower() and not writes(session)
