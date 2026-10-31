"""Регрессии по снимкам пользователя; только MockSession, без настоящей CRM."""
import asyncio

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import booking_keyboard
from stocrm_agent.bot.fastpath import try_answer
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockResponse, MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    session.data['offer/all_statuses']['14'].update(TITLE='Просчет запчастей', ACTIVE='Y')
    session.data['offer/all_statuses']['41'] = {'STATUS_ID': 41, 'BOARD_ID': 2, 'TITLE': 'Запись', 'ACTIVE': 'Y', 'REASONS': False}
    session.data['calendar/post/get'] = [{'POST_ID': 11, 'CUSTOMER_ID': 1, 'NAME': 'Рыжий', 'ACTIVE': 'Y'}]
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text):
    return asyncio.run(pipe.handle_text(text, [], 19))


def test_status_synonyms_locally_filter_foreign_rows(env, monkeypatch):
    _, tools, _ = env
    rows = [dict(offer_id=1313, board_id=2, status_id=14, status='Просчет запчастей'),
            dict(offer_id=1222, board_id=2, status_id=15, status='В работе'),
            dict(offer_id=3333, board_id=3, status_id=14, status='Просчет запчастей')]
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: rows)
    for text in ('Покажи сделки в просчете', 'Покажи сделки в просчете запчастей'):
        answer, ids = try_answer(text, tools)
        assert ids == [1313]
        assert '№1313' in answer and '№1222' not in answer and '№3333' not in answer
        assert 'фильтр не подтверждён' not in answer.lower()


def test_status_partial_scan_never_claims_no_deals(env, monkeypatch):
    _, tools, _ = env
    foreign = [dict(offer_id=i, board_id=2, status_id=15) for i in range(1, 101)]
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: foreign)
    answer, ids = try_answer('Покажи сделки в просчете', tools)
    assert not ids and 'просмотренных' in answer and 'может быть неполным' in answer


def test_add_note_is_blocked_without_ui_binding_proof(env):
    session, tools, pipe = env
    first = send(pipe, 'Сделка 1313 добавь заметку')
    assert not first.drafts and not first.applied
    assert 'не подтверждает' in first.answer.lower()
    reply = send(pipe, 'Добавь заметку в сделку 1313: 103846')
    assert not reply.drafts and not reply.applied
    assert 'не подтвержден' in reply.answer.lower().replace('ё', 'е')
    assert not any(c['method'] == 'note/create' for c in session.calls)
    with pytest.raises(ValueError, match='привязка'):
        tools.draft_add_offer_note(offer_id=1313, text='103846')


def test_old_note_draft_cannot_bypass_block(env):
    session, tools, _ = env
    draft = tools.drafts.create(tool='add_offer_note', method='note/create', payload={'CODE': 1313, 'ENTITY': 'OFFER_NOTE', 'TEXT': '103846'}, summary='старая заметка', extra={'check': {'kind': 'added_note', 'offer_id': 1313, 'text': '103846'}})
    with pytest.raises(ValueError, match='заблокирован'):
        tools.apply_draft(draft['token'])
    assert not any(c['method'] == 'note/create' for c in session.calls)


def test_incomplete_other_contact_still_allows_confirmed_main_phone(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/contact/get_info') and json.get('CONTACT_ID') == 2087:
            return MockResponse({'CODE': 200, 'RESPONSE': {'STRUCTURIZED': False}}, 200)
        return original(url, json=json, timeout=timeout)
    session.post = post
    result = send(pipe, 'Запиши 89990001122 на завтра в 9 утра к рыжему')
    assert result.pending_booking['field'] == 'contact_id'
    assert 'не удалось' in result.answer and result.pending_booking['options'][0]['value'] == 1031
    selected = send(pipe, '1')
    assert selected.pending_booking['field'] == 'offer_choice'
    assert any(o['value'] == '1654' for o in selected.pending_booking['options'])
    assert not any(c['method'] == 'contact/create' for c in session.calls)


def test_incomplete_phone_search_never_creates_unverified_client(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/contact/get_info'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'STRUCTURIZED': False}}, 200)
        return original(url, json=json, timeout=timeout)
    session.post = post
    result = send(pipe, 'Запиши 89001112233 на завтра в 9 утра к рыжему')
    assert not result.drafts and 'не удалось' in result.answer.lower()
    assert not any(c['method'] in ('contact/create', 'offer/new/with_contact') for c in session.calls)


def test_duration_buttons_and_custom(env):
    _, _, pipe = env
    first = send(pipe, 'Запиши сделку 1654 на завтра в 9 утра к рыжему')
    assert first.pending_booking['field'] == 'change_status'
    pending = send(pipe, 'нет')
    assert pending.pending_booking['field'] == 'duration_minutes'
    markup = booking_keyboard('abc', pending.pending_booking)
    assert [row[0].text for row in markup.inline_keyboard] == ['1 час', 'Своя длительность']
    custom = send(pipe, '2')
    assert custom.pending_booking['field'] == 'duration_minutes' and not custom.pending_booking['options']
    result = send(pipe, '45 минут')
    assert len(result.drafts) == 1 and 'T09:45:00' in result.drafts[0]['summary']


def test_duration_one_hour_and_status_are_distinct(env):
    _, _, pipe = env
    assert send(pipe, 'Запиши сделку 1654 на завтра в 9 утра к рыжему').pending_booking['field'] == 'change_status'
    reply = send(pipe, 'час')
    assert reply.pending_booking['field'] == 'change_status'
    assert reply.pending_booking['args']['duration_minutes'] == 60
    pending = send(pipe, 'нет')
    assert pending.pending_booking['field'] == 'duration_minutes'
    result = send(pipe, '1')
    assert len(result.drafts) == 1 and 'T10:00:00' in result.drafts[0]['summary']


def test_no_active_deals_requires_choice(env):
    session, _, pipe = env
    session.data['offers/get_from_filter'][0]['STATUS_NAME'] = 'Отказ'
    result = send(pipe, 'Запиши 89990001122 на завтра в 9 утра к рыжему')
    assert result.pending_booking['field'] == 'offer_choice'
    assert [o['value'] for o in result.pending_booking['options']] == ['new']
    assert not result.drafts


def test_short_card_shows_calculated_debt_and_unknown_is_not_zero(env):
    session, tools, _ = env
    text, _ = try_answer('покажи сделку 1654', tools)
    assert 'ДОЛГ: 13 400 ₽' in text
    session.data['payment/get_balance'] = {'RUB_CASH': {'CURRENCY_VALUE': 'неизвестно'}}
    tools._invalidate_cache()
    text, _ = try_answer('покажи сделку 1654', tools)
    assert 'не удалось проверить баланс' in text and 'Долг: 0' not in text


def test_balance_for_other_entity_is_not_accepted(env):
    session, tools, _ = env
    session.data['payment/get_balance'] = {'RUB_CASH': {'CURRENCY_VALUE': 0, 'ENTITY_ID': 999}}
    text, _ = try_answer('покажи сделку 1654', tools)
    assert 'не удалось проверить баланс' in text


def test_explicit_duration_requires_confirmation(env):
    _, _, pipe = env
    assert send(pipe, 'Запиши сделку 1654 на завтра в 9 утра к рыжему на 45 минут').pending_booking['field'] == 'change_status'
    result = send(pipe, 'нет')
    assert result.pending_booking['field'] == 'duration_minutes'
    assert [x['label'] for x in result.pending_booking['options']] == ['Оставить 45 мин', '1 час', 'Своя длительность']
    ready = send(pipe, '1')
    assert len(ready.drafts) == 1 and 'T09:45:00' in ready.drafts[0]['summary']


def test_telegram_booking_duration_button_creates_draft_not_write(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path / 'access.json'))
    bot, dispatcher, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
        Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.calls = []
        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if method.__api_method__ == 'sendMessage':
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self):
            pass
        async def stream_content(self, *args, **kwargs):
            if False:
                yield b''

    fake = FakeSession()
    bot.session = fake
    user = {'id': 19, 'is_bot': False, 'first_name': 'Тест'}
    chat = {'id': 19, 'type': 'private'}
    def update(value):
        return Update.model_validate(value, context={'bot': bot})
    def sent():
        return [x for x in fake.calls if x.__api_method__ == 'sendMessage']
    async def run():
        await dispatcher.feed_update(bot, update({'update_id': 1, 'message': {'message_id': 1, 'date': 1, 'chat': chat, 'from': user, 'text': 'Запиши сделку 1654 на завтра в 9 утра к рыжему'}}))
        choices = [m for m in sent() if m.reply_markup and any('Нет, оставить' in b.text for row in m.reply_markup.inline_keyboard for b in row)]
        assert len(choices) == 1
        no_status = next(b.callback_data for row in choices[-1].reply_markup.inline_keyboard for b in row if 'Нет, оставить' in b.text)
        await dispatcher.feed_update(bot, update({'update_id': 2, 'callback_query': {'id': 'cb1', 'from': user, 'chat_instance': 'x', 'data': no_status, 'message': {'message_id': 101, 'date': 1, 'chat': chat, 'text': choices[-1].text}}}))
        durations = [m for m in sent() if m.reply_markup and any(b.text == '1 час' for row in m.reply_markup.inline_keyboard for b in row)]
        assert len(durations) == 1
        hour = next(b.callback_data for row in durations[-1].reply_markup.inline_keyboard for b in row if b.text == '1 час')
        await dispatcher.feed_update(bot, update({'update_id': 3, 'callback_query': {'id': 'cb2', 'from': user, 'chat_instance': 'x', 'data': hour, 'message': {'message_id': 102, 'date': 1, 'chat': chat, 'text': durations[-1].text}}}))
        assert any(m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row) for m in sent())
        assert not any(c['method'] in ('calendar/card/new', 'offer/status/change') for c in session.calls)
        await bot.session.close()
    asyncio.run(run())


def test_contact_search_caps_detail_requests_and_does_not_claim_complete(env):
    session, tools, _ = env
    session.data['contacts/get_from_filter'] = [{'CONTACT_ID': i, 'TITLE': f'Клиент {i}', 'MAIN_PHONE': '89991234567' if i == 21 else '89990000000'} for i in range(1, 22)]
    found = tools._booking_contacts('89991234567')
    assert len(found) == 1 and found[0]['contact_id'] == 21 and found[0]['search_incomplete']
    assert sum(c['method'] == 'contact/get_info' for c in session.calls) == 20
    with pytest.raises(ValueError, match='Не удалось проверить'):
        tools._booking_contacts('89997654321')


def test_direct_booking_cannot_bypass_incomplete_contact_choice(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_booking_contacts', lambda phone: [{'contact_id': 1031, 'title': 'Иван', 'search_incomplete': True}])
    with pytest.raises(ValueError, match='Сначала явно выберите'):
        tools.draft_book_new_offer(client_name='Иван', phone='89990001122', start='2026-11-20T12:00:00',
                                   duration_minutes=60, duration_confirmed=True, post_id=11, customer_id=1)
