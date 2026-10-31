"""Только локальная MockSession: не подтверждает поведение реальной CRM."""
import asyncio
import datetime as dt

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.booking import parse_request
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession, MockResponse
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    session.data['offer/all_statuses']['41'] = {'STATUS_ID': 41, 'BOARD_ID': 2, 'TITLE': 'Запись', 'ACTIVE': 'Y', 'REASONS': False}
    session.data['calendar/post/get'] = [{'POST_ID': 11, 'CUSTOMER_ID': 1, 'NAME': 'Андрей', 'ACTIVE': 'Y'}]
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text, history):
    return asyncio.run(pipe.handle_text(text, history, 19))


def writes(session):
    return [c['method'] for c in session.calls if c['method'] in ('offer/new/with_contact', 'offer/status/change', 'calendar/card/new')]


def request(phone='89990001122'):
    return f'Запиши клиента {phone}\nНа завтра в 12 к Андрею'


def add_offer(session, ident, contact, status):
    session.data['offers/get_from_filter'].append({'OFFER_ID': ident, 'CONTACT_ID': contact, 'BOARD_ID': 2,
        'BOARD_NAME': 'Автосервис', 'STATUS_ID': 14, 'STATUS_NAME': status, 'CAR_TITLE_TABLE': 'Тест Авто'})


def test_reproduces_screenshot_without_stale_offer(env):
    session, _, pipe = env
    history = []
    pipe._last_offer[19] = 1654
    assert send(pipe, 'Запиши сделку 1654 на сегодня', history).pending_booking['field'] == 'change_status'
    assert 'отменена' in send(pipe, 'Отмена', history).answer
    result = send(pipe, request('89001112233'), history)
    assert result.pending_booking['field'] == 'offer_choice'
    assert '1654' not in result.answer
    args = result.pending_booking['args']
    assert args['phone'] == '89001112233' and args['master'] == 'Андрею'
    assert args['start'] == (dt.date.today()+dt.timedelta(days=1)).isoformat()+'T12:00:00'
    assert not writes(session)


def test_new_client_dialog_and_confirmed_readback(env):
    session, tools, pipe = env
    history = []
    assert send(pipe, request('89001112233'), history).pending_booking['field'] == 'offer_choice'
    assert send(pipe, '1', history).pending_booking['field'] == 'client_name'
    after_name = send(pipe, 'Новый Клиент', history)
    assert after_name.pending_booking['field'] == 'duration_minutes', after_name.answer
    ready = send(pipe, 'час', history)
    assert len(ready.drafts) == 1, ready.answer
    d = ready.drafts[0]
    assert 'Новый Клиент' in d['summary'] and 'Андрей' in d['summary']
    assert 'T12:00:00+03:00' in d['summary'] and 'T13:00:00+03:00' in d['summary']
    assert not writes(session)
    result = tools.apply_draft(d['token'])
    assert result['status'] == 'applied', result
    assert writes(session) == ['offer/new/with_contact', 'offer/status/change', 'calendar/card/new']
    assert result['verified'] and len(session.calendar_cards) == 1
    assert any(c['method'] == 'calendar/post/get_filtered' for c in session.calls)


def test_active_deal_offers_choice_and_preserves_status(env):
    session, tools, pipe = env
    history = []
    first = send(pipe, request(), history)
    assert first.pending_booking['field'] == 'offer_choice'
    options = first.pending_booking['options']
    assert [o['value'] for o in options] == ['new', '1654']
    selected = send(pipe, '2', history)
    assert selected.pending_booking['field'] == 'change_status'
    assert '1654' in selected.answer
    needed = send(pipe, 'нет', history)
    assert needed.pending_booking['field'] == 'duration_minutes', needed.answer
    ready = send(pipe, 'час', history)
    assert len(ready.drafts) == 1, ready.answer
    draft = ready.drafts[0]
    assert draft['check']['original']['contact_id'] == '1031'
    assert draft['check']['verified_phone'] == '89990001122'
    assert not writes(session)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied', result
    assert writes(session) == ['calendar/card/new']
    assert session.calendar_cards[0]['OFFER_ID'] == 1654


def test_multiple_active_options_and_explicit_new(env):
    session, tools, pipe = env
    add_offer(session, 2001, 1031, 'Ожидает запчасти')
    history=[]
    first=send(pipe, request(), history)
    assert [o['value'] for o in first.pending_booking['options']] == ['new', '1654', '2001']
    assert send(pipe, '1', history).pending_booking['field'] == 'duration_minutes'
    ready=send(pipe, 'час', history)
    assert len(ready.drafts) == 1, ready.answer
    d=ready.drafts[0]
    assert d['check']['approved_active_ids'] == [1654, 2001]
    assert not writes(session)
    result=tools.apply_draft(d['token'])
    assert result['status']=='applied', result
    assert result['result']['OFFER_ID'] not in (1654, 2001)


@pytest.mark.parametrize('closed', ['Успешно реализовано', 'ОТКАЗ', 'Мусор', 'Возврат'])
def test_only_specified_closed_statuses_are_excluded(env, closed):
    session, _, pipe = env
    session.data['offers/get_from_filter'][0]['STATUS_NAME'] = closed
    result=send(pipe, request(), [])
    assert result.pending_booking['field'] == 'offer_choice', result.answer
    assert [o['value'] for o in result.pending_booking['options']] == ['new']
    assert not writes(session)


def test_other_final_status_is_still_active(env):
    session, _, pipe = env
    session.data['offers/get_from_filter'][0]['STATUS_NAME'] = 'Успешно'
    assert send(pipe, request(), []).pending_booking['field'] == 'offer_choice'


def test_new_deal_preflight_rechecks_active_set(env):
    session, tools, pipe = env
    history=[]
    send(pipe, request(), history)
    send(pipe, '1', history)
    draft=send(pipe, 'час', history).drafts[0]
    add_offer(session, 2002, 1031, 'В работе')
    with pytest.raises(ValueError, match='изменились'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_existing_contact_change_before_confirmation_blocks(env):
    session, tools, pipe = env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    draft=send(pipe, 'час', history).drafts[0]
    session.data['offers/get_from_filter'][0]['CONTACT_ID']=2087
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_filter_ignored_does_not_offer_other_clients(env):
    session, _, pipe = env
    post=session.post
    def ignored(url, json=None, timeout=None):
        if url.endswith('/offers/get_from_filter') and (json or {}).get('FILTER', {}).get('CONTACT_ID'):
            payload={**json, 'FILTER': {}}
            return post(url, json=payload, timeout=timeout)
        return post(url, json=json, timeout=timeout)
    session.post=ignored
    first=send(pipe, request(), [])
    assert [o['value'] for o in first.pending_booking['options']] == ['new', '1654']
    assert '1712' not in first.answer


def test_unreadable_offers_prevents_guessing(env):
    session, _, pipe=env
    post=session.post
    def broken(url, json=None, timeout=None):
        if url.endswith('/offers/get_from_filter'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'unexpected': 'bad'}}, 200)
        return post(url, json=json, timeout=timeout)
    session.post=broken
    result=send(pipe, request(), [])
    assert not result.drafts and not writes(session)
    assert 'не удалось' in result.answer.lower()


def test_explicit_wrong_offer_with_phone_rejected(env):
    session, _, pipe=env
    r=send(pipe, 'Запиши клиента 89990001122 сделку 1712 на завтра в 12 к Андрею', [])
    assert not r.drafts and not writes(session)
    assert 'не подтверждена' in r.answer


def test_direct_new_offer_cannot_bypass_active_choice(env):
    _, tools, _=env
    with pytest.raises(ValueError, match='активные сделки'):
        tools.draft_book_new_offer(client_name='Иван', phone='89990001122', start='2026-11-20T12:00:00', duration_minutes=60, duration_confirmed=True, post_id=11, customer_id=1)


def test_partial_blocks_new_command_for_same_contact(env):
    session, tools, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    draft=send(pipe, 'час', history).drafts[0]
    tools.drafts.mark(draft['token'], 'partial')
    r=send(pipe, request(), history)
    assert 'заблокирован' in r.answer and not r.drafts
    assert not writes(session)


def test_client_change_during_pending_does_not_reuse_data(env):
    _, _, pipe=env
    history=[]
    send(pipe, request(), history)
    r=send(pipe, 'Запиши клиента 89001112233 на послезавтра в 15 к Андрею', history)
    assert r.pending_booking['field']=='offer_choice'
    args=r.pending_booking['args']
    assert args['phone']=='89001112233' and 'contact_id' not in args and 'offer_choice' not in args
    assert args['start'].endswith('T15:00:00')


def test_parse_plain_number_does_not_leak_into_time():
    result=parse_request(request('89001112233'))
    assert result['phone']=='89001112233'
    assert result['start'].endswith('T12:00:00')


def test_same_phone_multiple_contacts_requires_contact_choice(env):
    session, _, pipe=env
    session.data['contacts/get_from_filter'][1]['MAIN_PHONE'] = '79990001122'
    history=[]
    first=send(pipe, request(), history)
    assert first.pending_booking['field'] == 'contact_id'
    assert [o['value'] for o in first.pending_booking['options']] == [1031, 2087]
    selected=send(pipe, '2', history)
    assert selected.pending_booking['field'] == 'offer_choice'
    assert [o['value'] for o in selected.pending_booking['options']] == ['new', '1712']
    assert not writes(session)


def test_phone_from_additional_contact_phone(env):
    session, _, pipe=env
    session.data['contacts/get_from_filter'][0]['EXTRA_PHONES'] = ['89001112233']
    result=send(pipe, request('79001112233'), [])
    assert result.pending_booking['field'] == 'offer_choice'
    assert [o['value'] for o in result.pending_booking['options']] == ['new', '1654']


def test_active_selection_reasked_when_list_changes(env):
    session, _, pipe=env
    history=[]
    first=send(pipe, request(), history)
    assert first.pending_booking['field'] == 'offer_choice'
    add_offer(session, 2111, 1031, 'Запись')
    result=send(pipe, '1', history)
    assert result.pending_booking['field'] == 'offer_choice'
    assert [o['value'] for o in result.pending_booking['options']] == ['new', '1654', '2111']
    assert not writes(session)


def test_selected_offer_is_still_verified_when_it_disappears(env):
    session, _, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    session.data['offers/get_from_filter'][0]['STATUS_NAME'] = 'Мусор'
    result=send(pipe, 'нет', history)
    assert not result.drafts and not writes(session)
    assert 'активн' in result.answer.lower()


def test_existing_booking_automatic_only_if_disabled_in_settings(env):
    session, _, pipe=env
    pipe.policy.set_confirm_all(False, by=19)
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    result=send(pipe, 'час', history)
    assert not result.drafts and len(result.applied)==1
    assert not result.applied[0].get('warning')
    assert any(c['method'] == 'calendar/post/get_filtered' for c in session.calls)
    assert writes(session)==['calendar/card/new']


def test_legacy_new_booking_draft_blocked_before_write(env):
    session, tools, _=env
    draft=tools.draft_book_new_offer(client_name='Новый', phone='89001112233', start='2026-11-20T12:00:00', duration_minutes=60, duration_confirmed=True, post_id=11, customer_id=1)
    check=dict(draft['check'])
    check.pop('approved_active_ids')
    tools.drafts.mark(draft['token'], 'pending', check=check)
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_explicit_existing_phone_matches_client(env):
    session, _, pipe=env
    r=send(pipe, 'Запиши клиента 89990001122 сделку 1654 на завтра в 12 к Андрею', [])
    assert r.pending_booking['field'] == 'change_status'
    assert '1654' in r.answer and not writes(session)


def test_partial_cannot_be_bypassed_by_explicit_deal_another_date(env):
    session, tools, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    draft=send(pipe, 'час', history).drafts[0]
    tools.drafts.mark(draft['token'], 'partial')
    result=send(pipe, 'Запиши сделку 1654 на послезавтра в 15', history)
    assert 'заблокирован' in result.answer
    assert not writes(session)


def test_repeated_pages_do_not_become_complete_search(env):
    session, tools, _=env
    # Имитация панели, проигнорировавшей постраничность.
    rows=[{'OFFER_ID': 3000+i, 'CONTACT_ID':1031, 'STATUS_NAME':'Запись'} for i in range(500)]
    original=tools._read
    def read(tool, method, payload):
        if method == 'offers/get_from_filter':
            return rows
        return original(tool, method, payload)
    tools._read=read
    with pytest.raises(ValueError, match='повторяет страницы'):
        tools._booking_client_offers(1031)
    assert not writes(session)


def test_missing_contact_id_in_offer_blocks_search(env):
    session, _, pipe=env
    post=session.post
    def broken(url, json=None, timeout=None):
        if url.endswith('/offers/get_from_filter'):
            return MockResponse({'CODE':200, 'RESPONSE':[{'OFFER_ID':111, 'STATUS_NAME':'Мусор'}]},200)
        return post(url, json=json, timeout=timeout)
    session.post=broken
    result=send(pipe, request(), [])
    assert not result.drafts and 'не удалось' in result.answer.lower()
    assert not writes(session)


def test_legacy_existing_booking_draft_blocked_before_write(env):
    session, tools, pipe=env
    history=[]
    send(pipe, 'Запиши сделку 1654 на завтра в 12 к Андрею', history)
    send(pipe, 'нет', history)
    draft=send(pipe, 'час', history).drafts[0]
    check=dict(draft['check'])
    check.pop('binding_check_version')
    tools.drafts.mark(draft['token'], 'pending', check=check)
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_fresh_same_client_request_does_not_reuse_previous_deal_selection(env):
    session, _, pipe=env
    history=[]
    send(pipe, request(), history)
    chosen=send(pipe, '2', history)
    assert chosen.pending_booking['field'] == 'change_status'
    fresh=send(pipe, 'Создай новую сделку для клиента 89990001122 и запиши на завтра в 12 к Андрею', history)
    assert fresh.pending_booking['field'] == 'offer_choice'
    assert not fresh.pending_booking['args'].get('offer_choice')
    assert not writes(session)


def test_new_failed_client_request_invalidates_old_buttons(env):
    session, _, pipe=env
    history=[]
    old=send(pipe, request(), history).pending_booking
    assert old['field']=='offer_choice'
    post=session.post
    def broken(url, json=None, timeout=None):
        if url.endswith('/contacts/get_from_filter'):
            return MockResponse({'CODE': 500, 'MESSAGE':'ошибка', 'RESPONSE':False}, 200)
        return post(url, json=json, timeout=timeout)
    session.post=broken
    send(pipe, request('89001112233'), history)
    assert pipe._bookings.get(19) != old
    assert not writes(session)


@pytest.mark.parametrize('text', [
    'Поставь клиента 89990001122 в календарь на завтра в 12 к Андрею',
    'Оформи запись для клиента 89990001122 на завтра в 12 к Андрею',
    'Запишем клиента 89990001122 на завтра в 12 к Андрею',
])
def test_booking_synonyms_never_reuse_previous_offer(env, text):
    session, _, pipe=env
    pipe._last_offer[19]=1712
    result=send(pipe, text, [])
    assert result.pending_booking['field']=='offer_choice'
    assert [o['value'] for o in result.pending_booking['options']]==['new', '1654']
    assert not writes(session)


def test_contact_ambiguity_appears_while_waiting_for_duration(env):
    session, _, pipe=env
    history=[]
    first=send(pipe, request(), history)
    assert first.pending_booking['field']=='offer_choice'
    assert send(pipe, '2', history).pending_booking['field']=='change_status'
    assert send(pipe, 'нет', history).pending_booking['field']=='duration_minutes'
    session.data['contacts/get_from_filter'][1]['MAIN_PHONE']='79990001122'
    result=send(pipe, 'час', history)
    assert result.pending_booking['field']=='contact_id'
    assert len(result.pending_booking['options'])==2
    assert not writes(session)


def test_duplicate_phone_after_draft_blocks_existing_booking(env):
    session, tools, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    draft=send(pipe, 'час', history).drafts[0]
    session.data['contacts/get_from_filter'][1]['MAIN_PHONE']='79990001122'
    with pytest.raises(ValueError, match='Контакты по телефону изменились'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_duplicate_phone_after_draft_blocks_new_booking(env):
    session, tools, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '1', history)
    draft=send(pipe, 'час', history).drafts[0]
    session.data['contacts/get_from_filter'][1]['MAIN_PHONE']='79990001122'
    with pytest.raises(ValueError, match='Контакты по телефону изменились'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_contact_check_api_failure_can_retry_without_losing_context(env):
    session, _, pipe=env
    original=session.post
    def broken(url, json=None, timeout=None):
        if url.endswith('/contacts/get_from_filter'):
            return MockResponse({'CODE':500, 'MESSAGE':'ошибка', 'RESPONSE':False},200)
        return original(url,json=json,timeout=timeout)
    session.post=broken
    history=[]
    result=send(pipe,request(),history)
    assert result.pending_booking['field']=='retry_booking'
    assert not writes(session)
    session.post=original
    retry=send(pipe,'повторить проверку',history)
    assert retry.pending_booking['field']=='offer_choice'
    assert retry.pending_booking['args']['start'].endswith('T12:00:00')


def test_new_contact_choice_does_not_reuse_old_status_answer(env):
    session, _, pipe=env
    history=[]
    send(pipe, request(), history)
    send(pipe, '2', history)
    send(pipe, 'нет', history)
    session.data['contacts/get_from_filter'][1]['MAIN_PHONE']='79990001122'
    assert send(pipe, 'час', history).pending_booking['field']=='contact_id'
    assert send(pipe, '2', history).pending_booking['field']=='offer_choice'
    result=send(pipe, '2', history)
    assert result.pending_booking['field']=='change_status'
    assert '1712' in result.answer
    assert not writes(session)


def test_disappearing_contact_does_not_supply_old_crm_name(env):
    session, _, pipe=env
    session.data['offers/get_from_filter'][0]['STATUS_NAME']='Мусор'
    history=[]
    first=send(pipe, request(), history)
    assert first.pending_booking['field']=='offer_choice'
    session.data['contacts/get_from_filter'][0]['MAIN_PHONE']='79991112233'
    result=send(pipe, '1', history)
    assert result.pending_booking['field']=='offer_choice'
    result=send(pipe, '1', history)
    assert result.pending_booking['field']=='client_name'
    assert not writes(session)
