"""Удаление и ввод бренда: только MockSession и подменённые поиски."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from test_quick_deal_v100 import env, send, writes  # noqa: F401
from stocrm_agent import parts, quick_deal
from stocrm_agent.bot.app import drafts_keyboard


def test_quick_unknown_brand_waits_and_keeps_everything(env, monkeypatch):
    session, tools, pipe = env
    calls = []
    def search(**kwargs):
        calls.append(kwargs['sku'])
        return {'status': 'not_found', 'candidates': [], 'warnings': ['источник недоступен']}
    monkeypatch.setattr(tools, 'resolve_part', search)
    q = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул W914/2 цена 450 и OP572 цена 200')
    assert 'бренд' in q.answer.lower() and 'не доказано' in q.answer.lower() and not q.drafts
    assert calls == ['W914/2'] and not writes(session)
    assert pipe._quick_price_reply('MANN', 2) is None
    assert 'бренд' in send(pipe, 'NONAME').answer.lower()
    q = send(pipe, 'MANN')
    assert calls == ['W914/2', 'OP572'] and 'OP572' in q.answer and not q.drafts
    q = send(pipe, 'FILTRON')
    assert len(q.drafts) == 1 and calls == ['W914/2', 'OP572']
    payload = q.drafts[0]['payload']
    assert payload['work']['price'] == 1000
    assert [(x['sku'], x['brand'], x['price']) for x in payload['articles']] == [('W914/2', 'MANN', 450), ('OP572', 'FILTRON', 200)]
    assert not writes(session)


def test_quick_choices_exact_sku_owner_ttl_and_manual(env, monkeypatch):
    session, tools, pipe = env
    candidates = [{'sku': 'CR9E', 'brand': 'NGK', 'source': 'Росско'},
                  {'sku': 'CR9E', 'brand': 'DENSO', 'source': 'Профит Лига'},
                  {'sku': 'CR9E1', 'brand': 'WRONG', 'source': 'интернет'},
                  {'sku': 'CR9E', 'brand': None}]
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'ambiguous', 'candidates': candidates})
    q = send(pipe, 'новая быстрая сделка артикул CR9E цена 300')
    choice = q.choices[0]
    assert [c['brand'] for c in choice['candidates']] == ['NGK', 'DENSO']
    assert not pipe.quick_read_choice(choice, 2, 0).drafts
    manual = pipe.quick_read_choice(choice, 1, 2)
    assert 'введите бренд' in manual.answer.lower() and not manual.drafts
    assert not pipe.quick_read_choice(choice, 1, 0).drafts  # устаревшая кнопка
    q = send(pipe, 'ACME')
    assert q.drafts[0]['payload']['articles'][0]['brand'] == 'ACME'
    assert not writes(session)


def test_quick_expired_or_cancelled_brand_does_not_make_draft(env):
    session, tools, pipe = env
    q = send(pipe, 'новая быстрая сделка артикул CR9E цена 300')
    assert 'бренд' in q.answer.lower()
    pipe._quick_pending[1]['at'] = time.time() - 1801
    assert 'устарел' in send(pipe, 'NGK').answer
    assert not tools.drafts.list() and not writes(session)
    send(pipe, 'новая быстрая сделка артикул CR9E цена 300')
    assert 'отменено' in send(pipe, 'отмена').answer.lower()
    assert not tools.drafts.list() and not writes(session)


def _draft(env):
    _, _, pipe = env
    return send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]


def test_remove_work_then_last_article_cancels_and_old_buttons_fail(env):
    session, tools, _ = env
    old = _draft(env)
    buttons = [b for row in drafts_keyboard([old]).inline_keyboard for b in row]
    assert any('Работа' in b.text and b.callback_data.endswith(':1') for b in buttons)
    assert any('Артикул' in b.text and b.callback_data.endswith(':2') for b in buttons)
    out = tools.drop_draft_position(old['token'], number=1)
    new = out['draft']
    assert new['token'] != old['token'] and new['payload']['work'] is None
    assert new['payload']['articles'][0]['brand'] == 'NGK' and 'диагностика' not in new['summary']
    tools.drafts.verify(new['token'])
    with pytest.raises(ValueError):
        tools.apply_draft(old['token'])
    with pytest.raises(ValueError):
        tools.drop_draft_position(old['token'], number=2)
    assert tools.drop_draft_position(new['token'], number=1)['status'] == 'discarded'
    with pytest.raises(ValueError):
        tools.apply_draft(new['token'])
    assert not writes(session)


def test_remove_article_keeps_work_and_apply_only_survivor(env):
    session, tools, _ = env
    old = _draft(env)
    new = tools.drop_draft_position(old['token'], number=2)['draft']
    assert new['payload']['articles'] == [] and new['payload']['work']['price'] == 1000
    assert 'CR9E' not in new['summary']
    result = tools.apply_draft(new['token'])
    assert result['verified']
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_quick_drop_race_only_one_revision_and_applying_cannot_edit(env):
    session, tools, _ = env
    draft = _draft(env)
    def remove(n):
        try:
            return tools.drop_draft_position(draft['token'], number=n)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove, [1, 2]))
    assert sum(x is not None for x in results) == 1
    new = next(x['draft'] for x in results if x)
    tools.drafts.claim(new['token'])
    with pytest.raises(ValueError):
        tools.drop_draft_position(new['token'], number=1)
    assert not writes(session)


def test_regular_batch_revisions_keep_steps_and_stale_apply_blocked(env):
    session, tools, _ = env
    a = tools.draft_add_offer_part(offer_id=1654, name='CR9E', sku='CR9E', brand='NGK', price=300)
    b = tools.draft_add_offer_part(offer_id=1654, name='OP572', sku='OP572', brand='FILTRON', price=200)
    merged = tools.merge_part_drafts([a, b])
    new = tools.drop_draft_position(merged['token'], number=1)['draft']
    assert new['token'] != merged['token']
    assert new['check']['items'][0]['sku'] == 'OP572'
    assert [s['method'] for s in new['steps']] == ['offer/require/add', 'offer/order/add_free_order']
    assert new['steps'][1]['payload']['REQUIRE_ID'] == {'$from_step': 0}
    with pytest.raises(ValueError):
        tools.apply_draft(merged['token'])
    tools.drafts.verify(new['token'])
    assert not writes(session)


def test_unknown_brand_forbidden_even_through_direct_or_old_draft(env):
    session, tools, _ = env
    with pytest.raises(ValueError, match='бренд'):
        tools.draft_add_offer_part(offer_id=1654, name='CR9E', sku='CR9E', price=300)
    with pytest.raises(ValueError, match='бренд'):
        tools.draft_add_offer_part(offer_id=1654, name='CR9E', sku='CR9E', brand='NONAME', price=300)
    with pytest.raises(ValueError):
        quick_deal.prepare(tools, (5555, 2, 51), None, None, [{'sku': 'CR9E', 'brand': 'NONAME', 'price': 300, 'qty': 1}])
    assert not parts.known_brand('NO NAME') and not writes(session)


def test_pagination_has_every_quick_item(env):
    _, _, pipe = env
    base = _draft(env)
    base['payload']['articles'] = [{'sku': f'A{n:04}', 'brand': 'MANN', 'price': 1, 'qty': 1} for n in range(18)]
    pages = [drafts_keyboard([base], quick_page=p) for p in range(3)]
    buttons = [b for page in pages for row in page.inline_keyboard for b in row if b.callback_data.startswith('dp:')]
    assert len(buttons) == 19
    assert {int(b.callback_data.rsplit(':', 1)[-1]) for b in buttons} == set(range(1, 20))
    assert any(b.callback_data.startswith('qpg:') for row in pages[0].inline_keyboard for b in row)


def test_text_remove_quick_position_tracks_new_token(env):
    session, tools, pipe = env
    draft = _draft(env)
    q = send(pipe, 'убери 1')
    assert q.drafts and q.drafts[0]['token'] != draft['token']
    assert q.drafts[0]['payload']['work'] is None
    q = send(pipe, 'убери 1')
    assert 'отменён' in q.answer and not tools.drafts.list() and not writes(session)


def test_normal_group_manual_button_requires_brand_and_preserves_price(env, monkeypatch):
    from stocrm_agent import websearch
    session, tools, pipe = env
    pipe._last_offer[1] = 1654
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    q = send(pipe, 'Добавь в сделку 1654 CR9E за 300 и OP572 за 200')
    assert q.choices
    q = send(pipe, 'добавь как указано')
    assert 'бренд' in q.answer.lower() and not q.drafts
    q = send(pipe, 'NGK')
    assert q.choices and not q.drafts
    q = send(pipe, 'FILTRON')
    assert [(x['brand'], x['price']) for x in q.drafts[0]['check']['items']] == [('NGK', 300), ('FILTRON', 200)]
    assert not writes(session)


def test_old_quick_noname_draft_is_blocked_before_any_write(env):
    session, tools, _ = env
    old = _draft(env)
    # Имитация корректно подписанного черновика предыдущей версии.
    payload = dict(old['payload'])
    payload['articles'] = [{**payload['articles'][0], 'brand': 'NONAME'}]
    tools.drafts.mark(old['token'], 'discarded')
    legacy = tools.drafts.create(tool='quick_deal', method='quick_deal', payload=payload,
                                 summary='старый', extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='бренд'):
        tools.apply_draft(legacy['token'])
    assert not writes(session)


def test_telegram_brand_selection_then_delete_work(env, monkeypatch, tmp_path):
    import asyncio
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route
    session, tools, _ = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'ambiguous', 'candidates': [
        {'sku': 'CR9E', 'brand': 'NGK', 'source': 'Росско'}, {'sku': 'CR9E', 'brand': 'DENSO', 'source': 'Профит Лига'}]})
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
                                  Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
    class Fake(BaseSession):
        def __init__(self): super().__init__(); self.methods = []
        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
            if method.__api_method__ == 'sendMessage':
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self): pass
        async def stream_content(self, *a, **kw):
            if False: yield b''
    fake = Fake(); bot.session = fake
    chat = {'id': 19, 'type': 'private'}
    user = {'id': 19, 'is_bot': False, 'first_name': 'Тест'}
    def update(value): return Update.model_validate(value, context={'bot': bot})
    def messages(): return [m for m in fake.methods if m.__api_method__ == 'sendMessage']
    async def run():
        await dp.feed_update(bot, update({'update_id': 1, 'message': {'message_id': 1, 'date': 1, 'chat': chat, 'from': user,
            'text': 'новая быстрая сделка диагностика 1000 рублей и добавь артикул CR9E цена 300'}}))
        choice = next(m for m in messages() if m.reply_markup and any('NGK' in b.text for r in m.reply_markup.inline_keyboard for b in r))
        btn = next(b.callback_data for r in choice.reply_markup.inline_keyboard for b in r if 'NGK' in b.text)
        await dp.feed_update(bot, update({'update_id': 2, 'callback_query': {'id': 'b1', 'from': user, 'chat_instance': 'x', 'data': btn,
            'message': {'message_id': 101, 'date': 1, 'chat': chat, 'text': choice.text}}}))
        ready = next(m for m in messages() if m.reply_markup and any(b.callback_data.startswith('dp:') for r in m.reply_markup.inline_keyboard for b in r))
        delete = next(b.callback_data for r in ready.reply_markup.inline_keyboard for b in r if 'Работа' in b.text)
        await dp.feed_update(bot, update({'update_id': 3, 'callback_query': {'id': 'b2', 'from': user, 'chat_instance': 'x', 'data': delete,
            'message': {'message_id': 102, 'date': 1, 'chat': chat, 'text': ready.text}}}))
        edits = [m for m in fake.methods if m.__api_method__ == 'editMessageText']
        assert any('Убрал' in m.text and 'NGK' in m.text and 'Работа «диагностика»' not in m.text for m in edits)
        assert not writes(session)
        await bot.session.close()
    asyncio.run(run())


def test_unbranded_card_does_not_end_variant_or_history_search(env, monkeypatch):
    from stocrm_agent import rossko, profit_liga
    _, tools, _ = env
    monkeypatch.undo()  # restore real resolver; the following substitutes every external source
    calls = []
    def cards(query=None, limit=30):
        calls.append(query)
        if len(calls) == 1:
            return [{'sku': 'W914/2', 'brand': None, 'name': 'фильтр'}]
        return [{'sku': 'W914/2', 'brand': 'MANN', 'name': 'фильтр'}]
    monkeypatch.setattr(tools, 'product_cards', cards)
    monkeypatch.setattr(tools, 'part_history', lambda **kw: [])
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **kw: [])
    monkeypatch.setattr(rossko, 'search_brands', lambda *a, **kw: {'status': 'unavailable', 'brands': []})
    monkeypatch.setattr(profit_liga, 'search_brands', lambda *a, **kw: {'status': 'unavailable', 'brands': []})
    result = tools.resolve_part(query='W914/2', sku='W914/2', allow_web=False)
    assert len(calls) >= 2 and any(c.get('brand') == 'MANN' for c in result['candidates'])
    assert all(parts.known_brand(c.get('brand')) for c in result['candidates'])


def test_regular_batch_keyboard_pages_cover_every_item():
    draft = {'token': 'draft_abcdef12', 'status': 'pending', 'tool': 'add_offer_part',
             'check': {'kind': 'added_lines', 'items': [{'sku': f'A{i}'} for i in range(19)]}}
    pages = [drafts_keyboard([draft], quick_page=p) for p in range(3)]
    indices = [int(b.callback_data.rsplit(':', 1)[-1]) for page in pages for row in page.inline_keyboard
               for b in row if b.callback_data.startswith('dp:')]
    assert indices == list(range(1, 20))


def test_legacy_part_payload_noname_cannot_be_hidden_by_check_metadata(env):
    session, tools, _ = env
    step1 = {'method': 'offer/require/add', 'payload': {'OFFER_ID': 1654, 'TITLE': 'CR9E', 'BRAND': 'NONAME', 'SKU': 'CR9E'}}
    step2 = {'method': 'offer/order/add_free_order', 'payload': {'REQUIRE_ID': {'$from_step': 0}, 'NAME': 'CR9E',
        'SKU': 'CR9E', 'BRAND': 'NONAME', 'PRICE': 300, 'NEED_MARKUP': False, 'AMOUNT': 1}}
    legacy = tools.drafts.create(tool='add_offer_part', method=step1['method'], payload=step1['payload'],
        steps=[step1, step2], summary='старый', extra={'check': {'kind': 'added_line', 'brand': 'NGK', 'sku': 'CR9E'}})
    with pytest.raises(ValueError, match='бренд'):
        tools.apply_draft(legacy['token'])
    assert not writes(session)
