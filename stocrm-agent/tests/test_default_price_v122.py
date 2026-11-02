"""Подтверждение нулевой цены: только локальная имитация CRM, без сети."""
import asyncio
import pytest

from stocrm_agent.bot.app import part_keyboard, render_part_choice
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def button(pipe, choice, value='yes', user=1):
    return asyncio.run(pipe.handle_text(f"__price_button__:{choice['_price_nonce']}:{value}", [], user))


def test_single_missing_price_question_zero_and_readback(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    r = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E')
    assert len(r.choices) == 1 and not r.drafts and not writes(session), r.answer
    c = r.choices[0]
    assert c['_price_scope'] == 'single' and '0 ₽' in render_part_choice(c)
    kb = part_keyboard('abc', [], default_price=True)
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [['prt:abc:priceyes', 'prt:abc:priceno'], ['prt:abc:pricecancel']]
    nonce = c['_price_nonce']
    denied = button(pipe, c, user=20)
    assert 'устарел' in denied.answer and not writes(session)
    accepted = button(pipe, c)
    assert len(accepted.drafts) == 1 and not writes(session), accepted.answer
    draft = accepted.drafts[0]
    assert draft['check']['price'] == 0 and '0 ₽' in draft['summary']
    assert draft['steps'][-1]['zero_price_confirmed'] is True
    old = asyncio.run(pipe.handle_text(f'__price_button__:{nonce}:yes', [], 1))
    assert 'устарел' in old.answer and not writes(session)
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'applied' and outcome['verified'], outcome
    order = next(c['payload'] for c in writes(session) if c['method'] == 'offer/order/add_free_order')
    assert order['PRICE'] == 0 and not ({'WAREHOUSE_ID', 'SUPPLIER_ID', 'STOCK_ID'} & order.keys())


def test_zero_rejected_without_choice_and_untrusted_tool_flag(env):
    session, tools, pipe = env
    existing(session, 1900)
    with pytest.raises(ValueError, match='0 ₽'):
        tools.draft_add_offer_part(offer_id=1900, name='CR9E', sku='CR9E', brand='NGK', price=0)
    result = pipe.router.execute('draft_add_offer_part', {'offer_id':1900, 'name':'CR9E', 'sku':'CR9E', 'brand':'NGK', 'price':0, 'allow_zero_price': True, '_part_chosen':True})
    assert not result.draft and not writes(session)


def test_no_asks_manual_price_and_old_button_expires(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    c = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E').choices[0]
    nonce = c['_price_nonce']
    no = button(pipe, c, 'no')
    assert 'Укажите цену' in no.answer and not writes(session)
    stale = asyncio.run(pipe.handle_text(f'__price_button__:{nonce}:yes', [], 1))
    assert 'устарел' in stale.answer
    r = send(pipe, '700')
    assert len(r.drafts) == 1 and r.drafts[0]['check']['price'] == 700 and not writes(session), r.answer


def test_catalog_price_is_not_used_as_sale_price(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    row = {'brand': 'NGK', 'sku': 'CR9E', 'name': 'Свеча', 'price': 500, 'source': 'номенклатура'}
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status':'matched', 'best':row, 'candidates':[row]})
    r = send(pipe, 'Добавь в сделку 1900 артикул CR9E')
    assert r.choices and r.choices[0].get('_default_price') and not r.drafts and not writes(session), r.answer


def test_group_per_item_zero_and_normal_price(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    r = send(pipe, 'Добавь в сделку 1900 артикулы NGK CR9E и MANN W914/2')
    assert r.choices and r.choices[0]['_group_step'] == 'part', r.answer
    r = send(pipe, 'NGK')
    assert r.choices and r.choices[0]['_price_scope'] == 'group', r.answer
    r = button(pipe, r.choices[0])
    assert r.choices and r.choices[0]['_group_step'] == 'part', r.answer
    r = send(pipe, 'MANN')
    assert r.choices and r.choices[0]['_price_scope'] == 'group', r.answer
    r = button(pipe, r.choices[0], 'no')
    r = send(pipe, '700')
    assert len(r.drafts) == 1 and not writes(session), r.answer
    draft = r.drafts[0]
    assert [x['price'] for x in draft['check']['items']] == [0, 700]
    assert '0 ₽' in draft['summary']
    outcome = tools.apply_draft(draft['token'])
    assert outcome['verified'], outcome
    orders = [c['payload'] for c in writes(session) if c['method'] == 'offer/order/add_free_order']
    assert [x['PRICE'] for x in orders] == [0, 700]
    assert all(not ({'WAREHOUSE_ID', 'SUPPLIER_ID', 'STOCK_ID'} & x.keys()) for x in orders)


def test_mixed_work_and_part_zero(env):
    session, tools, pipe = env
    existing(session, 1900)
    r = send(pipe, 'В сделку 1900 добавь услугу диагностика за 500 и артикул NGK CR9E')
    assert r.choices and r.choices[0]['_price_scope'] == 'quick' and not writes(session), r.answer
    r = button(pipe, r.choices[0])
    assert len(r.drafts) == 1 and not writes(session), r.answer
    d = r.drafts[0]
    assert d['payload']['articles'][0]['price'] == 0 and d['payload']['articles'][0]['zero_price_confirmed'] is True
    assert '0 ₽' in d['summary']
    result = tools.apply_draft(d['token'])
    assert result['status'] == 'applied', result
    assert [c['method'] for c in writes(session)] == ['work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert writes(session)[-1]['payload']['PRICE'] == 0


def test_wrong_zero_or_missing_price_readback_not_verified(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    c = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E').choices[0]
    d = button(pipe, c).drafts[0]
    original = session.post
    def wrong(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('offer/order/add_free_order'):
            session.data['products/get_filtered_orders']['DATA'][-1]['PRICE'] = 77
        return response
    monkeypatch.setattr(session, 'post', wrong)
    result = tools.apply_draft(d['token'])
    assert not result.get('verified') and ('77' in str(result) or '77' in str(tools.drafts.get(d['token'])))


def test_text_yes_and_final_confirmation_even_when_auto_enabled(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    pipe.router.policy.set_confirm_all(False)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    r = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E')
    assert r.choices and not writes(session)
    r = send(pipe, 'Да')
    assert len(r.drafts) == 1 and not r.applied and not writes(session), r.answer
    assert r.drafts[0]['check']['price'] == 0


def test_cancel_no_and_superseded_choice(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    c = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E').choices[0]
    nonce = c['_price_nonce']
    cancel = button(pipe, c, 'cancel')
    assert 'Отменено' in cancel.answer and not writes(session)
    assert 'устарел' in send(pipe, f'__price_button__:{nonce}:yes').answer
    c = send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E').choices[0]
    nonce = c['_price_nonce']
    other = send(pipe, 'В сделку 1900 добавь услугу диагностика за 500 и артикул NGK CR9E за 700')
    assert other.drafts and not writes(session), other.answer
    assert 'устарел' in send(pipe, f'__price_button__:{nonce}:yes').answer


@pytest.mark.parametrize('actual', [None, '1 000,00', 'broken'])
def test_zero_readback_missing_or_different_is_unverified(env, monkeypatch, actual):
    session, tools, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    d = button(pipe, send(pipe, 'Добавь в сделку 1900 артикул NGK CR9E').choices[0]).drafts[0]
    original = session.post
    def wrong(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('offer/order/add_free_order'):
            session.data['products/get_filtered_orders']['DATA'][-1]['PRICE'] = actual
        return response
    monkeypatch.setattr(session, 'post', wrong)
    result = tools.apply_draft(d['token'])
    assert not result.get('verified'), result
    assert tools.drafts.get(d['token'])['status'] == 'partial'


def test_group_zero_always_waits_for_final_confirmation(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    pipe.router.policy.set_confirm_all(False)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    r = send(pipe, 'Добавь в сделку 1900 артикулы NGK CR9E и MANN W914/2')
    r = send(pipe, 'NGK')
    r = button(pipe, r.choices[0])
    r = send(pipe, 'MANN')
    r = send(pipe, '700')
    assert r.drafts and not r.applied and not writes(session), r.answer


def test_telegram_real_dispatcher_price_buttons(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    existing(session, 1900)
    monkeypatch.setattr(tools, 'stock_by_sku_variants', lambda *a, **k: [])
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path / 'access.json'))
    settings = BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678', policy_path=str(tmp_path/'policy.json'))
    bot, dp, _, _ = app.create_app(settings, Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))

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
    user = {'id':19, 'is_bot':False, 'first_name':'Тест'}
    chat = {'id':19, 'type':'private'}
    def update(data):
        return Update.model_validate(data, context={'bot':bot})
    def messages():
        return [m for m in fake.methods if m.__api_method__ == 'sendMessage']
    async def run():
        await dp.feed_update(bot, update({'update_id':1, 'message':{'message_id':1,'date':1,'chat':chat,'from':user,
                          'text':'Добавь в сделку 1900 артикул NGK CR9E'}}))
        msg = next(m for m in messages() if m.reply_markup and any(b.callback_data.endswith(':priceyes') for row in m.reply_markup.inline_keyboard for b in row))
        assert '0 ₽' in msg.text and not writes(session)
        callback = next(b.callback_data for row in msg.reply_markup.inline_keyboard for b in row if b.callback_data.endswith(':priceyes'))
        await dp.feed_update(bot, update({'update_id':2,'callback_query':{'id':'p1','from':user,'chat_instance':'x','data':callback,
                            'message':{'message_id':101,'date':1,'chat':chat,'text':msg.text}}}))
        assert not writes(session)
        ready = next(m for m in messages() if m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row))
        assert '0 ₽' in ready.text and 'CR9E' in ready.text
        await dp.feed_update(bot, update({'update_id':3,'callback_query':{'id':'p2','from':user,'chat_instance':'x','data':callback,
                            'message':{'message_id':101,'date':1,'chat':chat,'text':msg.text}}}))
        assert not writes(session)
        assert len([m for m in messages() if m.reply_markup and any(b.callback_data.startswith('ap:') for row in m.reply_markup.inline_keyboard for b in row)]) == 1
    asyncio.run(run())


def test_llm_cannot_invent_price_or_confirm_zero(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    pipe.fastpath = False
    monkeypatch.setattr(pipe, '_start_prefetch', lambda text: None)
    row = {'brand': 'NGK', 'sku':'CR9E', 'name':'Свеча', 'price':500, 'source':'номенклатура'}
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status':'matched', 'best':row, 'candidates':[row]})
    class Model:
        async def chat(self, *args, **kwargs):
            return {'tool_calls':[{'id':'call1','function':{'name':'draft_add_offer_part', 'arguments':
                '{"offer_id":1900,"name":"CR9E","sku":"CR9E","brand":"NGK","price":500,"allow_zero_price":true}'}}]}
    pipe.llm = Model()
    r = send(pipe, 'Добавь в сделку 1900 артикул CR9E')
    assert r.choices and r.choices[0].get('_default_price') and not r.drafts and not writes(session), r.answer
