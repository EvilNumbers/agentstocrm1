"""Без живых сервисов: форматы по журналу и опасные пограничные случаи."""
import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.bot.fastpath import format_offer_card, format_offer_items
from stocrm_agent.calendar import _rows
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.mock import MockResponse
from test_client_booking import env as booking_env, request, send, writes  # noqa: F401
from test_quick_read import env as quick_env  # noqa: F401
from test_details_efficiency_v95 import env as details_env  # noqa: F401


def offer_row(session, oid=1654):
    return next(r for r in session.data['offers/get_from_filter'] if r['OFFER_ID'] == oid)


def test_logged_offer_car_fields_used_without_guessing(quick_env):
    session, pipe = quick_env
    row = offer_row(session, 1900)
    row.update(CAR_TITLE='Toyota Corolla', CAR_PROFILE_VIN='TEST123', CAR_NUMBER_FLAT='А123АА', CAR_PROFILE_ID=42)
    card = pipe.tools.offer_card(1900, with_items=True)
    assert 'Автомобиль: Toyota Corolla · А123АА' in format_offer_card(card, detailed=True)
    assert 'VIN: TEST123' in quick_read.status_detail(pipe.tools, (2, 51), 1900, 5555)
    call = next(c for c in session.calls if c['method'] == 'offers/get_from_filter')
    assert {'CAR_TITLE', 'CAR_PROFILE_VIN', 'CUSTOMER_ID'} <= set(call['payload']['REQUIRED_FIELDS'])
    assert not any(c['method'] == 'car_profile/get_filtered_profiles' for c in session.calls)


def test_conflicting_car_titles_and_vin_not_mixed(quick_env):
    session, pipe = quick_env
    row = offer_row(session, 1900)
    row.update(CAR_TITLE='Toyota', CAR_TITLE_TABLE='Mazda', CAR_PROFILE_VIN='VIN1', CAR_VIN='VIN2', CAR_PROFILE_ID=77, CAR_NUMBER_FLAT='А123')
    text = quick_read.status_detail(pipe.tools, (2, 51), 1900, 5555)
    assert 'Автомобиль: данные CRM противоречивы' in text
    assert all(x not in text for x in ('Toyota', 'Mazda', 'VIN1', 'VIN2', 'А123'))
    assert not any(c['method'] == 'car_profile/get_filtered_profiles' for c in session.calls)


def test_conflicting_vin_is_not_silently_chosen(quick_env):
    session, pipe = quick_env
    offer_row(session, 1900).update(CAR_TITLE='Mazda', CAR_PROFILE_VIN='VIN1', CAR_VIN='VIN2')
    text = quick_read.status_detail(pipe.tools, (2, 51), 1900, 5555)
    assert 'Автомобиль: Mazda' in text and 'VIN: данные CRM противоречивы' in text
    assert 'VIN1' not in text and 'VIN2' not in text


def test_fallback_to_documented_car_fields_on_rejection(quick_env, monkeypatch):
    session, pipe = quick_env
    offer_row(session, 1900).update(CAR_TITLE_TABLE='Mazda', CAR_NUMBER_FLAT='А123')
    tools = pipe.tools
    original = tools._read_uncached
    calls = []
    def read(tool, method, payload=None, **kw):
        if method == 'offers/get_from_filter':
            calls.append(list(payload['REQUIRED_FIELDS']))
            if 'CAR_TITLE' in payload['REQUIRED_FIELDS']:
                raise StocrmError('неизвестное поле', code=400)
        return original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read_uncached', read)
    assert 'Автомобиль: Mazda · А123' in format_offer_card(tools.offer_card(1900))
    assert 'CAR_TITLE' in calls[0] and 'CAR_TITLE' not in calls[1] and 'CAR_TITLE_TABLE' in calls[1]


@pytest.mark.parametrize('code,message', [(401, 'не найдено'), (403, 'нет данных'), (429, 'нет данных'), (500, 'не найдено')])
def test_access_or_server_error_not_empty(code, message):
    with pytest.raises(StocrmError):
        StocrmClient._unwrap({'CODE': code, 'MESSAGE': message, 'RESPONSE': False}, 'work/get_filtered')


@pytest.mark.parametrize('method,code,message', [
    ('work/get_filtered', 404, 'Не найдено'),
    ('products/get_filtered_orders', 4047632405, 'Не найдено'),
    ('wms/get_filtered_stocks', 40473495214, 'По данному запросу остатки не найдены'),
    ('contacts/get_from_filter', 404962143198, 'Контакты по запрашиваемым критериям не найдены'),
])
def test_actual_log_empty_response_kept(method, code, message):
    assert StocrmClient._unwrap({'CODE': code, 'MESSAGE': message, 'RESPONSE': False}, method) == []
    with pytest.raises(StocrmError):
        StocrmClient._unwrap({'CODE': code, 'MESSAGE': message, 'RESPONSE': False}, method, tolerate_empty=False)


@pytest.mark.parametrize('bad', [None, False, {}, {'UNEXPECTED': []}, {'DATA': None}, {'DATA': 'bad'}])
def test_calendar_unknown_never_free(booking_env, monkeypatch, bad):
    _, tools, _ = booking_env
    original = tools._read
    def read(tool, method, payload=None, **kw):
        return bad if method == 'calendar/card/get' else original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read', read)
    with pytest.raises(ValueError):
        tools.draft_create_calendar_entry(customer_id=1, post_id=11, offer_id=1654, start='2026-11-03T12:00', end='2026-11-03T13:00')


@pytest.mark.parametrize('raw', [[], {'DATA': []}, {'ITEMS': []}])
def test_explicit_empty_calendar_is_valid(raw):
    assert _rows(raw) == []


def test_wrong_or_missing_customer_cannot_prepare_calendar(booking_env):
    session, tools, _ = booking_env
    row = offer_row(session)
    for customer in (2, None, False):
        row['CUSTOMER_ID'] = customer
        with pytest.raises(ValueError, match='одразделени'):
            tools.draft_create_calendar_entry(customer_id=1, post_id=11, offer_id=1654, start='2026-11-03T12:00', end='2026-11-03T13:00')
    assert not any(c['method'] in ('calendar/card/new', 'calendar/card/edit') for c in session.calls)


def test_customer_changed_after_draft_blocks_calendar_before_write(booking_env):
    session, tools, _ = booking_env
    draft = tools.draft_create_calendar_entry(customer_id=1, post_id=11, offer_id=1654, start='2026-11-03T12:00', end='2026-11-03T13:00')
    offer_row(session)['CUSTOMER_ID'] = 2
    with pytest.raises(ValueError, match='другому подразделению'):
        tools.apply_draft(draft['token'])
    assert not any(c['method'] == 'calendar/card/new' for c in session.calls)


def test_existing_booking_rechecks_customer(booking_env):
    session, tools, _ = booking_env
    offer_row(session)['CUSTOMER_ID'] = 2
    with pytest.raises(ValueError, match='другому подразделению'):
        tools.draft_book_existing_offer(offer_id=1654, start='2026-11-03T12:00', duration_minutes=60,
                                        duration_confirmed=True, customer_id=1, post_id=11, change_status=False)
    assert not any(c['method'] == 'calendar/card/new' for c in session.calls)


def test_display_fractional_and_unknown_and_zero_prices():
    card = {'offer_id': 1, 'sum': 0, 'sum_known': False, 'paid': 1.49, 'debt': 2.49, 'payments': {},
            'parts': [{'name': 'A', 'qty': 1, 'price': 0, 'price_known': True},
                      {'name': 'B', 'qty': 2, 'price': 1.49, 'price_known': True},
                      {'name': 'C', 'qty': 1, 'price': 0, 'price_known': False}], 'works': []}
    text = format_offer_card(card, detailed=True)
    assert 'Сумма: не получена' in text and 'Оплачено: 1,49 ₽' in text and 'Долг (осталось оплатить): 2,49 ₽' in text
    assert 'A — 1 шт. × 0 ₽ = 0 ₽' in text
    assert 'B — 2 шт. × 1,49 ₽ = 2,98 ₽' in text
    assert 'C — 1 шт. · цена не получена' in text


def part(n):
    return {'OFFER_ID': 1654, 'ORDER_ID': n, 'NAME': f'P{n}', 'QUANTITY': 1, 'PRICE': 1}


def work(n):
    return {'OFFER_ID': 1654, 'WORK_ID': n, 'TITLE': f'W{n}', 'SUM': '1.00'}


@pytest.mark.parametrize('kind', ['parts', 'works'])
def test_capped_short_pages_are_read_to_confirmed_end(details_env, monkeypatch, kind):
    _, tools, _ = details_env
    method = 'products/get_filtered_orders' if kind == 'parts' else 'work/get_filtered'
    make = part if kind == 'parts' else work
    pages = []
    original = tools._read
    def read(tool, endpoint, payload=None, **kw):
        if endpoint == method:
            pages.append(payload['PAGE'])
            n = payload['PAGE']
            return {'DATA': [make(n)] if n <= 2 else [], 'TOTAL_COUNT': 1 if n <= 2 else 0}
        return original(tool, endpoint, payload, **kw)
    monkeypatch.setattr(tools, '_read', read)
    if kind == 'parts':
        rows, error = tools._offer_parts_details(1654)
        assert error is None
    else:
        rows = tools.offer_works(1654)
    assert len(rows) == 2 and pages == [1, 2, 3]


@pytest.mark.parametrize('kind', ['parts', 'works'])
def test_missing_next_page_does_not_become_complete(details_env, monkeypatch, kind):
    _, tools, _ = details_env
    method = 'products/get_filtered_orders' if kind == 'parts' else 'work/get_filtered'
    make = part if kind == 'parts' else work
    def read(tool, endpoint, payload=None, **kw):
        if payload['PAGE'] == 1:
            return {'DATA': [make(1)], 'TOTAL_COUNT': 2}
        raise StocrmError('сбой второй страницы')
    monkeypatch.setattr(tools, '_read', read)
    if kind == 'parts':
        rows, error = tools._offer_parts_details(1654)
        assert len(rows) == 1 and 'не удалось полностью' in error.lower()
        with pytest.raises(StocrmError):
            tools.offer_parts(1654)
    else:
        with pytest.raises(StocrmError):
            tools.offer_works(1654)


@pytest.mark.parametrize('kind', ['parts', 'works'])
def test_repeated_page_does_not_prove_completeness(details_env, monkeypatch, kind):
    _, tools, _ = details_env
    make = part if kind == 'parts' else work
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'DATA': [make(1)]})
    if kind == 'parts':
        rows, error = tools._offer_parts_details(1654)
        assert len(rows) == 1 and 'повтор' in error.lower()
    else:
        with pytest.raises(StocrmError, match='повтор'):
            tools.offer_works(1654)


def test_search_offer_after_short_page_and_incomplete_search(details_env, monkeypatch):
    _, tools, _ = details_env
    original = tools._read
    def read(tool, method, payload=None, **kw):
        if method == 'offers/get_from_filter':
            if 'FILTER' in payload:
                return {'DATA': [{'OFFER_ID': 1}]}
            return {'DATA': [{'OFFER_ID': 1 if payload['PAGE'] == 1 else 1654}]}
        return original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read', read)
    assert tools._search_offer_row(1654)[0]['OFFER_ID'] == 1654
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'DATA': [{'OFFER_ID': 1}]})
    with pytest.raises(ValueError, match='неполон'):
        tools._search_offer_row(1654)


def test_short_booking_pages_are_not_end(booking_env, monkeypatch):
    _, tools, _ = booking_env
    original = tools._read
    def read(tool, method, payload=None, **kw):
        if tool == 'booking_existing_offers':
            n = payload['PAGE']
            return {'DATA': [{'OFFER_ID': 800+n, 'CONTACT_ID': 1031, 'STATUS_NAME': 'В работе'}] if n <= 2 else []}
        return original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read', read)
    assert [x['offer_id'] for x in tools._booking_client_offers(1031)] == [801, 802]
    assert tools._booking_offer_ids(1031) == {801, 802}


def test_short_contact_pages_check_all_before_new_client(booking_env, monkeypatch):
    _, tools, _ = booking_env
    original = tools._read
    def read(tool, method, payload=None, **kw):
        if tool == 'booking_contacts':
            page = payload['PAGE']
            if page <= 2:
                return {'DATA': [{'CONTACT_ID': page, 'MAIN_PHONE': '79000000000' if page == 1 else '79000000001', 'TITLE': 'Клиент'}]}
            return {'DATA': []}
        if tool == 'booking_contact_phones':
            raise StocrmError('дополнительный телефон не получен')
        return original(tool, method, payload, **kw)
    monkeypatch.setattr(tools, '_read', read)
    rows = tools._booking_contacts('79000000001')
    assert [row['contact_id'] for row in rows] == [2] and rows[0]['search_incomplete']


def test_contacts_special_message_does_not_hide_server_error():
    with pytest.raises(StocrmError):
        StocrmClient._unwrap({'CODE': 500, 'MESSAGE': 'Контакты по запрашиваемым критериям не найдены', 'RESPONSE': False}, 'contacts/get_from_filter')


def test_existing_booking_customer_changed_after_draft_blocks_write(booking_env):
    session, tools, pipe = booking_env
    history = []
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    draft = send(pipe, 'час', history).drafts[0]
    offer_row(session)['CUSTOMER_ID'] = 2
    with pytest.raises(ValueError, match='другому подразделению'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_new_booking_wrong_customer_after_creation_is_partial_and_stops(booking_env):
    session, tools, pipe = booking_env
    history = []
    send(pipe, request(), history)
    send(pipe, '1', history)
    draft = send(pipe, 'час', history).drafts[0]
    original = session.post
    def post(url, json=None, timeout=None):
        result = original(url, json=json, timeout=timeout)
        if url.endswith('/offer/new/with_contact'):
            session.data['offers/get_from_filter'][-1]['CUSTOMER_ID'] = 2
        return result
    session.post = post
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and 'подразделению' in result['warning']
    assert writes(session) == ['offer/new/with_contact']
    assert tools.drafts.get(draft['token'])['status'] == 'partial'


def test_calendar_customers_short_pages_and_repeat(booking_env, monkeypatch):
    _, tools, _ = booking_env
    def read(tool, method, payload=None, **kw):
        p = payload['PAGE']
        return {'DATA': [{'CUSTOMER_ID': p, 'CUSTOMER_NAME': f'Подразделение {p}', 'TIME_ZONE': 'Europe/Moscow', 'ACTIVE': 'Y'}] if p <= 2 else []}
    monkeypatch.setattr(tools, '_read', read)
    assert [x['customer_id'] for x in tools._calendar_customers()] == [1, 2]
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'DATA': [{'CUSTOMER_ID': 1}]})
    with pytest.raises(ValueError, match='повторяет'):
        tools._calendar_customers()


def test_duplicate_offer_id_not_used_as_proof_of_customer(booking_env, monkeypatch):
    _, tools, _ = booking_env
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'DATA': [
        {'OFFER_ID': 1654, 'CUSTOMER_ID': 1}, {'OFFER_ID': 1654, 'CUSTOMER_ID': 2}]})
    with pytest.raises(ValueError, match='несколько сделок'):
        tools._calendar_check_offer(1654, 1)


def test_server_error_does_not_trigger_car_field_retry(quick_env, monkeypatch):
    _, pipe = quick_env
    calls = []
    def read(tool, method, payload=None, **kw):
        calls.append(1)
        raise StocrmError('сбой сервера', code=500)
    monkeypatch.setattr(pipe.tools, '_read_uncached', read)
    with pytest.raises(StocrmError):
        pipe.tools._search_offer_row(1900)
    assert len(calls) == 1
