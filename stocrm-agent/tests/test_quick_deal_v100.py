"""Составные быстрые сделки: только fake CRM, никаких живых запросов."""
import asyncio
import concurrent.futures
import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession, MockResponse
from stocrm_agent.tools import Toolset


class QuickSession(MockSession):
    def __init__(self):
        super().__init__()
        self.requires = {}
        self.data['contacts/get_from_filter'].append({'CONTACT_ID': 5555, 'TITLE': '1111 быстрые сделки', 'MAIN_PHONE': '81111111110'})
        self.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'Y'}

    def post(self, url, json=None, timeout=None):
        body = json or {}
        method = url.split('/api/external/v1/')[-1]
        if method == 'offer/require/add':
            self.calls.append({'method': method, 'payload': body})
            rid = 6000 + len(self.requires)
            self.requires[rid] = dict(body)
            return MockResponse({'CODE': 200, 'RESPONSE': {'REQUIRE_ID': rid, 'OFFER_ID': body['OFFER_ID']}})
        if method == 'offer/order/add_free_order':
            self.calls.append({'method': method, 'payload': body})
            source = self.requires[body['REQUIRE_ID']]
            order = {'ORDER_ID': 7000 + len(self.requires), 'OFFER_ID': source['OFFER_ID'],
                     'NAME': body['NAME'], 'BRAND': body['BRAND'], 'SKU': body['SKU'],
                     'PRICE': body['PRICE'], 'QUANTITY_CURRENT': body['AMOUNT'], 'ACTIVE': 'Y'}
            self.data['products/get_filtered_orders']['DATA'].append(order)
            return MockResponse({'CODE': 200, 'RESPONSE': {'ORDERS': order}})
        return super().post(url, json=json, timeout=timeout)


@pytest.fixture
def env(tmp_path, monkeypatch):
    session = QuickSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    return session, tools, pipe


def send(pipe, text, user=1):
    return asyncio.run(pipe.handle_text(text, [], user))


def writes(session):
    methods = {'offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order', 'offer/order/add'}
    return [c for c in session.calls if c['method'] in methods]


def existing(session, oid):
    session.data['offers/get_from_filter'].append({'OFFER_ID': oid, 'CONTACT_ID': 5555, 'CONTACT_TITLE': '1111 быстрые сделки', 'BOARD_ID': 2, 'STATUS_ID': 51, 'STATUS_NAME': 'Быстрые сделки', 'OFFER_SUM': 6000})


def test_new_work_and_article_confirmed_as_one_atomic_user_decision(env):
    session, tools, pipe = env
    result = send(pipe, 'новая быстрая сделка, прокачка тормозов 1000 рублей и добавь артикул NGK CR9E, цена 300')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    draft = result.drafts[0]
    assert 'прокачка тормозов' in draft['summary'] and '1000' in draft['summary'] and '300' in draft['summary']
    assert 'NGK' in draft['summary'] and 'NONAME' not in draft['summary']
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'applied' and outcome['verified'], outcome
    calls = writes(session)
    assert [c['method'] for c in calls] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert calls[0]['payload']['PHONE'] == '81111111110' and calls[0]['payload']['CONTACT_ID'] == 5555
    assert calls[2]['payload']['DATA']['SUM'] == 1000
    assert calls[4]['payload']['PRICE'] == 300 and calls[4]['payload']['NEED_MARKUP'] is False
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 5


def test_missing_prices_stay_separate_and_no_write_before_confirmation(env):
    session, tools, pipe = env
    result = send(pipe, 'новая быстрая сделка прокачка тормозов 1000 рублей и добавь артикул W914/2 и OP572 цена 200')
    assert 'W914/2' in result.answer and 'цен' in result.answer.lower() and not result.drafts
    assert 'цен' in send(pipe, '0').answer.lower()
    assert pipe._quick_price_reply('450', 2) is None  # другой пользователь не отвечает за автора
    result = send(pipe, '450')
    assert 'бренд' in result.answer.lower() and not result.drafts
    result = send(pipe, 'MANN')
    assert 'бренд' in result.answer.lower() and not result.drafts
    result = send(pipe, 'FILTRON')
    assert len(result.drafts) == 1, result.answer
    p = result.drafts[0]['payload']
    assert p['work']['price'] == 1000 and [x['price'] for x in p['articles']] == [450, 200]
    assert not writes(session)
    outcome = tools.apply_draft(result.drafts[0]['token'])
    assert outcome['status'] == 'applied', outcome
    assert [c['payload']['PRICE'] for c in writes(session) if c['method'] == 'offer/order/add_free_order'] == [450, 200]


def test_existing_choice_and_new_option(env):
    session, tools, pipe = env
    existing(session, 1900); existing(session, 1901)
    result = send(pipe, 'быстрая сделка прокачка тормозов 1000 рублей')
    choice = result.choices[0]
    assert choice['kind'] == 'quick_select' and set(choice['offers']) == {1900, 1901}
    assert 'Долг' in choice['question'] and not writes(session)
    assert 'устарел' in pipe.quick_read_choice(choice, 2, 0).answer
    selection = pipe.quick_read_choice(choice, 1, 0)
    draft = selection.drafts[0]
    assert draft['payload']['offer_id'] in {1900, 1901}
    outcome = tools.apply_draft(draft['token'])
    assert outcome['verified'] and [c['method'] for c in writes(session)] == ['work/add']
    second = send(pipe, 'быстрая сделка диагностика 1200 рублей').choices[0]
    newdraft = pipe.quick_read_choice(second, 1, len(second['offers'])).drafts[0]
    assert newdraft['payload']['offer_id'] is None


def test_explicit_new_bypasses_choice_but_not_confirmation(env):
    session, _, pipe = env
    existing(session, 1900); existing(session, 1901)
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert len(result.drafts) == 1 and not result.choices and not writes(session)


def test_no_existing_implicitly_new(env):
    session, _, pipe = env
    result = send(pipe, 'быстрая сделка диагностика 1000 рублей')
    assert len(result.drafts) == 1 and result.drafts[0]['payload']['offer_id'] is None
    assert not writes(session)


def test_stale_choice_and_changed_status_before_apply(env):
    session, tools, pipe = env
    existing(session, 1900); existing(session, 1901)
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей').choices[0]
    session.data['offers/get_from_filter'][-2].update(STATUS_ID=15, STATUS_NAME='В работе')
    assert not pipe.quick_read_choice(choice, 1, 0).drafts
    session.data['offers/get_from_filter'][-2].update(STATUS_ID=51, STATUS_NAME='Быстрые сделки')
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей').choices[0]
    draft = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    session.data['offers/get_from_filter'][-2].update(STATUS_ID=15, STATUS_NAME='В работе')
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not writes(session)


@pytest.mark.parametrize('method', ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order'])
def test_timeout_after_each_write_blocks_retries(env, monkeypatch, method):
    session, tools, pipe = env
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300')
    draft = result.drafts[0]
    original = tools.client.call
    def lost(method_name, payload=None, **kwargs):
        response = original(method_name, payload, **kwargs)
        if method_name == method:
            raise StocrmError('таймаут после записи', method=method)
        return response
    monkeypatch.setattr(tools.client, 'call', lost)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    count = len(writes(session))
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    again = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert not again.drafts and ('незавершённая' in again.answer or 'незавершенное' in again.answer.lower())
    assert len(writes(session)) == count
    tools.drafts.resolve_quick_manually(draft['token'], user_id=1)
    assert tools.drafts.get(draft['token'])['status'] == 'discarded'
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])


def test_parallel_confirmations_write_only_once(env):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    def apply():
        try:
            return tools.apply_draft(draft['token'])
        except ValueError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert len([r for r in results if r and r['verified']]) == 1
    assert sum(c['method'] == 'offer/new/with_contact' for c in writes(session)) == 1


def test_reject_other_actions_and_wrong_contact(env):
    session, _, pipe = env
    for text in ('быстрая сделка удали работу диагностика', 'быстрая сделка оплата 1000 рублей', 'новая быстрая сделка с поставщиком артикул NGK CR9E цена 200'):
        result = send(pipe, text)
        assert not result.drafts and not result.choices
    session.data['contacts/get_from_filter'][-1]['TITLE'] = 'Не тот контакт'
    assert not send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts
    assert not writes(session)


def test_multiple_reasons_are_explicitly_selected(env):
    session, tools, pipe = env
    session.data['offer/all_statuses']['51']['REASONS'] = [
        {'REASON_ID': 90, 'TITLE': 'Розница'}, {'REASON_ID': 91, 'TITLE': 'Обслуживание'}]
    first = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert not first.drafts and first.choices and first.choices[0]['kind'] == 'quick_reason', first.answer
    choice = first.choices[0]
    selected = pipe.quick_read_choice(choice, 1, 1)
    assert len(selected.drafts) == 1, selected.answer
    assert selected.drafts[0]['payload']['reason']['id'] == 91
    assert tools.apply_draft(selected.drafts[0]['token'])['verified']
    assert next(c for c in writes(session) if c['method'] == 'offer/status/change')['payload']['REASON_ID'] == 91


def test_foreign_require_response_never_creates_order(env):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка артикул NGK CR9E цена 300').drafts[0]
    original = tools.client.call
    def wrong(method, payload=None, **kwargs):
        if method == 'offer/require/add':
            return {'REQUIRE_ID': 123, 'OFFER_ID': 1654}
        return original(method, payload, **kwargs)
    tools.client.call = wrong
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial'
    assert not any(c['method'] == 'offer/order/add_free_order' for c in writes(session))


def test_new_request_cancels_stale_choice(env):
    session, _, pipe = env
    existing(session, 1900)
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей').choices[0]
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert result.drafts
    assert 'устарел' in pipe.quick_read_choice(choice, 1, 0).answer


def test_draft_tampering_and_changed_contact_prevent_all_writes(env):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    with tools.drafts._locked():
        tools.drafts._data[draft['token']]['payload']['work']['price'] = 1
        tools.drafts._save()
    with pytest.raises(ValueError, match='Целостность'):
        tools.apply_draft(draft['token'])
    assert not writes(session)
    tools.drafts.archive(draft['token'])
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    session.data['contacts/get_from_filter'][-1]['TITLE'] = 'Не тот'
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_extra_actions_not_silently_dropped(env):
    session, _, pipe = env
    for text in ('новая быстрая сделка диагностика 1000 и добавь работу замена масла 2000',
                 'новая быстрая сделка диагностика 1000 и добавь артикул NGK CR9E цена 300 и добавь работу масло 500'):
        response = send(pipe, text)
        assert not response.drafts and 'не удалось' in response.answer.lower()
    assert not writes(session)


def test_interrupted_applying_draft_survives_purge_and_requires_stale_manual_review(env):
    _, tools, pipe = env
    import time
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    tools.drafts.claim(draft['token'])
    with pytest.raises(ValueError, match='ещё выполняться'):
        tools.drafts.resolve_quick_manually(draft['token'], user_id=1)
    tools.drafts.mark(draft['token'], 'applying', claimed_at=time.time() - 900000, created_at=time.time() - 900000)
    tools.drafts.purge(keep_days=1)
    assert tools.drafts.get(draft['token'])['status'] == 'applying'
    tools.drafts.resolve_quick_manually(draft['token'], user_id=1)
    assert tools.drafts.get(draft['token'])['status'] == 'discarded'
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])


def test_telegram_choice_owner_and_single_confirmation(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    existing(session, 1900); existing(session, 1901)
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
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
    user = {'id': 19, 'is_bot': False, 'first_name': 'Тест'}
    def update(data): return Update.model_validate(data, context={'bot': bot})
    def msgs(): return [m for m in fake.methods if m.__api_method__ == 'sendMessage']
    async def run():
        await dp.feed_update(bot, update({'update_id': 1, 'message': {'message_id': 1, 'date': 1, 'chat': chat, 'from': user, 'text': 'быстрая сделка диагностика 1000 рублей'}}))
        msg = next(m for m in msgs() if m.reply_markup and any(b.callback_data.startswith('quick:') for row in m.reply_markup.inline_keyboard for b in row))
        buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
        assert any('Создать новую' in b.text for b in buttons) and 'Долг' in msg.text
        data = buttons[0].callback_data
        cb = {'id': 'cb1', 'from': user, 'chat_instance': 'x', 'data': data, 'message': {'message_id': 101, 'date': 1, 'chat': chat, 'text': msg.text}}
        await dp.feed_update(bot, update({'update_id': 2, 'callback_query': cb}))
        ready = next(m for m in msgs() if m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row))
        assert 'диагностика' in ready.text and not writes(session)
        apply_button = next(b.callback_data for row in ready.reply_markup.inline_keyboard for b in row if b.callback_data.startswith('ap:'))
        cb2 = {'id': 'cb2', 'from': user, 'chat_instance': 'x', 'data': apply_button, 'message': {'message_id': 102, 'date': 1, 'chat': chat, 'text': ready.text}}
        await dp.feed_update(bot, update({'update_id': 3, 'callback_query': cb2}))
        assert [c['method'] for c in writes(session)] == ['work/add']
        await bot.session.close()
    asyncio.run(run())


def test_large_contact_database_does_not_block_identified_system_contact(env):
    session, _, pipe = env
    session.data['contacts/get_from_filter'].extend({'CONTACT_ID': 8000 + i, 'TITLE': f'Другой клиент {i}', 'MAIN_PHONE': '79995555555'} for i in range(50))
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert result.drafts, result.answer


def test_two_system_contacts_are_ambiguous_even_when_one_has_extra_number(env):
    session, _, pipe = env
    session.data['contacts/get_from_filter'].append({'CONTACT_ID': 5556, 'TITLE': '1111 быстрые сделки', 'MAIN_PHONE': '79991112233', 'EXTRA_PHONES': ['81111111110']})
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert not result.drafts and 'однозначно' in result.answer and not writes(session)


def test_unverified_work_stops_chain_before_article(env, monkeypatch):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    monkeypatch.setattr(tools, '_verify_work_sum', lambda check: 'сумма не подтверждена')
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and 'WORK_ID' in outcome['result']
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_changes_in_status_during_write_follow_selected_deal(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    draft = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original = tools.client.call
    def moved(method, payload=None, **kwargs):
        response = original(method, payload, **kwargs)
        if method == 'work/add':
            session.data['offers/get_from_filter'][-1].update(STATUS_ID=15, STATUS_NAME='В работе')
        return response
    monkeypatch.setattr(tools.client, 'call', moved)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_reason_choice_after_existing_or_new_selection(env):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['offer/all_statuses']['51']['REASONS'] = [{'REASON_ID': 90, 'TITLE': 'А'}, {'REASON_ID': 91, 'TITLE': 'Б'}]
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей').choices[0]
    next_choice = pipe.quick_read_choice(choice, 1, len(choice['offers'])).choices[0]
    assert next_choice['kind'] == 'quick_reason'
    result = pipe.quick_read_choice(next_choice, 1, 0)
    assert result.drafts and result.drafts[0]['payload']['reason']['id'] == 90
    assert tools.apply_draft(result.drafts[0]['token'])['verified']


def test_archived_unknown_still_blocks_until_manual_review(env, monkeypatch):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts[0]
    # Потеря ответа только на запись; контрольное чтение работает.
    original = tools.client.call
    def lost(method, payload=None, **kwargs):
        if method == 'offer/new/with_contact':
            raise StocrmError('ответ потерян')
        return original(method, payload, **kwargs)
    monkeypatch.setattr(tools.client, 'call', lost)
    assert tools.apply_draft(draft['token'])['status'] == 'partial'
    tools.drafts.archive(draft['token'])
    assert not send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts
    tools.drafts.resolve_quick_manually(draft['token'], user_id=19)
    assert send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts


def test_explicit_small_price_is_not_lost(env):
    session, _, pipe = env
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 5')
    assert result.drafts and result.drafts[0]['payload']['articles'][0]['price'] == 5
    assert not writes(session)


def test_quantity_not_mistaken_for_article_price(env):
    session, _, pipe = env
    result = send(pipe, 'новая быстрая сделка артикул NGK CR9E за 2 штуки')
    assert not result.drafts and 'цен' in result.answer.lower()
    assert not writes(session)
