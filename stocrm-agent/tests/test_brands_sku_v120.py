"""Словарь и защита артикула проверяются на тексте, не на живом STT/CRM."""
import pytest
from stocrm_agent import parts, quick_deal
from stocrm_agent.bot import quick_parse
from stocrm_agent.drafts import _fingerprint
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize(('spoken', 'expected'), [
    ('феби', 'FEBI'), ('феби бильштайн', 'FEBI'), ('фебест', 'FEBEST'),
    ('адвикс', 'ADVICS'), ('линкс', 'LYNXAUTO'), ('линкс авто', 'LYNXAUTO'),
    ('эн джи кей', 'NGK'), ('каяба', 'KYB'), ('лемфердер', 'LEMFORDER'),
    ('стеллокс', 'STELLOX'), ('айсин', 'AISIN'), ('асакаши', 'ASAKASHI'),
    ('кнехт', 'KNECHT'), ('мале', 'MAHLE'), ('джей пи групп', 'JP GROUP'),
    ('ликви моли', 'LIQUI MOLY'), ('ти ар дабл ю', 'TRW'),
])
def test_spoken_brand_paths(spoken, expected):
    assert parts.find_brand_in_text('фильтр ' + spoken + ' 12345') == expected
    assert parts.explicit_brand_in_text('бренд ' + spoken + ' артикул 12345') == expected
    assert parts.manual_brand(spoken) == expected
    assert parts.exact_brand_match(spoken, expected)


def test_brands_are_not_guessed_by_prefix_or_substring():
    assert parts.canonical_brand('FEBEST') == 'FEBEST'
    assert not parts.exact_brand_match('FEBEST', 'FEBI')
    assert parts.canonical_brand('FEBIUNKNOWN') == 'FEBIUNKNOWN'
    assert parts.find_brand_in_text('патронник 12345') is None


@pytest.mark.parametrize(('value', 'expected'), [
    ('фк-2010', 'FK-2010'), ('GDв1550', 'GDB1550'), ('w914/2', 'W914/2'),
    ('0 451 103 314', '0 451 103 314'), ('12345', '12345'),
])
def test_sku_normalization(value, expected):
    assert parts.sku_for_write(value) == expected
    assert parts.sku_is_latin(expected)


@pytest.mark.parametrize('value', ['БW1234', 'ABЖ1234', 'AБ1234', 'ABΩ1234', 'ß1234', 'фь1234', 'ABC１２３４', 'ABC@1234'])
def test_sku_ambiguous_input_rejected(value):
    with pytest.raises(ValueError):
        parts.sku_for_write(value)


def test_multiple_russian_brands_assigned_to_nearest_article(env):
    session, tools, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и товар феби 12345 за 300 и линкс ABC1234 за 700')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    items = result.drafts[0]['payload']['articles']
    assert [(p['sku'], p['brand'], p['price']) for p in items] == [('12345', 'FEBI', 300), ('ABC1234', 'LYNXAUTO', 700)]
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'
    assert [c['payload']['BRAND'] for c in writes(session) if c['method'] == 'offer/order/add_free_order'] == ['FEBI', 'LYNXAUTO']


@pytest.mark.parametrize('bad', ['БW1234', 'ABЖ1234', 'ABΩ1234', 'ß1234', 'фь1234'])
def test_bad_sku_no_partial_draft_or_write(env, bad):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, f'в сделку 1900 добавь услугу диагностика 500 и товар NGK {bad} за 300')
    assert not result.drafts and not writes(session) and 'артикул' in result.answer.lower(), result.answer


def test_explicit_input_is_normalized_before_confirmation(env):
    session, tools, _ = env
    existing(session, 1900)
    draft = tools.draft_add_offer_part(offer_id=1900, name='фильтр', sku='фк-2010', brand='феби', price=300)
    assert draft['check']['sku'] == 'FK-2010' and draft['check']['brand'] == 'FEBI'
    assert not writes(session)
    assert tools.apply_draft(draft['token'])['status'] == 'applied'
    assert all(c['payload']['SKU'] == 'FK-2010' for c in writes(session) if 'SKU' in c['payload'])


def test_ambiguous_direct_and_composite_rejected(env):
    session, tools, _ = env
    existing(session, 1900)
    with pytest.raises(ValueError, match='артикул'):
        tools.draft_add_offer_part(offer_id=1900, name='фильтр', sku='ABЖ1234', brand='NGK', price=300)
    with pytest.raises(ValueError, match='артикул'):
        quick_deal.prepare(tools, (0, 0, 0), 1900, {'title': 'диагностика', 'price': 500}, [{'sku': 'ABЖ1234', 'brand': 'NGK', 'qty': 1, 'price': 300}])
    assert not writes(session)


@pytest.mark.parametrize('kind', ['single', 'composite'])
def test_old_cyrillic_drafts_blocked_before_any_write(env, kind):
    session, tools, _ = env
    existing(session, 1900)
    if kind == 'single':
        draft = tools.draft_add_offer_part(offer_id=1900, name='товар', sku='AB1234', brand='NGK', price=300)
    else:
        draft = quick_deal.prepare(tools, (0, 0, 0), 1900, {'title': 'диагностика', 'price': 500}, [{'sku': 'AB1234', 'brand': 'NGK', 'qty': 1, 'price': 300}])
    with tools.drafts._locked():
        old = tools.drafts._data[draft['token']]
        if kind == 'single':
            old['check']['sku'] = 'ABЖ1234'
            for step in old['steps']:
                step['payload']['SKU'] = 'ABЖ1234'
        else:
            old['payload']['articles'][0]['sku'] = 'ABЖ1234'
        old['fingerprint'] = _fingerprint(old['method'], old['payload'], old['steps'])
        tools.drafts._save()
    with pytest.raises(ValueError, match='артикул'):
        tools.apply_draft(draft['token'])
    assert not writes(session) and tools.drafts.get(draft['token'])['status'] == 'pending'
