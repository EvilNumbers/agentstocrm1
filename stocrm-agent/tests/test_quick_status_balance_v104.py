"""Все контакты одного статуса и изоляция баланса: только фальшивая CRM."""
import asyncio

import pytest

from stocrm_agent.bot import quick_read
from stocrm_agent.mock import MockResponse
from test_quick_read import env, send  # noqa: F401


def add_other_contact_deal(session):
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1902, 'CONTACT_ID': 8123,
        'CONTACT_TITLE': 'Клиент Б', 'BOARD_ID': 2, 'STATUS_ID': 51,
        'STATUS_NAME': 'Быстрые сделки', 'OFFER_SUM': 1300})
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 1001, 'OFFER_ID': 1902,
        'TITLE': 'Работа клиента Б', 'SUM': 1300})


def test_status_includes_other_contacts_but_each_card_is_its_own(env):
    session, pipe = env
    add_other_contact_deal(session)
    result = send(pipe, 'Покажи быстрые сделки')
    choice = result.choices[0]
    assert set(choice['offers']) == {1900, 1901, 1902}
    assert choice['contacts'][1902] == 8123
    details = choice['question']
    a = details.split('📋 Быстрая сделка №1900', 1)[1].split('📋 Быстрая сделка', 1)[0]
    b = details.split('📋 Быстрая сделка №1902', 1)[1].split('📋 Быстрая сделка', 1)[0]
    assert 'Клиент Б' not in a and 'Работа клиента Б' not in a
    assert 'Клиент Б' in b and 'Работа клиента Б' in b
    index = choice['offers'].index(1902)
    card = pipe.quick_read_choice(choice, 1, index)
    assert 'Клиент Б' in card.answer and 'Работа клиента Б' in card.answer
    assert all(c['method'] not in {'offer/new/with_contact', 'work/add', 'offer/status/change',
                                   'offer/require/add', 'offer/order/add_free_order'} for c in session.calls)


def test_wrong_contact_or_status_after_list_cannot_be_selected(env):
    session, pipe = env
    choice = send(pipe, 'Покажи быстрые сделки').choices[0]
    oid = choice['offers'][0]
    for row in session.data['offers/get_from_filter']:
        if row['OFFER_ID'] == oid:
            row['CONTACT_ID'] = 9999
    assert 'не удалось подтвердить' in pipe.quick_read_choice(choice, 1, 0).answer.lower()


def test_unknown_status_cannot_be_reported_as_empty(env):
    session, pipe = env
    for row in session.data['offers/get_from_filter']:
        row['STATUS_ID'] = None
    response = send(pipe, 'Покажи быстрые сделки')
    assert not response.choices and 'неполон' in response.answer
    assert not any('не найдено' in response.answer for _ in [0])


def test_one_unknown_row_warns_but_never_appears_as_another_card(env):
    session, pipe = env
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1999, 'CONTACT_ID': 777, 'BOARD_ID': 2, 'STATUS_ID': None})
    result = send(pipe, 'Покажи быстрые сделки')
    assert result.choices and 'неполным' in result.choices[0]['question']
    assert 1999 not in result.choices[0]['offers']


def test_readback_fields_are_explicit_and_checked_even_if_crm_ignores_filters(env):
    session, pipe = env
    add_other_contact_deal(session)
    result = send(pipe, 'Покажи быстрые сделки')
    assert 1654 not in result.choices[0]['offers']
    calls = [c['payload'] for c in session.calls if c['method'] == 'offers/get_from_filter']
    assert calls and all(set(('OFFER_ID', 'CONTACT_ID', 'STATUS_ID', 'BOARD_ID')) <= set(p['REQUIRED_FIELDS']) for p in calls)


def test_required_fields_can_restore_missing_default_status(env, monkeypatch):
    session, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('/offers/get_from_filter'):
            body = response.json()
            if not {'STATUS_ID', 'BOARD_ID', 'CONTACT_ID'} <= set((json or {}).get('REQUIRED_FIELDS') or []):
                for row in body.get('RESPONSE', []):
                    row.pop('STATUS_ID', None)
            return MockResponse(body)
        return response
    monkeypatch.setattr(session, 'post', post)
    assert set(send(pipe, 'Покажи быстрые сделки').choices[0]['offers']) == {1900, 1901}


def test_work_without_deal_identity_is_not_shown(env, monkeypatch):
    session, pipe = env
    original = pipe.tools._read
    def raw(tool, method, payload=None):
        if method == 'work/get_filtered':
            return {'DATA': [{'TITLE': 'Чужая работа', 'SUM': 500}]}
        return original(tool, method, payload)
    monkeypatch.setattr(pipe.tools, '_read', raw)
    card = pipe.tools.offer_card(1900, with_items=True)
    assert card['works'] == [] and card.get('works_error')
    assert 'Чужая работа' not in quick_read.status_detail(pipe.tools, (2, 51), 1900, 5555)


@pytest.mark.parametrize('response', [None, {}, [], {'RUB_CASH': {'CURRENCY_VALUE': 0}},
    {'RUB_CASH': {'CURRENCY_VALUE': 0, 'ENTITY_ID': 999, 'ENTITY_TYPE': 'OFFER'}},
    {'RUB_CASH': {'CURRENCY_VALUE': 0, 'ENTITY_ID': 1900, 'ENTITY_TYPE': 'CONTACT'}},
    {'RUB_CASH': {'CURRENCY_VALUE': 'invalid', 'ENTITY_ID': 1900, 'ENTITY_TYPE': 'OFFER'}}])
def test_unknown_balance_never_becomes_zero_or_borrowed(env, monkeypatch, response):
    _, pipe = env
    original = pipe.tools._read
    def read(tool, method, payload=None):
        return response if method == 'payment/get_balance' else original(tool, method, payload)
    monkeypatch.setattr(pipe.tools, '_read', read)
    card = pipe.tools.offer_card(1900, with_items=True)
    assert card['debt'] is None and card['paid'] is None
    assert 'Долг: не удалось проверить' in quick_read.status_detail(pipe.tools, (2, 51), 1900, 5555)


def test_balance_correctly_calculated_for_matching_entity(env, monkeypatch):
    _, pipe = env
    original = pipe.tools._read
    def read(tool, method, payload=None):
        if method == 'payment/get_balance':
            return {'RUB_CASH': {'CURRENCY_VALUE': '1500', 'ENTITY_ID': 1900, 'ENTITY_TYPE': 'OFFER'}}
        return original(tool, method, payload)
    monkeypatch.setattr(pipe.tools, '_read', read)
    card = pipe.tools.offer_card(1900, with_items=True)
    assert card['paid'] == 1500 and card['debt'] == 4500


def test_balance_diagnostic_never_returns_raw_contents(env, monkeypatch):
    _, pipe = env
    monkeypatch.setattr(pipe.tools, '_read', lambda *a, **k: {'RUB_CASH': {'CURRENCY_VALUE': '123456',
        'ENTITY_ID': 999, 'ENTITY_TYPE': 'OFFER', 'SECRET': 'private-data'}, 'DATA': {'secret': 'private-data'}})
    result = pipe.tools.balance_diagnostic(1900)
    assert result['wrong_entity'] == 1 and result['known_wrappers'] == ['DATA']
    assert '123456' not in str(result) and 'private-data' not in str(result)


def test_telegram_status_and_balance_diagnostic_are_read_only(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, pipe = env
    add_other_contact_deal(session)
    monkeypatch.setattr(app, 'build_tools', lambda cfg: pipe.tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678',
            allowed_users=frozenset({19, 20}), admin_users=frozenset({19})),
            Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
    class Fake(BaseSession):
        def __init__(self):
            super().__init__(); self.methods = []
        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
            if method.__api_method__ == 'sendMessage':
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self): pass
        async def stream_content(self, *args, **kwargs):
            if False: yield b''
    fake = Fake(); bot.session = fake
    chat = {'id': 19, 'type': 'private'}
    def update(n, who, text):
        return Update.model_validate({'update_id': n, 'message': {'message_id': n, 'date': 1,
            'chat': chat, 'from': {'id': who, 'is_bot': False, 'first_name': 'Test'}, 'text': text}}, context={'bot': bot})
    def messages(): return [m for m in fake.methods if m.__api_method__ == 'sendMessage']
    async def run():
        await dp.feed_update(bot, update(1, 19, 'Покажи быстрые сделки'))
        choice = next(m for m in messages() if m.reply_markup and any(
            b.callback_data.startswith('quick:') for row in m.reply_markup.inline_keyboard for b in row))
        assert 'Клиент Б' in choice.text
        assert any('1902' in b.text for row in choice.reply_markup.inline_keyboard for b in row)
        await dp.feed_update(bot, update(2, 19, '/balance_test 1902'))
        assert any('Диагностика баланса сделки №1902' in m.text and 'только чтение' in m.text for m in messages())
        count = sum(c['method'] == 'payment/get_balance' for c in session.calls)
        await dp.feed_update(bot, update(3, 20, '/balance_test 1902'))
        assert 'только администратору' in messages()[-1].text
        assert sum(c['method'] == 'payment/get_balance' for c in session.calls) == count
        await dp.feed_update(bot, update(4, 19, '/offer_test 1902'))
        assert any('Диагностика сделки №1902' in m.text and 'ID статуса: 51' in m.text for m in messages())
        assert 'Клиент Б' not in messages()[-1].text
        await dp.feed_update(bot, update(5, 20, '/offer_test 1902'))
        assert 'только администратору' in messages()[-1].text
        await bot.session.close()
    asyncio.run(run())
    assert not any(c['method'] in {'offer/new/with_contact', 'work/add', 'offer/status/change', 'offer/order/add_free_order'} for c in session.calls)


def test_quick_deal_readback_requests_status_and_does_not_repeat_writes(monkeypatch, tmp_path):
    from stocrm_agent.audit import AuditLog
    from stocrm_agent.bot.pipeline import BotPipeline
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.client import StocrmClient
    from stocrm_agent.drafts import DraftStore
    from stocrm_agent.tools import Toolset
    from test_quick_deal_v100 import QuickSession, send as quick_send, writes

    session = QuickSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('/offers/get_from_filter'):
            body = response.json()
            if 'STATUS_ID' not in (json or {}).get('REQUIRED_FIELDS', []):
                for row in body.get('RESPONSE', []):
                    row.pop('STATUS_ID', None)
            return MockResponse(body)
        return response
    monkeypatch.setattr(session, 'post', post)
    draft = quick_send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    assert not writes(session)
    result = tools.apply_draft(draft['token'])
    assert result['verified'] and [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']
