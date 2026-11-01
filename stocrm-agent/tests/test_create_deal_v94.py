"""Сделка по клиенту: только локальная имитация, без живой CRM."""
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


def send(pipe, msg, user=19):
    return asyncio.run(pipe.handle_text(msg, [], user))


def writes(session):
    return [c for c in session.calls if c['method'] in ('offer/new', 'offer/new/with_contact', 'contact/create')]


def test_existing_client_and_verified_apply(env):
    session, tools, pipe = env
    first = send(pipe, 'Создай новую сделку клиенту 89990001122')
    assert 'воронке' in first.answer and not first.drafts and not writes(session)
    second = send(pipe, '1')
    assert len(second.drafts) == 1
    draft = second.drafts[0]
    assert draft['payload']['CONTACT_ID'] == session.data['contacts/get_from_filter'][0]['CONTACT_ID']
    assert draft['payload']['PHONE'] == '89990001122' and 'TITLE' not in draft['payload']
    assert 'существующего клиента' in draft['summary'] and 'Новая сделка' not in draft['summary']
    assert not writes(session)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['verified'] and outcome['result']['CONTACT_ID'] == draft['payload']['CONTACT_ID']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 1


def test_new_client_phone_in_separate_message_and_name(env):
    session, tools, pipe = env
    assert 'телефон' in send(pipe, 'Создай новую сделку').answer.lower()
    assert 'как зовут' in send(pipe, '89001112233').answer.lower()
    assert 'воронке' in send(pipe, 'Мария Иванова').answer
    draft = send(pipe, '1').drafts[0]
    assert draft['payload'] == {'BOARD_ID': 2, 'PHONE': '89001112233', 'TITLE': 'Мария Иванова'}
    assert 'Создать клиента Мария Иванова' in draft['summary']
    assert not writes(session)
    result = tools.apply_draft(draft['token'])
    assert result['verified'] and len(writes(session)) == 1
    assert result['result']['CONTACT_ID']


def test_multiple_matches_require_explicit_choice(env):
    session, _, pipe = env
    session.data['contacts/get_from_filter'].append({'CONTACT_ID': 999, 'TITLE': 'Тест', 'MAIN_PHONE': '79990001122'})
    answer = send(pipe, 'Создай новую сделку клиенту 89990001122')
    assert 'Выберите клиента' in answer.answer and not answer.drafts
    assert 'воронке' in send(pipe, '2').answer
    draft = send(pipe, '1').drafts[0]
    assert draft['payload']['CONTACT_ID'] == 999 and not writes(session)


def test_changed_contacts_stop_before_write(env):
    session, tools, pipe = env
    send(pipe, 'Создай сделку клиенту 89001112233')
    send(pipe, 'Ирина')
    draft = send(pipe, '1').drafts[0]
    session.data['contacts/get_from_filter'].append({'CONTACT_ID': 999, 'TITLE': 'Уже существует', 'MAIN_PHONE': '79001112233'})
    with pytest.raises(ValueError, match='изменились'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_failed_contact_search_not_treated_as_new(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/contact/get_info'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'STRUCTURIZED': False}})
        return original(url, json=json, timeout=timeout)
    session.post = post
    result = send(pipe, 'Создай сделку клиенту 89001112233')
    assert 'не удалось' in result.answer.lower() and not result.drafts and not writes(session)


def test_timeout_stays_partial_blocks_new_attempt(env, monkeypatch):
    session, tools, pipe = env
    send(pipe, 'Создай сделку клиенту 89990001122')
    draft = send(pipe, '1').drafts[0]
    old = tools.client.call
    def fail(method, payload=None, **kwargs):
        if method == 'offer/new/with_contact':
            raise StocrmError('таймаут', method=method)
        return old(method, payload, **kwargs)
    monkeypatch.setattr(tools.client, 'call', fail)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    send(pipe, 'Создай сделку клиенту 89990001122')
    response = send(pipe, '1')
    assert not response.drafts and 'не подтверждена' in response.answer
    assert not writes(session)


def test_legacy_draft_and_direct_tool_do_not_bypass_search(env):
    session, tools, _ = env
    draft = tools.drafts.create(tool='create_offer', method='offer/new/with_contact', payload={'PHONE': '89001112233', 'TITLE': 'Новая сделка'}, summary='old')
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(draft['token'])
    with pytest.raises(ValueError, match='настоящее имя'):
        tools.draft_create_offer(board_id=2, phone='89001112233', title='Новая сделка')
    assert not writes(session)


def test_new_client_pending_cancel_and_user_isolation(env):
    _, _, pipe = env
    send(pipe, 'Создай сделку клиенту 89001112233', user=19)
    send(pipe, 'Создай сделку клиенту 89002223344', user=20)
    assert 'отменено' in send(pipe, 'отмена', user=19).answer.lower()
    assert 'воронке' in send(pipe, 'Мария', user=20).answer
    pipe.reset_pending(20)
    assert 20 not in pipe._offer_create_pending


def test_api_requires_phone_even_with_contact_id(env):
    session, tools, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/offer/new/with_contact'):
            assert json.get('PHONE') == '89990001122'
            assert json.get('CONTACT_ID')
        return original(url, json=json, timeout=timeout)
    session.post = post
    send(pipe, 'Создай новую сделку клиенту 89990001122')
    draft = send(pipe, '1').drafts[0]
    assert tools.apply_draft(draft['token'])['verified']


def test_new_customer_blocked_for_large_incomplete_search(env):
    session, _, pipe = env
    session.data['contacts/get_from_filter'].extend(
        {'CONTACT_ID': 1000+i, 'TITLE': 'Тест', 'MAIN_PHONE': '79991112233'} for i in range(25))
    answer = send(pipe, 'Создай сделку клиенту 89001112233')
    assert 'не удалось проверить' in answer.answer.lower()
    assert not answer.drafts and not writes(session)


def test_contact_name_changed_after_preparation(env):
    session, tools, pipe = env
    send(pipe, 'Создай сделку клиенту 89990001122')
    draft = send(pipe, '1').drafts[0]
    session.data['contacts/get_from_filter'][0]['TITLE'] = 'Другое имя'
    with pytest.raises(ValueError, match='Имя клиента изменилось'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_explicit_extra_conditions_not_silently_discarded(env):
    session, _, pipe = env
    result = send(pipe, 'Создай новую сделку клиенту 89990001122 в воронке Магазин')
    assert 'дополнительные условия' in result.answer and not result.drafts
    assert not writes(session)


def test_telegram_flow_shows_confirm_button_for_existing_client(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
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
    async def run():
        for uid, text in [(1, 'Создай новую сделку клиенту 89990001122'), (2, '1')]:
            update = Update.model_validate({'update_id': uid, 'message': {'message_id': uid, 'date': 1,
                'chat': {'id': 19, 'type': 'private'}, 'from': {'id': 19, 'is_bot': False, 'first_name': 'Тест'}, 'text': text}}, context={'bot': bot})
            await dp.feed_update(bot, update)
        msgs = [m for m in fake.methods if m.__api_method__ == 'sendMessage']
        assert any('существующего клиента' in m.text and m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row) for m in msgs)
        assert not writes(session)
        await bot.session.close()
    asyncio.run(run())


def test_older_duplicate_draft_is_blocked_after_success(env):
    session, tools, pipe = env
    send(pipe, 'Создай сделку клиенту 89990001122')
    first = send(pipe, '1').drafts[0]
    second = tools.drafts.create(tool='create_offer', method=first['method'], payload=first['payload'], summary=first['summary'], extra={'check': first['check']})
    assert tools.apply_draft(first['token'])['verified']
    with pytest.raises(ValueError, match='уже выполняется'):
        tools.apply_draft(second['token'])
    assert len(writes(session)) == 1
