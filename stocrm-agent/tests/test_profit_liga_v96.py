"""Проверки на поддельных ответах, без сети и реальной CRM."""
import asyncio
import copy
import json
import logging

import pytest
import requests

from stocrm_agent import profit_liga, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import part_keyboard, render_part_choice
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.envfile import _parse
from stocrm_agent.mock import MockSession
from stocrm_agent.partcheck import run_partcheck
from stocrm_agent.profit_liga_config import ConfigurationError, configure_file, run
from stocrm_agent.tools import Toolset

KEY = 'private-key-never-print-123'
ROWS = [
    {'article': 'ZZ-99123', 'brand': 'ABCORP', 'countProducts': 0, 'price': 9999999},
    {'article': 'zz99123', 'brand': 'OTHERCO', 'countProducts': 4, 'price': 1234567},
    {'article': 'ZZ99123', 'brand': 'ABCORP'},
    {'article': 'ZZ991230', 'brand': 'NOTEXACT'},
    {'article': 'ZZ99123@987', 'brand': 'WRONG'},
]

class Response:
    def __init__(self, content=None, status=200):
        self.content = json.dumps(ROWS).encode() if content is None else content
        self.status_code = status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.content

@pytest.fixture
def fake_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.delenv('STOCRM_MOCK', raising=False)
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '1')
    monkeypatch.setenv('PROFIT_LIGA_KEY', KEY)
    monkeypatch.setenv('ROSSKO_ENABLED', '0')
    monkeypatch.setattr(profit_liga, '_last_request', 0)
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kw):
            calls.append((self.trust_env, url, kw))
            return Response()
    monkeypatch.setattr(profit_liga.requests, 'Session', Session)
    return calls

@pytest.fixture
def env(tmp_path, monkeypatch):
    session = MockSession()
    session.data['offers/get_from_filter'].append({**copy.deepcopy(session.data['offers/get_from_filter'][0]), 'OFFER_ID': 1313})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    tools.PART_WRITE_INTERVAL = 0
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    return session, tools, pipe

def test_fixed_read_only_search_excludes_crosses_and_stock(fake_api):
    result = profit_liga.search_brands('ZZ99123')
    assert result == {'status': 'ok', 'brands': [
        {'brand': 'ABCORP', 'sku': 'ZZ99123', 'source': 'Профит Лига'},
        {'brand': 'OTHERCO', 'sku': 'ZZ99123', 'source': 'Профит Лига'}]}
    trust, url, kw = fake_api[0]
    assert not trust and url == 'https://api.pr-lg.ru/search/products'
    assert kw['params'] == {'secret': KEY, 'article': 'ZZ99123'}
    assert not kw['allow_redirects'] and kw['verify'] and kw['stream']
    assert KEY not in str(result) and '9999999' not in str(result)
    assert len(fake_api) == 1

def test_disabled_mock_missing_key_and_invalid_article_no_network(fake_api, monkeypatch):
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '0')
    assert profit_liga.search_brands('ZZ99123')['status'] == 'disabled'
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '1')
    monkeypatch.setenv('STOCRM_MOCK', '1')
    assert profit_liga.search_brands('ZZ99123')['status'] == 'disabled'
    monkeypatch.delenv('STOCRM_MOCK')
    monkeypatch.delenv('PROFIT_LIGA_KEY')
    assert profit_liga.search_brands('ZZ99123')['status'] == 'unavailable'
    monkeypatch.setenv('PROFIT_LIGA_KEY', KEY)
    assert profit_liga.search_brands('ZZ?123')['status'] == 'skipped'
    assert not fake_api

@pytest.mark.parametrize('content', [b'{"error":"bad key"}', b'not json', b'null', b'"failure"', b'\xff', b'[' + b' ' * 2_000_001])
def test_invalid_or_oversized_never_claim_empty(fake_api, monkeypatch, content):
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *a, **kw): return Response(content)
    monkeypatch.setattr(profit_liga.requests, 'Session', Session)
    assert profit_liga.search_brands('ZZ99123')['status'] == 'unavailable'

def test_empty_and_incomplete_are_distinct(fake_api, monkeypatch):
    response = [b'[]']
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *a, **kw): return Response(response[0])
    monkeypatch.setattr(profit_liga.requests, 'Session', Session)
    assert profit_liga.search_brands('ZZ99123')['status'] == 'empty'
    response[0] = json.dumps([ROWS[0], {'article': 'ZZ99123'}, {'brand': KEY, 'article': 'ZZ99123'}]).encode()
    result = profit_liga.search_brands('ZZ99123')
    assert result['status'] == 'partial' and [b['brand'] for b in result['brands']] == ['ABCORP']
    assert KEY not in str(result)

@pytest.mark.parametrize('failure', [requests.Timeout(KEY), requests.ConnectionError(KEY), requests.exceptions.SSLError(KEY)])
def test_network_errors_do_not_echo_secrets(fake_api, monkeypatch, failure):
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *a, **kw): raise failure
    monkeypatch.setattr(profit_liga.requests, 'Session', Session)
    result = profit_liga.search_brands('ZZ99123')
    assert result['status'] == 'unavailable' and KEY not in str(result)

def test_redirect_and_urllib_debug_never_expose_secret(fake_api, monkeypatch, caplog):
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *a, **kw):
            logging.getLogger('urllib3.connectionpool').debug('GET /search/products?secret=%s&article=x', KEY)
            assert not kw['allow_redirects']
            return Response(status=302)
    monkeypatch.setattr(profit_liga.requests, 'Session', Session)
    with caplog.at_level(logging.DEBUG):
        result = profit_liga.search_brands('ZZ99123')
    assert result['status'] == 'unavailable' and KEY not in caplog.text and KEY not in str(result)

def test_resolve_merges_history_or_stock_and_requires_brand_choice(env, fake_api):
    session, tools, _ = env
    session.data['wms/get_filtered_stocks'].append({'STOCK_ID': 42, 'WAREHOUSE_ID': 1, 'SKU': 'ZZ99123', 'BRAND': 'ABCORP', 'NAME': 'деталь', 'QUANTITY': 5})
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['status'] == 'ambiguous'
    assert {c['brand'] for c in result['candidates']} == {'ABCORP', 'OTHERCO'}
    vendor = next(c for c in result['candidates'] if c['brand'] == 'OTHERCO')
    assert vendor['source'] == 'Профит Лига' and not vendor.get('price') and not vendor.get('qty')
    assert all(x['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for x in session.calls)
    assert len(fake_api) == 1

def test_single_supplier_brand_still_requires_selection(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(profit_liga, 'search_brands', lambda sku: {'status': 'ok', 'brands': [{'brand': 'ABCORP', 'sku': sku, 'source': 'Профит Лига'}]})
    assert tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)['status'] == 'ambiguous'

def test_choice_and_confirmation_use_only_free_line(env, fake_api):
    session, tools, pipe = env
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1313 товар ZZ99123 за 5000', [], 19))
    assert result.choices and not result.drafts
    choice = result.choices[0]
    assert 'Профит Лига' in render_part_choice(choice)
    selected = next(c for c in choice['candidates'] if c['brand'] == 'OTHERCO')
    result = pipe.resume_choice(choice, selected['index'], user_key=19)
    draft = result.drafts[0]
    assert draft['check']['price'] == 5000 and draft['check']['brand'] == 'OTHERCO'
    assert [s['method'] for s in draft['steps']] == ['offer/require/add', 'offer/order/add_free_order']
    assert not ({'SUPPLIER_ID', 'STOCK', 'PRICE_SUPP_HUM', 'WAREHOUSE_ID'} & draft['steps'][-1]['payload'].keys())
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)
    tools.apply_draft(draft['token'])
    assert all(c['method'] != 'offer/order/add' for c in session.calls)

def test_many_brands_have_pages_and_truncation_warning(env, monkeypatch):
    _, tools, pipe = env
    def search(sku):
        return {'status': 'ok', 'brands': [{'brand': f'BRAND{i:03}', 'sku': sku, 'source': 'Профит Лига'} for i in range(125)]}
    monkeypatch.setattr(profit_liga, 'search_brands', search)
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1313 товар ZZ99123 за 5000', [], 19))
    choice = result.choices[0]
    assert len(choice['candidates']) == 100
    assert 'обрезан до 100 из 125' in choice['question']
    assert len(part_keyboard('abc', choice['candidates'], page=1).inline_keyboard) >= 7
    checks = run_partcheck(tools, 'ZZ99123')
    assert any('Показаны первые 100' in c.title for c in checks)
    assert sum(c.title.count('BRAND') for c in checks if 'бренды:' in c.title) == 100

def test_explicit_brand_filtered_exactly(env, fake_api):
    _, tools, _ = env
    found = tools.resolve_part(query='ZZ99123', sku='ZZ99123', brand='OTHERCO', allow_web=False)
    assert {x['brand'] for x in found['candidates']} == {'OTHERCO'}
    assert found['candidates'][0]['sku'] == 'ZZ99123'

def test_failed_source_is_warning_not_absence(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(profit_liga, 'search_brands', lambda sku: {'status': 'unavailable', 'brands': [], 'error': 'нет связи'})
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['profit_liga']['status'] == 'unavailable'
    assert any('отсутствие детали не доказано' in line for line in result['tried'])
    assert any('поиск не выполнен' in c.title and 'Профит Лига' in c.title for c in run_partcheck(tools, 'ZZ99123'))

def test_local_config_preserves_bom_duplicates_and_other_fields(tmp_path):
    path = tmp_path/'.env'
    path.write_bytes(b'\xef\xbb\xbfOTHER=stay\r\nPROFIT_LIGA_KEY=old\r\nPROFIT_LIGA_ENABLED=0\r\nexport PROFIT_LIGA_KEY=duplicate\r\nROSSKO_KEY1=keep\r\n')
    configure_file(path, KEY)
    data = path.read_bytes()
    assert data.startswith(b'\xef\xbb\xbf') and b'OTHER=stay\r\n' in data and b'ROSSKO_KEY1=keep\r\n' in data
    assert data.count(b'PROFIT_LIGA_KEY=') == 1
    parsed = _parse(data.decode('utf-8-sig'))
    assert parsed['PROFIT_LIGA_KEY'] == KEY and parsed['PROFIT_LIGA_ENABLED'] == '1'
    assert not list(tmp_path.glob('.profit-env-*'))

def test_setup_never_displays_key_or_touches_state(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('PROFIT_LIGA_ENABLED', raising=False)
    monkeypatch.delenv('PROFIT_LIGA_KEY', raising=False)
    path = tmp_path/'.env'; path.write_text('SOME=setting\n')
    state = tmp_path/'.state'; state.mkdir(); (state/'drafts').write_text('keep')
    assert run(path=path, ask=lambda _: KEY) == 0
    assert KEY not in capsys.readouterr().out
    assert (state/'drafts').read_text() == 'keep'
    assert run(path=path, ask=lambda _: '') == 0
    monkeypatch.setenv('PROFIT_LIGA_KEY', 'outside')
    before = path.read_bytes()
    assert run(path=path, ask=lambda _: 'ignored') == 1 and path.read_bytes() == before

def test_bad_key_or_missing_file_does_not_change_env(tmp_path):
    path = tmp_path/'.env'; path.write_text('X=1\n')
    with pytest.raises(ConfigurationError): configure_file(path, '\n')
    assert path.read_text() == 'X=1\n'
    with pytest.raises(ConfigurationError): configure_file(tmp_path/'missing', KEY)

def test_choice_on_later_page_still_uses_selected_brand(env, monkeypatch):
    session, _tools, pipe = env
    monkeypatch.setattr(profit_liga, 'search_brands', lambda sku: {'status': 'ok', 'brands': [
        {'brand': f'BRAND{i:03}', 'sku': sku, 'source': 'Профит Лига'} for i in range(60)]})
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1313 товар ZZ99123 за 5000', [], 19))
    choice = result.choices[0]
    selected = next(c for c in choice['candidates'] if c['brand'] == 'BRAND054')
    assert selected['index'] >= 8
    result = pipe.resume_choice(choice, selected['index'], user_key=19)
    assert result.drafts[0]['check']['brand'] == 'BRAND054'
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)
