"""Создание контакта: имитация API, никаких запросов в рабочую CRM."""
import asyncio
import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession, MockResponse
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text, user=19):
    return asyncio.run(pipe.handle_text(text, [], user))


def calls(session, method):
    return [c for c in session.calls if c['method'] == method]


def test_phone_is_not_sku_and_creation_is_confirmed_and_verified(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: pytest.fail('номер телефона ушёл в поиск товара'))
    first = send(pipe, 'Добавь клиента 89001112233')
    assert 'зовут' in first.answer and not first.choices and not first.drafts
    assert not calls(session, 'contact/create')
    second = send(pipe, 'Иван Петров')
    assert len(second.drafts) == 1
    draft = second.drafts[0]
    assert draft['payload'] == {'DATA': {'FIRSTNAME': 'Иван', 'SURNAME': 'Петров'},
                                 'PROPERTIES': {'1': [{'VALUE': '89001112233', 'MAIN': 'Y'}]}}
    assert not calls(session, 'contact/create')
    result = tools.apply_draft(draft['token'])
    assert result['verified'] and result['result']['CONTACT_ID']
    assert len(calls(session, 'contact/create')) == 1
    assert calls(session, 'contact/get_info')
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(calls(session, 'contact/create')) == 1


def test_single_name_and_both_in_one_message(env):
    _, _, pipe = env
    result = send(pipe, 'Создай контакт Иван 89001112233')
    assert result.drafts[0]['payload']['DATA'] == {'FIRSTNAME': 'Иван', 'SURNAME': ''}


def test_existing_phone_is_reported_and_not_created(env):
    session, _, pipe = env
    assert 'зовут' in send(pipe, 'Добавь клиента 89990001122').answer
    result = send(pipe, 'Иван Петров')
    assert 'найден' in result.answer.lower() and not result.drafts and not result.choices
    assert not calls(session, 'contact/create')


def test_cannot_create_when_other_details_unchecked(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/contact/get_info'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'STRUCTURIZED': False}}, 200)
        return original(url, json=json, timeout=timeout)
    session.post = post
    result = send(pipe, 'Добавь клиента Иван 89001112233')
    assert 'не удалось' in result.answer.lower() and not result.drafts
    assert not calls(session, 'contact/create')


def test_new_duplicate_after_draft_stops_before_write(env):
    session, tools, pipe = env
    draft = send(pipe, 'Добавь клиента Иван 89001112233').drafts[0]
    session.data['contacts/get_from_filter'].append({'CONTACT_ID': 999, 'TITLE': 'Другой', 'MAIN_PHONE': '79001112233'})
    with pytest.raises(ValueError, match='появился'):
        tools.apply_draft(draft['token'])
    assert not calls(session, 'contact/create')


def test_timeout_partial_and_no_duplicate_retry(env):
    session, tools, pipe = env
    draft = send(pipe, 'Добавь клиента Иван 89001112233').drafts[0]
    original = tools.client.call
    def call(method, payload=None, **kw):
        if method == 'contact/create':
            raise StocrmError('таймаут, нет ответа', method=method)
        return original(method, payload, **kw)
    tools.client.call = call
    with pytest.raises(StocrmError, match='неизвестен'):
        tools.apply_draft(draft['token'])
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    tools.drafts.archive(draft['token'])
    result = send(pipe, 'Добавь клиента Иван 89001112233')
    assert 'повтор' in result.answer.lower() and not result.drafts


def test_unknown_id_or_readback_is_not_success(env, monkeypatch):
    _, tools, pipe = env
    draft = send(pipe, 'Добавь клиента Иван 89001112233').drafts[0]
    old = tools.client.call
    monkeypatch.setattr(tools.client, 'call', lambda method, payload=None, **kw: True if method == 'contact/create' else old(method, payload, **kw))
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and not result['verified']
    assert tools.drafts.get(draft['token'])['status'] == 'partial'


def test_cancel_reset_and_isolation(env):
    _, _, pipe = env
    assert 'зовут' in send(pipe, 'Добавь клиента 89001112233').answer
    assert 'отменено' in send(pipe, 'отмена').answer.lower()
    assert 19 not in pipe._contact_create_pending
    send(pipe, 'Добавь клиента 89001112233')
    pipe.reset_pending(19)
    assert 19 not in pipe._contact_create_pending
    send(pipe, 'Добавь клиента 89001112233', user=19)
    send(pipe, 'Добавь клиента 89002223344', user=20)
    assert send(pipe, 'Иван', user=19).drafts[0]['check']['phone'] == '89001112233'
    assert send(pipe, 'Мария', user=20).drafts[0]['check']['phone'] == '89002223344'


def test_search_includes_additional_phones(env):
    session, _, pipe = env
    session.data['contacts/get_from_filter'][0]['EXTRA_PHONES'] = ['79001112233']
    result = send(pipe, 'Добавь клиента Иван 89001112233')
    assert 'найден' in result.answer.lower() and not result.drafts
    assert not calls(session, 'contact/create')


def test_concurrent_other_draft_blocked_by_claim(env):
    _, tools, pipe = env
    first = send(pipe, 'Добавь клиента Иван 89001112233').drafts[0]
    second = tools.drafts.create(tool='create_contact', method='contact/create', payload=first['payload'], summary='duplicate', extra={'check': first['check']})
    tools.drafts.claim(first['token'])
    with pytest.raises(ValueError, match='уже выполнялось'):
        tools.drafts.claim(second['token'])


def test_explicit_client_intent_does_not_steal_product_and_mixed_needs_clarification(env, monkeypatch):
    _, tools, pipe = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    result = send(pipe, 'Добавь в сделку 1654 товар CH88011 за 1200')
    assert result.choices and result.choices[0]['args']['sku'] == 'CH88011'
    mixed = send(pipe, 'Добавь клиента 89001112233 и запиши в сделку 1654')
    assert not mixed.drafts and 'уточните' in mixed.answer.lower()


def test_auto_mode_failed_create_does_not_offer_apply_again(env, monkeypatch):
    _, tools, pipe = env
    pipe.policy.set_confirm_all(False)
    old = tools.client.call
    def call(method, payload=None, **kw):
        if method == 'contact/create':
            raise StocrmError('таймаут', method=method)
        return old(method, payload, **kw)
    monkeypatch.setattr(tools.client, 'call', call)
    result = send(pipe, 'Добавь клиента Иван 89001112233')
    assert result.drafts and result.drafts[0]['status'] == 'partial'
    assert 'неизвестен' in result.answer.lower() and 'повтор' in result.answer.lower()


def test_telegram_message_then_name_has_confirm_button_only(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path / 'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
                                   Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.methods = []
        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
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
    async def run():
        for uid, text in [(1, 'Добавь клиента 89001112233'), (2, 'Иван Петров')]:
            update = Update.model_validate({'update_id': uid, 'message': {'message_id': uid, 'date': 1, 'chat': chat, 'from': user, 'text': text}}, context={'bot': bot})
            await dp.feed_update(bot, update)
        msgs = [m for m in fake.methods if m.__api_method__ == 'sendMessage']
        assert any('зовут' in m.text for m in msgs)
        assert any(m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row) for m in msgs)
        assert not calls(session, 'contact/create')
        await bot.session.close()
    asyncio.run(run())


def test_unverified_legacy_contact_attempt_blocks_new_draft(env):
    _, tools, pipe = env
    tools.drafts.create(tool='create_contact', method='contact/create', payload={'DATA': {'FIRSTNAME': 'Иван'}, 'PROPERTIES': {'1': [{'VALUE': '89001112233'}]}}, summary='old')
    old = tools.drafts.list('all')[0]
    tools.drafts.mark(old['token'], 'partial', done_steps={'error': 'uncertain'})
    result = send(pipe, 'Добавь клиента Иван 89001112233')
    assert 'повтор' in result.answer.lower() and not result.drafts


def test_wrong_readback_name_not_reported_as_success(env, monkeypatch):
    _, tools, pipe = env
    draft = send(pipe, 'Добавь клиента Иван 89001112233').drafts[0]
    original = tools._booking_contacts
    def search(phone, contact_id=None):
        if contact_id:
            return [{'contact_id': contact_id, 'title': 'Другое имя', 'phone': phone}]
        return original(phone, contact_id)
    monkeypatch.setattr(tools, '_booking_contacts', search)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and not result['verified']
