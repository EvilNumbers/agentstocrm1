"""Только моки CRM и поисковиков; внешних сетевых запросов и записи в CRM нет."""
import asyncio
import json
import threading

import pytest
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession, MockResponse
from stocrm_agent.tools import Toolset
from stocrm_agent import websearch


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text):
    return asyncio.run(pipe.handle_text(text, [], 19)).answer


def row(ident, offer=1654, name=None):
    return {'ORDER_ID': ident, 'OFFER_ID': offer, 'NAME': name or f'Деталь {ident}',
            'BRAND': 'MANN', 'SKU': f'A{ident}', 'QUANTITY_CURRENT': 1, 'PRICE': '200'}


def test_new_and_21st_product_visible_without_repeating_model(env):
    session, tools, pipe = env
    data = session.data['products/get_filtered_orders']['DATA']
    data.extend(row(i) for i in range(200000, 200024))
    assert 'Деталь 200023' in send(pipe, 'покажи подробно сделку 1654')
    # Другая копия бота/человек меняет сделку в CRM после её просмотра.
    data.append(row(200025, name='Новая деталь'))
    answer = send(pipe, 'покажи подробно сделку 1654')
    assert 'Новая деталь' in answer
    assert not any(c['method'] in ('offer/order/add', 'offer/order/add_free_order') for c in session.calls)


def test_filter_ignored_on_first_page_other_deal_found_on_second(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/products/get_filtered_orders'):
            session.calls.append({'method': 'products/get_filtered_orders', 'payload': json})
            page = json['PAGE']
            rows = [row(i, 9999) for i in range(200)] if page == 1 else [row(999, 1654, 'Нужный товар')]
            return MockResponse({'CODE': 200, 'RESPONSE': {'DATA': rows}})
        return original(url, json=json, timeout=timeout)
    session.post = post
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Нужный товар' in answer and 'Деталь 0' not in answer
    assert len([c for c in session.calls if c['method'] == 'products/get_filtered_orders']) == 2


def test_failed_parts_not_mistaken_for_empty(env, monkeypatch):
    _, tools, pipe = env
    monkeypatch.setattr(tools, '_offer_parts_details', lambda _: (_ for _ in ()).throw(ValueError('таймаут')))
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Товары: не удалось проверить' in answer and 'Товары: нет' not in answer
    assert 'таймаут' in answer


def test_partial_second_page_preserved_and_warned(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/products/get_filtered_orders'):
            if json['PAGE'] == 2:
                raise StocrmError('ошибка чтения страницы')
            return MockResponse({'CODE': 200, 'RESPONSE': {'DATA': [row(i, 9999) for i in range(199)] + [row(1000, 1654, 'Первая позиция')]}})
        return original(url, json=json, timeout=timeout)
    session.post = post
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Первая позиция' in answer and 'не удалось полностью прочитать' in answer.lower()


def test_items_without_deal_not_shown_as_owned_or_empty(env):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/products/get_filtered_orders'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'DATA': [{'ORDER_ID': 1, 'NAME': 'Чужая деталь'}]}})
        return original(url, json=json, timeout=timeout)
    session.post = post
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Чужая деталь' not in answer and 'Товары: не удалось проверить' in answer
    assert 'без номера сделки' in answer


def test_balance_and_item_reads_overlap_without_extra_requests(env, monkeypatch):
    _, tools, _ = env
    balance_started = threading.Event()
    items_started = threading.Event()
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID': 1654, 'OFFER_SUM': 0}, 1))
    def fake_read(tool, method, payload=None):
        assert method == 'payment/get_balance'
        balance_started.set()
        assert items_started.wait(2), 'позиции запущены только после баланса'
        return {'RUB_CASH': {'CURRENCY_VALUE': 0, 'ENTITY_ID': 1654, 'ENTITY_TYPE': 'OFFER'}}
    def get_items(_):
        assert balance_started.wait(2)
        items_started.set()
        return [dict(name='Деталь')], None
    monkeypatch.setattr(tools, '_read', fake_read)
    monkeypatch.setattr(tools, '_offer_parts_details', get_items)
    monkeypatch.setattr(tools, 'offer_works', lambda *a, **kw: [])
    card = tools.offer_card(1654, with_items=True)
    assert card['parts'][0]['name'] == 'Деталь' and card['paid'] == 0


def test_search_cache_serves_different_limits_from_single_request(monkeypatch):
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'available', lambda: True)
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    calls = []
    def engine(name, query, **kwargs):
        calls.append(kwargs['limit'])
        return [websearch.Hit(f'MANN ABC123 {n}', f'https://example.test/{n}') for n in range(kwargs['limit'])]
    monkeypatch.setattr(websearch, '_by_engine', engine)
    assert len(websearch.search_batch(['ABC123'], limit=1)) == 1
    assert len(websearch.search_batch(['ABC123'], limit=8)) == 8
    assert calls == [websearch.MAX_HITS]
    websearch.clear_cache()


def test_brave_sends_key_in_header_and_uses_cache(monkeypatch):
    websearch.clear_cache()
    monkeypatch.setenv('WEBSEARCH_API_KEY', 'test-secret-not-real')
    monkeypatch.setattr(websearch, 'provider', lambda: 'brave')
    monkeypatch.setattr(websearch, 'available', lambda: True)
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    calls = []
    def get(url, *, timeout, headers=None):
        calls.append((url, headers))
        return json.dumps({'web': {'results': [{'title': 'MANN ABC123', 'url': 'https://example.test/product'}]}})
    monkeypatch.setattr(websearch, '_get', get)
    assert websearch.search_batch(['ABC123'])[0].title == 'MANN ABC123'
    assert websearch.search_batch(['ABC123'])[0].title == 'MANN ABC123'
    assert len(calls) == 1 and calls[0][1]['X-Subscription-Token'] == 'test-secret-not-real'
    assert 'test-secret-not-real' not in calls[0][0]
    websearch.clear_cache()


def test_partial_write_invalidates_old_card_even_when_later_step_fails(env, monkeypatch):
    _, tools, _ = env
    tools._cached('card:1654:full', lambda: {'parts': []})
    draft = tools.drafts.create(tool='test_write', method='task/create', payload={}, summary='test',
                                steps=[{'method': 'task/create', 'payload': {}}, {'method': 'task/create', 'payload': {}}])
    calls = []
    def call(step, payload, results):
        calls.append(step)
        if len(calls) == 2:
            raise StocrmError('отказ', method='task/create', code=400)
        return {'ID': 1}
    monkeypatch.setattr(tools, '_call_step', call)
    with pytest.raises(StocrmError):
        tools.apply_draft(draft['token'])
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    assert 'card:1654:full' not in tools._cache


def test_duplicate_pages_are_labeled_incomplete(env):
    session, _, pipe = env
    original = session.post
    rows = [row(i, 9999) for i in range(199)] + [row(999, 1654, 'Позиция')]
    def post(url, json=None, timeout=None):
        if url.endswith('/products/get_filtered_orders'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'DATA': rows}})
        return original(url, json=json, timeout=timeout)
    session.post = post
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Позиция' in answer and 'список товаров может быть неполным' in answer


def test_missing_quantity_is_not_invented(env):
    session, _, pipe = env
    session.data['products/get_filtered_orders']['DATA'].append({'ORDER_ID': 222200, 'OFFER_ID': 1654, 'NAME': 'Неизвестное количество', 'PRICE': '100'})
    answer = send(pipe, 'подробно сделка 1654')
    assert 'Неизвестное количество — количество не получено' in answer


def test_after_adding_item_details_show_it_in_same_bot(env):
    session, tools, pipe = env
    before = send(pipe, 'покажи подробно сделку 1654')
    assert 'ABCD1234' not in before
    draft = tools.draft_add_offer_part(offer_id=1654, name='Новый фильтр', sku='ABCD1234', brand='MANN', qty=1, price=777)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result.get('verified') is not False
    answer = send(pipe, 'покажи подробно сделку 1654')
    assert 'ABCD1234' in answer and 'Новый фильтр' in answer
    assert len([c for c in session.calls if c['method'] == 'offer/order/add_free_order']) == 1


def test_search_single_flight_respects_each_callers_limit(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'available', lambda: True)
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    started, release = threading.Event(), threading.Event()
    calls = []
    def engine(name, query, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return [websearch.Hit(f'MANN ABC123 {n}', f'https://example.test/{n}') for n in range(10)]
    monkeypatch.setattr(websearch, '_by_engine', engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        small = pool.submit(websearch.search_batch, ['ABC123'], limit=1)
        assert started.wait(2)
        large = pool.submit(websearch.search_batch, ['ABC123'], limit=8)
        time.sleep(0.02)
        release.set()
        assert len(small.result()) == 1
        assert len(large.result()) == 8
    assert len(calls) == 1
    websearch.clear_cache()


def test_real_telegram_handler_sends_entire_long_deal_list(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message, Update
    from stocrm_agent.bot import app
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route
    session, tools, _ = env
    session.data['products/get_filtered_orders']['DATA'].extend(
        row(i, name=f'Деталь номер {i} ' + 'запчасть ' * 5) for i in range(200000, 200090))
    session.data['products/get_filtered_orders']['DATA'].append(row(200099, name='JSON FILTER последняя деталь'))
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
                                   Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.sent = []
        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == 'sendMessage':
                self.sent.append(method.text)
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self): pass
        async def stream_content(self, *args, **kwargs):
            if False: yield b''
    fake = FakeSession()
    bot.session = fake
    async def run():
        update = Update.model_validate({'update_id': 1, 'message': {'message_id': 1, 'date': 1,
            'chat': {'id': 19, 'type': 'private'}, 'from': {'id': 19, 'is_bot': False, 'first_name': 'Тест'},
            'text': 'покажи подробно сделку 1654'}}, context={'bot': bot})
        await dp.feed_update(bot, update)
        assert len(fake.sent) >= 2
        joined = '\n'.join(fake.sent)
        assert 'JSON FILTER последняя деталь' in joined
        assert 'Деталь номер 200000' in joined
        assert all(len(piece) <= 4096 for piece in fake.sent)
        await bot.session.close()
    asyncio.run(run())


def test_network_error_does_not_suggest_unsafe_retry():
    from stocrm_agent.human import humanize_error
    answer = humanize_error(StocrmError('сетевая ошибка при записи', method='offer/order/add_free_order'))
    assert 'не повторяйте' in answer.lower() and 'нажмите «Применить» ещё раз' not in answer
