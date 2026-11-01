"""Регрессии скриншотов; внешняя сеть и рабочая CRM не используются."""
import asyncio
import copy

import pytest

from stocrm_agent import parts, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline, _normalize_deal_slip
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.netproxy import Route
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    template = copy.deepcopy(session.data['offers/get_from_filter'][0])
    for ident in (1325, 1314, 13, 842):
        session.data['offers/get_from_filter'].append({**template, 'OFFER_ID': ident})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    pipe._last_offer[19] = 842
    return session, tools, pipe


def send(pipe, text):
    return asyncio.run(pipe.handle_text(text, [], 19))


def test_voice_service_number_and_two_work_prices(env):
    session, _, pipe = env
    result = send(pipe, 'В сделку 13.25 добавь услугу замена масла ДВС 300 рублей, диагностика и замена катушки 1200.')
    assert not result.choices and len(result.drafts) == 2
    assert [(d['check']['offer_id'], d['check']['title'], d['check']['sum']) for d in result.drafts] == [
        (1325, 'замена масла ДВС', 300.0), (1325, 'диагностика и замена катушки', 1200.0)]
    assert not any(x['method'] in ('work/add', 'offer/require/add', 'offer/order/add_free_order') for x in session.calls)


def test_voice_one_work_not_confused_with_product(env):
    _, _, pipe = env
    result = send(pipe, 'в сделку 13.25, добавь услуга, замена ДВС 300 рублей.')
    assert len(result.drafts) == 1
    assert result.drafts[0]['check']['offer_id'] == 1325
    assert result.drafts[0]['check']['title'] == 'замена ДВС'
    assert result.drafts[0]['check']['sum'] == 300


def test_unknown_mangled_deal_does_not_use_previous(env):
    session, _, pipe = env
    result = send(pipe, 'и ЛК 1325, добавь услугу, замена масла ДВС, 300 рублей.')
    assert not result.drafts and not result.choices and not result.applied
    assert 'повторите' in result.answer.lower()
    assert not any(x['method'] == 'work/add' for x in session.calls)


def test_explicit_deal_in_card_beats_list_context(env):
    _, _, pipe = env
    pipe._lists[19] = [842] * 20
    result = send(pipe, 'Покажи сделку подробно 13.14')
    assert 'Сделка №1314' in result.answer
    assert 'Сделка №842' not in result.answer


def test_article_and_price_normalization_limited_scope():
    text = _normalize_deal_slip('Делку 13.25, добавь товар FC 21.39 за 2.500')
    assert text == 'в сделку 1325, добавь товар FC 2139 за 2.500'
    assert _normalize_deal_slip('Цена 2.500, масло 5.30') == 'Цена 2.500, масло 5.30'


def test_explicit_mixed_service_and_goods_not_interpreted_as_one(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    result = send(pipe, 'в сделку 13.25 добавь услугу замена масла за 300 и товар FC2139 за 2500')
    assert not result.drafts and not result.applied and 'бренд' in result.answer.lower()
    result = send(pipe, 'SUFIX')
    assert len(result.drafts) == 1 and not result.applied
    payload = result.drafts[0]['payload']
    assert payload['offer_id'] == 1325 and payload['work'] == {'title': 'замена масла', 'price': 300}
    assert payload['articles'][0]['sku'] == 'FC2139' and payload['articles'][0]['price'] == 2500
    assert not any(c['method'] in ('work/add', 'offer/require/add', 'offer/order/add_free_order') for c in session.calls)


def test_brand_lookup_separators_and_strict_article():
    hits = [{'title': 'SUFIX FC-2139 катушка зажигания', 'url': 'https://example.test/a'},
            {'title': 'WRONG FC-21390 катушка', 'url': 'https://example.test/b'},
            {'title': 'PILENGA FC-2139 ZEKKERT CH88014E', 'url': 'https://example.test/c'}]
    found = parts.brands_from_hits(hits, 'FC 2139')
    assert [x['brand'] for x in found] == ['SUFIX']
    assert websearch.relevant([websearch.Hit('SUFIX FC-2139', 'https://example.test/a')], ['FC 2139'])
    assert not websearch.relevant([websearch.Hit('SUFIX FC-21390', 'https://example.test/a')], ['FC 2139'])


def test_first_lookup_uses_route_and_sends_real_search(monkeypatch):
    import httpx
    seen = []
    monkeypatch.setattr(websearch.netproxy, 'cached_route', lambda url, **kw: (seen.append(('route', url)) or Route()))
    class Response:
        status_code = 200
        text = 'search response'
        def raise_for_status(self):
            pass
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            seen.append(('search', url))
            return Response()
    monkeypatch.setattr(httpx, 'Client', Client)
    assert websearch._get('https://example.test/search?q=FC2139', timeout=3) == 'search response'
    assert seen == [('route', 'https://example.test/'), ('search', 'https://example.test/search?q=FC2139')]


def test_article_lookup_offers_internet_brand_and_preserves_price(env, monkeypatch):
    session, tools, pipe = env
    calls = []
    def resolve(**kw):
        calls.append(kw)
        return {'status': 'ambiguous', 'candidates': [{'brand': 'SUFIX', 'sku': kw['sku'], 'name': 'Катушка', 'source': 'интернет', 'url': 'https://example.test/a', 'why': 'найдено в интернете'}]}
    monkeypatch.setattr(tools, 'resolve_part', resolve)
    result = send(pipe, 'Делку 13.25, добавь товар FC 21.39 за 2.500')
    assert result.choices and not result.drafts
    choice = result.choices[0]
    assert choice['args']['offer_id'] == 1325
    assert choice['args']['price'] == 2500
    assert parts.sku_exact(choice['args']['sku']) == 'FC2139'
    assert choice['candidates'][0]['brand'] == 'SUFIX'
    assert calls and calls[0].get('allow_web', True) is True
    assert not any(c['method'] == 'offer/order/add_free_order' for c in session.calls)


def test_article_clarification_updates_sku_does_not_cancel(env, monkeypatch):
    session, tools, pipe = env
    calls = []
    def resolve(**kw):
        calls.append(kw)
        return {'status': 'not_found', 'candidates': [], 'tried': []}
    monkeypatch.setattr(tools, 'resolve_part', resolve)
    first = send(pipe, 'Делку 13.25, добавь товар FC 21.39 за 2.500')
    assert first.choices and not first.drafts
    result = send(pipe, 'Это катушка, с артикулом FC-2139.')
    assert result.choices and not result.drafts
    assert 'отменено' not in result.answer.lower()
    assert calls[-1]['sku'] == 'FC-2139'
    assert result.choices[0]['args']['price'] == 2500
    assert not any(c['method'] == 'offer/order/add_free_order' for c in session.calls)


def test_web_lookup_returns_real_hit_as_choice_evidence(env, monkeypatch):
    _, tools, _ = env
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'engines', lambda: ['ddg'])
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'available', lambda: True)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    calls = []
    def engine(name, query, **kwargs):
        calls.append((name, query))
        return [websearch.Hit('SUFIX FC-2139 катушка зажигания', 'https://example.test/product')]
    monkeypatch.setattr(websearch, '_by_engine', engine)
    found = tools.web_lookup_part(sku='FC 2139')
    assert calls and found['status'] == 'ok'
    assert found['brands'][0]['brand'] == 'SUFIX' and found['brands'][0]['url'] == 'https://example.test/product'
    websearch.clear_cache()


def test_full_product_resolution_reaches_web_and_offers_brand(env, monkeypatch):
    session, _, pipe = env
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'engines', lambda: ['ddg'])
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'available', lambda: True)
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    calls = []
    def engine(name, query, **kwargs):
        calls.append(query)
        return [websearch.Hit('SUFIX FC-2139 катушка зажигания', 'https://example.test/product')]
    monkeypatch.setattr(websearch, '_by_engine', engine)
    result = send(pipe, 'Делку 13.25, добавь товар FC 21.39 за 2.500')
    assert calls and result.choices and not result.drafts
    assert any(c['brand'] == 'SUFIX' and c['source'] == 'интернет' for c in result.choices[0]['candidates'])
    assert result.choices[0]['args']['offer_id'] == 1325
    assert not any(c['method'] == 'offer/order/add_free_order' for c in session.calls)
    websearch.clear_cache()
