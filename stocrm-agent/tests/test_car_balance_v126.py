"""Карточки автомобилей и баланс волны 80; только макеты, без записи в CRM."""
import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.bot.fastpath import format_offer_card
from stocrm_agent.client import StocrmError
from stocrm_agent.mock import MockResponse
from stocrm_agent.tools import _offer_balance_values
from test_quick_read import env  # noqa: F401


def tools_for(env):
    session, pipe = env
    return session, pipe.tools


def test_card_requests_documented_fields_and_quick_cards_show_attached_car(env):
    session, tools = tools_for(env)
    row = next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)
    row.update(CAR_TITLE_TABLE='Toyota Corolla', CAR_NUMBER_FLAT='А123ВС', CAR_PROFILE_ID=77)
    card = tools.offer_card(1900, with_items=True)
    call = next(c for c in session.calls if c['method'] == 'offers/get_from_filter')
    assert {'CAR_TITLE_TABLE', 'CAR_NUMBER_FLAT', 'CAR_PROFILE_ID'} <= set(call['payload']['REQUIRED_FIELDS'])
    assert '🚗 Автомобиль: Toyota Corolla · А123ВС' in format_offer_card(card, detailed=True)
    assert '🚗 Автомобиль: Toyota Corolla · А123ВС' in quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert 'Toyota Corolla' in quick_read.status_details(tools, (2, 51), {1900: 5555})[0]
    assert not any(c['method'] == 'car_profile/get_filtered_profiles' for c in session.calls)
    assert not any(c['method'] in {'offer/new/with_contact', 'work/add', 'offer/status/change', 'offer/order/add_free_order'} for c in session.calls)


def test_car_fields_survive_panel_that_returns_only_requested_columns(env, monkeypatch):
    session, tools = tools_for(env)
    row = next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)
    row.update(CAR_TITLE_TABLE='Nissan Note', CAR_NUMBER_FLAT='К222КК', CAR_PROFILE_ID=77)
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('/offers/get_from_filter'):
            data = response.json()
            fields = set(json['REQUIRED_FIELDS'])
            data['RESPONSE'] = [{k: v for k, v in r.items() if k in fields} for r in data['RESPONSE']]
            return MockResponse(data)
        return response
    monkeypatch.setattr(session, 'post', post)
    assert 'Автомобиль: Nissan Note · К222КК' in quick_read.status_detail(tools, (2, 51), 1900, 5555)


def test_missing_car_does_not_pick_only_client_car(env):
    session, tools = tools_for(env)
    session.data['car_profile/get_filtered_profiles'] = {'DATA': [{'CAR_PROFILE_ID': 99, 'CONTACT_ID': 5555, 'TITLE': 'Чужая машина'}]}
    card = tools.offer_card(1900)
    assert 'Автомобиль: не указан или не получен' in format_offer_card(card)
    assert 'Чужая машина' not in quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert not any(c['method'] == 'car_profile/get_filtered_profiles' for c in session.calls)


def test_exact_profile_and_contact_used_when_title_missing(env, monkeypatch):
    session, tools = tools_for(env)
    row = next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)
    row.update(CAR_PROFILE_ID=77, CAR_TITLE_TABLE=False)
    original = tools._read
    def read(tool, method, payload=None):
        if method == 'car_profile/get_filtered_profiles':
            assert payload['FILTER'] == {'CAR_PROFILE_ID': 77}
            return {'DATA': [{'CAR_PROFILE_ID': '77', 'CONTACT_ID': '5555', 'TITLE': 'Mazda 3', 'LICENSE_PLATE_NUMBER': 'А777АА', 'VIN': 'TESTVIN'}]} if payload['PAGE'] == 1 else {'DATA': []}
        return original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)
    for text in (format_offer_card(tools.offer_card(1900)), quick_read.status_detail(tools, (2, 51), 1900, 5555)):
        assert 'Автомобиль: Mazda 3 · А777АА' in text and 'VIN: TESTVIN' in text


@pytest.mark.parametrize('responses', [
    [[{'CAR_PROFILE_ID': 78, 'CONTACT_ID': 5555, 'TITLE': 'wrong id'}], []],
    [[{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 42, 'TITLE': 'wrong owner'}], []],
    [[{'CAR_PROFILE_ID': 77, 'TITLE': 'missing owner'}], []],
    [[{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'duplicate'}, {'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'duplicate'}]],
    [[{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'duplicate later'}], [{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'duplicate later'}]],
    [[{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'incomplete'}], None],
    [[{'CAR_PROFILE_ID': 77, 'CONTACT_ID': 5555, 'TITLE': 'unknown page'}], {'OTHER': []}],
])
def test_unverified_profile_not_displayed(env, monkeypatch, responses):
    session, tools = tools_for(env)
    next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)['CAR_PROFILE_ID'] = 77
    original = tools._read
    def read(tool, method, payload=None):
        if method == 'car_profile/get_filtered_profiles':
            return responses[min(payload['PAGE'] - 1, len(responses) - 1)]
        return original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)
    text = quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert 'Автомобиль: не указан или не получен' in text
    assert not any(x in text for x in ('wrong id', 'wrong owner', 'missing owner', 'duplicate', 'incomplete', 'unknown page'))


def test_unavailable_car_endpoint_does_not_break_card(env, monkeypatch):
    session, tools = tools_for(env)
    next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == 1900)['CAR_PROFILE_ID'] = 77
    original = tools._read
    def read(tool, method, payload=None):
        if method == 'car_profile/get_filtered_profiles':
            raise StocrmError('недоступно')
        return original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)
    text = quick_read.status_detail(tools, (2, 51), 1900, 5555)
    assert 'Быстрая сделка №1900' in text and 'Автомобиль: не указан или не получен' in text


def test_older_panel_rejecting_car_fields_still_returns_card(env, monkeypatch):
    _, tools = tools_for(env)
    original = tools._read_uncached
    calls = []
    def read(tool, method, payload=None, **kw):
        if method == 'offers/get_from_filter':
            calls.append(payload['REQUIRED_FIELDS'])
            if 'CAR_PROFILE_ID' in payload['REQUIRED_FIELDS']:
                raise StocrmError('поле недоступно')
        return original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read_uncached', read)
    text = format_offer_card(tools.offer_card(1900))
    assert '1900' in text and 'Автомобиль: не указан или не получен' in text
    assert 'CAR_PROFILE_ID' in calls[0] and 'CAR_PROFILE_ID' in calls[1] and 'CAR_PROFILE_ID' not in calls[2]


@pytest.mark.parametrize('balance,total,expected', [
    ({'RUB_CASH': {'CURRENCY_VALUE': 9500}}, 56950, (9500, 47450, 0)),
    ({'RUB_CASH': {'CURRENCY_VALUE': 0}}, 2000, (0, 2000, 0)),
    ({}, 2000, (None, None, None)),
    ({'OTHER': {}}, 2000, (None, None, None)),
    ({'RUB_CASH': {'CURRENCY_VALUE': 'bad'}}, 2000, (None, None, None)),
    ({'RUB_CASH': {'CURRENCY_VALUE': 1, 'ENTITY_ID': 999}}, 2000, (None, None, None)),
    ({'RUB_CASH': {'CURRENCY_VALUE': 1}}, None, (None, None, None)),
])
def test_wave_80_balance_examples(balance, total, expected):
    assert _offer_balance_values(balance, 1329, total) == expected


def test_balance_diagnostic_reports_only_count_of_object_keys(env, monkeypatch):
    _, tools = tools_for(env)
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'private arbitrary field': 'secret'})
    diagnostic = tools.balance_diagnostic(1329)
    assert diagnostic['field_count'] == 1 and diagnostic['rub_count'] == 0
    assert 'private arbitrary field' not in str(diagnostic) and 'secret' not in str(diagnostic)
