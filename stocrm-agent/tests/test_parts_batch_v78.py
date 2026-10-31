"""Пакетные диалоги и readback только на локальной имитации CRM."""
import asyncio

import pytest

from stocrm_agent import parts, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import part_keyboard, render_part_choice
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore, BATCH_PREPARING
from stocrm_agent.mock import MockSession
from stocrm_agent.report import format_apply_result
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path, monkeypatch):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    tools.PART_WRITE_INTERVAL = 0
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    pipe._last_offer[19] = 1654
    monkeypatch.setattr(tools, 'resolve_part', lambda **kwargs: {'status': 'not_found', 'candidates': []})
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    return session, tools, pipe


def send(pipe, text):
    return asyncio.run(pipe.handle_text(text, [], 19))


def writes(session):
    return [c for c in session.calls if c['method'] in ('offer/require/add', 'offer/order/add_free_order', 'offer/order/add')]


def dialog(pipe):
    first = send(pipe, 'Добавь товары Ch88011 за 1200\nCh88014e за 4200')
    second = send(pipe, 'добавь как указано')
    final = send(pipe, 'добавь как указано')
    return first, second, final


def test_screenshot_whole_flow_one_confirmation_and_readback(env):
    session, tools, pipe = env
    a, b, c = dialog(pipe)
    assert a.choices[0]['args']['sku'] == 'CH88011'
    assert b.choices[0]['args']['sku'] == 'CH88014E'
    assert not a.drafts and not b.drafts and not writes(session)
    assert len(c.drafts) == 1 and not c.choices
    d = c.drafts[0]
    assert [(i['sku'], i['price'], i['qty']) for i in d['check']['items']] == [('CH88011',1200,1),('CH88014E',4200,1)]
    result = tools.apply_draft(d['token'])
    assert result['verified'] and result['queue_completed'] == 2
    assert [x['method'] for x in writes(session)] == ['offer/require/add','offer/order/add_free_order'] * 2
    requests = [c['payload'] for c in writes(session) if c['method']=='offer/order/add_free_order']
    assert [(x['SKU'], x['PRICE'], x['AMOUNT'], x['CURRENT_VOLUME'], x['MIN_VOLUME']) for x in requests] == [('CH88011',1200,1,1,1),('CH88014E',4200,1,1,1)]
    assert all(x['NEED_MARKUP'] is False for x in requests)
    assert len([c for c in session.calls if c['method']=='products/get_filtered_orders']) >= 2


def test_mismatch_stops_queue_partial_no_repeat_no_correction(env):
    session, tools, pipe = env
    _, _, result = dialog(pipe)
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    token = result.drafts[0]['token']
    with pytest.raises(StocrmError, match='не удалось надёжно проверить количество'):
        tools.apply_draft(token)
    assert tools.drafts.get(token)['status'] == 'partial'
    assert [x['method'] for x in writes(session)] == ['offer/require/add', 'offer/order/add_free_order']
    assert not any(c['method'] in tools.price_methods for c in session.calls)
    with pytest.raises((ValueError, StocrmError)):
        tools.apply_draft(token)
    assert len(writes(session)) == 2
    text = format_apply_result('offer/order/add_free_order', {'ORDER_ID': 8297}, warning='количество 1000000 вместо 1')
    assert 'требует проверки' in text and not text.startswith('✅')


def test_auto_waits_for_all_choices_then_queue(env):
    session, _, pipe = env
    pipe.router.policy.set_confirm_all(False)
    first = send(pipe, 'Добавь товары Ch88011 за 1200\nCh88014e за 4200')
    second = send(pipe, 'добавь как указано')
    assert first.choices and second.choices and not writes(session)
    final = send(pipe, 'добавь как указано')
    assert final.applied and len(writes(session)) == 4 and not final.drafts


def test_missing_price_and_cancel_and_skip(env):
    session, _, pipe = env
    first = send(pipe, 'Добавь товары Ch88011 за 1200\nCh88014e')
    assert first.choices
    assert send(pipe, 'добавь как указано').choices[0]['args']['sku'] == 'CH88014E'
    price = send(pipe, 'добавь как указано')
    assert price.choices[0]['_group_step'] == 'price' and not writes(session)
    labels = [b.text for row in part_keyboard('x', [], text_only=True).inline_keyboard for b in row]
    assert labels == ['❌ Отмена']
    result = send(pipe, '4200')
    assert len(result.drafts) == 1 and [x['price'] for x in result.drafts[0]['check']['items']] == [1200, 4200]
    assert not writes(session)
    send(pipe, 'Добавь товары Ch99011 за 1000\nCh99014 за 2000')
    assert 'отменена' in send(pipe, 'отмена').answer.lower() and not writes(session)
    send(pipe, 'Добавь товары Ch99011 за 1000\nCh99014 за 2000')
    send(pipe, 'пропусти')
    only = send(pipe, 'добавь как указано')
    assert len(only.drafts) == 1 and only.drafts[0]['check']['sku'] == 'CH99014'


def test_unknown_deal_preserves_all(env):
    session, _, pipe = env
    pipe._last_offer.clear()
    q = send(pipe, 'Добавь товары Ch88011 за 1200\nCh88014e за 4200')
    assert q.choices[0]['_group_step'] == 'offer'
    q = send(pipe, '1654')
    assert q.choices[0]['args']['sku'] == 'CH88011'
    assert not writes(session)


def test_batch_search_exact_attribution_and_rank(env, monkeypatch):
    _, _, pipe = env
    calls = []
    def search(skus, **kw):
        calls.append(skus)
        return [websearch.Hit('PILENGA CH88011', 'https://one.example/a'),
                websearch.Hit('ZEKKERT CH88014E', 'https://two.example/b'),
                websearch.Hit('PILENGA CH88011 CH88014E ZEKKERT', 'https://bad.example/c'),
                websearch.Hit('PILENGA CH88011', 'https://two.example/d'),
                websearch.Hit('NEWCO CH88011', 'https://three.example/e')]
    monkeypatch.setattr(websearch, 'search_batch', search)
    first = send(pipe, 'Добавь товары Ch88011 за 1200\nCh88014e за 4200')
    assert calls == [['CH88011', 'CH88014E']]
    assert [x['brand'] for x in first.choices[0]['candidates']] == ['PILENGA', 'NEWCO']
    assert 'Самый вероятный' in render_part_choice(first.choices[0])
    second = pipe.resume_choice(first.choices[0], 0, user_key=19)
    assert [x['brand'] for x in second.choices[0]['candidates']] == ['ZEKKERT']
    stale = pipe.resume_choice(first.choices[0], 0, user_key=19)
    assert 'устарел' in stale.answer and not stale.drafts
    final = pipe.resume_choice(second.choices[0], 0, user_key=19)
    assert [x['brand'] for x in final.drafts[0]['check']['items']] == ['PILENGA','ZEKKERT']
    assert not parts.brands_from_hits([{'title': 'PILENGA CH88011 ZEKKERT CH88014E', 'url':'https://bad.example'}], 'CH88014E')
    assert not parts.brands_from_hits([{'title': 'PILENGA CH880110', 'url':'https://bad.example'}], 'CH88011')


def test_preparing_draft_cannot_be_applied(env):
    session, tools, _ = env
    t = BATCH_PREPARING.set(True)
    try:
        d = tools.draft_add_offer_part(offer_id=1654, name='CH88011', sku='CH88011', price=1200)
    finally:
        BATCH_PREPARING.reset(t)
    with pytest.raises(ValueError, match='собирается'):
        tools.apply_draft(d['token'])
    assert not writes(session)


def test_no_silent_supplier_switch(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь от поставщика товары CH88011 за 1200 и CH88014E за 4200')
    assert 'поставщик' in result.answer and not result.drafts and not writes(session)


def test_search_one_provider_cached_and_respects_429(monkeypatch):
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    calls = []
    def engine(name, query, **kw):
        calls.append((name, query))
        return [websearch.Hit('ZEKKERT CH88011', 'https://one.example/p')]
    monkeypatch.setattr(websearch, '_by_engine', engine)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    assert len(websearch.search_batch(['CH88011', 'CH88014E', 'PC2031', 'FK2010'])) == 1
    websearch.search_batch(['CH88011', 'CH88014E', 'PC2031', 'FK2010'])
    assert len(calls) == 1 and calls[0][0] == 'ddg'
    assert 'OR' in calls[0][1] and all(x in calls[0][1] for x in ['CH88011','CH88014E','PC2031','FK2010'])
    def blocked(name, query, **kw):
        calls.append((name, query))
        raise websearch.WebSearchError('HTTP 429')
    monkeypatch.setattr(websearch, '_by_engine', blocked)
    with pytest.raises(websearch.WebSearchError, match='429'):
        websearch.search_batch(['ZZ1001','ZZ1002'])
    with pytest.raises(websearch.WebSearchError, match='пауза'):
        websearch.search_batch(['ZZ2001','ZZ2002'])
    assert len(calls) == 2
    websearch.clear_cache()


def test_no_order_id_or_no_deal_in_readback_is_not_success(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH12345', sku='CH12345', price=1234)
    original = session._add_order
    def no_id(method, payload):
        original(method, payload)
        return {'CODE': 200, 'RESPONSE': {'SUCCESS': True}}
    session._add_order = no_id
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and not result['verified'] and 'номер' in result['warning']
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    assert len(writes(session)) == 2


def test_telegram_callbacks_no_early_confirmation(env, monkeypatch, tmp_path):
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
            self.counter = 100
        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
            if method.__api_method__ == 'sendMessage':
                self.counter += 1
                return Message.model_validate({'message_id': self.counter, 'date': 1,
                    'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self):
            pass
        async def stream_content(self, *args, **kwargs):
            if False:
                yield b''
    fake = FakeSession()
    bot.session = fake
    user = {'id':19, 'is_bot':False, 'first_name':'Тест'}
    chat = {'id':19, 'type':'private'}
    def update(data):
        return Update.model_validate(data, context={'bot':bot})
    def messages():
        return [m for m in fake.methods if m.__api_method__ == 'sendMessage']
    def button(msg, suffix):
        return next(b.callback_data for row in msg.reply_markup.inline_keyboard for b in row if b.callback_data.endswith(suffix))
    async def run():
        await dp.feed_update(bot, update({'update_id':1,'message':{'message_id':1,'date':1,'chat':chat,'from':user,
            'text':'Добавь в сделку 1654 товары CH88011 за 1200\nCH88014E за 4200'}}))
        first = next(m for m in messages() if m.reply_markup and any(b.callback_data.endswith(':manual') for r in m.reply_markup.inline_keyboard for b in r))
        data = button(first, ':manual')
        await dp.feed_update(bot, update({'update_id':2,'callback_query':{'id':'q1','from':user,'chat_instance':'x', 'data':data,
            'message':{'message_id':101,'date':1,'chat':chat,'text':first.text}}}))
        assert not writes(session)
        assert not any(b.callback_data.startswith('ap:') for m in messages() if m.reply_markup for r in m.reply_markup.inline_keyboard for b in r)
        question_msgs = [m for m in messages() if m.reply_markup and any(b.callback_data.endswith(':manual') for r in m.reply_markup.inline_keyboard for b in r)]
        second = question_msgs[-1]
        assert 'CH88014E' in second.text
        await dp.feed_update(bot, update({'update_id':3,'callback_query':{'id':'q2','from':user,'chat_instance':'x', 'data':button(second, ':manual'),
            'message':{'message_id':102,'date':1,'chat':chat,'text':second.text}}}))
        ready = next(m for m in messages() if m.reply_markup and any(b.callback_data.startswith('ap:') for r in m.reply_markup.inline_keyboard for b in r))
        assert 'CH88011' in ready.text and 'CH88014E' in ready.text
        assert not writes(session)
        await dp.feed_update(bot, update({'update_id':4,'callback_query':{'id':'q3','from':user,'chat_instance':'x', 'data':button(ready, next(b.callback_data[2:] for r in ready.reply_markup.inline_keyboard for b in r if b.callback_data.startswith('ap:'))),
            'message':{'message_id':103,'date':1,'chat':chat,'text':ready.text}}}))
        assert len(writes(session)) == 4
        edits = [m for m in fake.methods if m.__api_method__ == 'editMessageText']
        assert any('проверено позиций' in m.text for m in edits)
    asyncio.run(run())


@pytest.mark.parametrize('engine', ['ddg', 'searx'])
def test_real_engine_uses_only_one_url_and_no_ban_bypass(monkeypatch, engine):
    urls = []
    def blocked(url, timeout):
        urls.append(url)
        raise websearch.WebSearchError('HTTP 429')
    monkeypatch.setattr(websearch, '_get', blocked)
    with pytest.raises(websearch.WebSearchError, match='429'):
        websearch._by_engine(engine, 'example', limit=8, timeout=1, single_request=True)
    assert len(urls) == 1


def test_more_than_four_split_and_keep_each_sku(env, monkeypatch):
    _, _, pipe = env
    calls = []
    monkeypatch.setattr(websearch, 'search_batch', lambda skus: calls.append(list(skus)) or [])
    first = send(pipe, 'Добавь в сделку 1654 товары AB11001 за 1000, AB11002 за 2000, AB11003 за 3000, AB11004 за 4000, AB11005 за 5000')
    assert calls == [['AB11001', 'AB11002', 'AB11003', 'AB11004'], ['AB11005']]
    assert first.choices and not first.drafts
    for _ in range(4):
        result = send(pipe, 'добавь как указано')
        assert result.choices and not result.drafts
    final = send(pipe, 'добавь как указано')
    assert [x['price'] for x in final.drafts[0]['check']['items']] == [1000,2000,3000,4000,5000]


def test_single_with_wrong_quantity_is_partial_without_correction(env):
    session, tools, _ = env
    tools.price_methods = ['offer/order/change']
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH12345', sku='CH12345', price=1234, qty=2)
    original = session._add_order
    def wrong(method, payload):
        result = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return result
    session._add_order = wrong
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    assert not any(c['method'] == 'offer/order/change' for c in session.calls)
    assert 'не удалось надёжно проверить количество' in outcome['warning']
    assert '1000000' not in outcome['warning']


def test_missing_offer_in_readback_is_partial(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH22345', sku='CH22345', price=1234)
    original = session._add_order
    def no_offer(method, payload):
        result = original(method, payload)
        session._order_rows()[-1]['OFFER_ID'] = None
        return result
    session._add_order = no_offer
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']


def test_stale_group_callback_and_cross_sku_candidate(env, monkeypatch):
    _, tools, pipe = env
    def wrong(**kw):
        return {'status':'matched', 'best':{'brand':'ZEKKERT', 'sku':'OTHER01', 'name':'OTHER01'}}
    monkeypatch.setattr(tools, 'resolve_part', wrong)
    first = send(pipe, 'Добавь CH88011 за 1200 и CH88014E за 4200')
    assert not first.choices[0]['candidates']
    pipe._part_pending[19]['at'] -= pipe.to_pending_ttl + 1
    response = pipe.resume_choice(first.choices[0], 0, user_key=19)
    assert 'устарел' in response.answer and not response.drafts


def test_quantity_price_retained_per_position(env):
    session, _, pipe = env
    first = send(pipe, 'Добавь в сделку 1654 2 шт CH88011 за 1200 и 3 шт CH88014E за 4200')
    assert first.choices
    send(pipe, 'добавь как указано')
    result = send(pipe, 'добавь как указано')
    assert [(x['sku'],x['price'],x['qty']) for x in result.drafts[0]['check']['items']] == [('CH88011',1200,2),('CH88014E',4200,3)]
    assert not writes(session)


def test_preparing_keyboard_cannot_apply_and_partial_is_not_called_unwritten(env):
    from stocrm_agent.bot.app import drafts_keyboard, drafts_block
    waiting = {'token':'draft_t', 'status':'pending', 'batch_preparing':True, 'summary':'позиция'}
    keyboard = drafts_keyboard([waiting])
    assert keyboard and all(not b.callback_data.startswith('ap:') for row in keyboard.inline_keyboard for b in row)
    partial = {'token':'draft_p', 'status':'partial', 'summary':'позиция'}
    assert 'пока ничего не изменено' not in drafts_block([partial])


def test_auto_mismatch_stops_and_preserves_partial(env):
    session, tools, pipe = env
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    pipe.router.policy.set_confirm_all(False)
    a, b, c = dialog(pipe)
    assert not a.drafts and not b.drafts
    assert len(writes(session)) == 2 and c.drafts[0]['status'] == 'partial'
    assert 'не повторяйте' in c.answer.lower()
    stored = tools.drafts.get(c.drafts[0]['token'])
    assert len(stored['result']) == 2
    assert stored['done_steps']['failed_index'] == 1


def test_review_cannot_confirm_unstarted_or_old_match(env):
    session, tools, pipe = env
    _, _, result = dialog(pipe)
    original = session._add_order
    def wrong(method, payload):
        result = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return result
    session._add_order = wrong
    d = result.drafts[0]
    with pytest.raises(StocrmError):
        tools.apply_draft(d['token'])
    session._order_rows().append({'ORDER_ID':999999,'OFFER_ID':1654,'SKU':'CH88014E','BRAND':'NONAME',
        'NAME':'CH88014E','QUANTITY':1,'PRICE':4200,'ACTIVE':'Y'})
    review = tools.review_draft(d['token'])
    assert not review['verified']
    assert review['details'][1]['matches'] == 1 and not review['details'][1]['verified']
    assert 'не запускалась' in review['details'][1]['execution']
    with pytest.raises(ValueError):
        tools.confirm_draft_result(d['token'])


def test_batch_safety_does_not_depend_on_fastpath_setting(env):
    session, _, pipe = env
    pipe.fastpath = False
    first = send(pipe, 'Добавь в сделку 1654 CH88011 за 1200 и CH88014E за 4200')
    assert first.choices and not first.drafts and not writes(session)
    second = send(pipe, 'добавь как указано')
    assert second.choices and not second.drafts and not writes(session)
    final = send(pipe, 'добавь как указано')
    assert len(final.drafts) == 1 and len(final.drafts[0]['check']['items']) == 2


def test_partial_blocks_new_command_and_survives_archive(env):
    session, tools, pipe = env
    _, _, result = dialog(pipe)
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    with pytest.raises(StocrmError):
        tools.apply_draft(result.drafts[0]['token'])
    with pytest.raises(ValueError, match='непроверенная попытка'):
        tools.draft_add_offer_part(offer_id=1654, name='PILENGA CH88011', sku='CH88011', brand='PILENGA', price=1200)
    tools.drafts.archive(result.drafts[0]['token'])
    assert tools.drafts.get(result.drafts[0]['token'])['archived_partial']
    with pytest.raises(ValueError, match='непроверенная попытка'):
        tools.draft_add_offer_part(offer_id=1654, name='PILENGA CH88011', sku='CH88011', brand='PILENGA', price=1200)
    assert len(writes(session)) == 2


def test_prepared_before_partial_is_blocked_at_claim(env):
    session, tools, pipe = env
    prior = tools.draft_add_offer_part(offer_id=1654, name='CH88011', sku='CH88011', brand='NEWCO', price=1200)
    _, _, result = dialog(pipe)
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    with pytest.raises(StocrmError):
        tools.apply_draft(result.drafts[0]['token'])
    with pytest.raises(ValueError, match='непроверенная попытка'):
        tools.apply_draft(prior['token'])
    assert len(writes(session)) == 2


def test_archived_partial_can_only_be_cleared_after_proven_readback(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH22345', sku='CH22345', price=1234)
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial'
    tools.drafts.archive(draft['token'])
    with pytest.raises(ValueError):
        tools.confirm_draft_result(draft['token'])
    session._order_rows()[-1]['QUANTITY'] = 1
    confirmed = tools.confirm_draft_result(draft['token'])
    assert confirmed['status'] == 'applied' and not confirmed['archived_partial']
    tools.draft_add_offer_part(offer_id=1654, name='CH22345', sku='CH22345', price=1234)


def test_legacy_applied_warning_is_blocked_and_reviewable(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH32345', sku='CH32345', price=1234)
    original = session._add_order
    def wrong(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = wrong
    tools.apply_draft(draft['token'])
    tools.drafts.mark(draft['token'], 'applied', check_warning='неверное количество')
    with pytest.raises(ValueError, match='непроверенная попытка'):
        tools.draft_add_offer_part(offer_id=1654, name='CH32345', sku='CH32345', price=1234)
    session._order_rows()[-1]['QUANTITY'] = 1
    assert tools.confirm_draft_result(draft['token'])['review']['verified']
