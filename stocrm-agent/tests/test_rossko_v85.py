"""Только локальные имитации. Настоящий Росско и CRM не вызываются."""
import asyncio
import copy
import xml.etree.ElementTree as ET

import pytest
import requests

from stocrm_agent import rossko, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.partcheck import run_partcheck
from stocrm_agent.tools import Toolset

SOAP = b'''<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body><GetSearchResponse xmlns="https://api.rossko.ru/"><SearchResult><success>true</success><PartsList>
<Part><brand>ABCORP</brand><partnumber>ZZ-99123</partnumber><name>Secret name</name><stocks><stock><price>999999</price><count>1000000</count></stock></stocks><crosses><Part><brand>CROSS</brand><partnumber>ZZ99123</partnumber></Part></crosses></Part>
<Part><brand>OTHERCO</brand><partnumber>ZZ99123</partnumber></Part><Part><brand>ABCORP</brand><partnumber>ZZ99123</partnumber></Part>
<Part><brand>WRONG</brand><partnumber>ZZ991230</partnumber></Part><Part><brand>VENDOR</brand><partnumber>ZZ99123@123</partnumber></Part>
</PartsList></SearchResult></GetSearchResponse></Body></Envelope>'''

class Response:
    def __init__(self, content=SOAP, status=200):
        self.content = content
        self.status_code = status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield self.content

@pytest.fixture
def fake_rossko(monkeypatch):
    calls = []
    monkeypatch.setenv('ROSSKO_ENABLED', '1')
    monkeypatch.setenv('ROSSKO_KEY1', 'secret-key-one')
    monkeypatch.setenv('ROSSKO_KEY2', 'secret-key-two')
    monkeypatch.setenv('ROSSKO_DELIVERY_ID', '000000001')
    monkeypatch.delenv('ROSSKO_ADDRESS_ID', raising=False)
    monkeypatch.setattr(rossko, '_last_request', 0)
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((self.trust_env, url, kwargs))
            return Response()
    monkeypatch.setattr(rossko.requests, 'Session', Session)
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

def test_wire_protocol_is_only_search_exact_fixed_endpoint(fake_rossko):
    result = rossko.search_brands('ZZ99123')
    assert result == {'status': 'ok', 'brands': [{'brand': 'ABCORP', 'sku': 'ZZ99123', 'source': 'Росско'}, {'brand': 'OTHERCO', 'sku': 'ZZ99123', 'source': 'Росско'}]}
    trust, url, kw = fake_rossko[0]
    assert trust is False and url == 'https://api.rossko.ru/service/v2.1/GetSearch'
    assert kw['allow_redirects'] is False and kw['verify'] is True and kw['stream'] is True
    assert kw['headers']['SOAPAction'] == f'"{url}"'
    root = ET.fromstring(kw['data'])
    body = next(iter(root))
    operation = next(iter(body))
    assert rossko._local(operation.tag) == 'getsearch'
    assert [rossko._local(n.tag) for n in operation] == ['key1', 'key2', 'text', 'delivery_id']
    assert next(n for n in operation if rossko._local(n.tag) == 'text').text == 'ZZ99123'
    assert len(fake_rossko) == 1
    assert 'secret' not in str(result) and '999999' not in str(result) and '1000000' not in str(result)

@pytest.mark.parametrize('xml', [b'<!DOCTYPE x><Envelope/>', b'<x/>', SOAP.replace(b'<success>true</success>', b'<success>false</success>'), b'<Envelope>'])
def test_bad_responses_never_claim_not_found(fake_rossko, monkeypatch, xml):
    class Bad:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *a, **kw): return Response(xml)
    monkeypatch.setattr(rossko.requests, 'Session', Bad)
    assert rossko.search_brands('ZZ99123')['status'] == 'unavailable'

def test_empty_is_not_failure_and_cross_is_not_brand(fake_rossko, monkeypatch):
    class Empty:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *a, **kw): return Response(SOAP.replace(b'ZZ99123', b'X0000').replace(b'ZZ-99123', b'X0000'))
    monkeypatch.setattr(rossko.requests, 'Session', Empty)
    assert rossko.search_brands('ZZ99123') == {'status': 'empty', 'brands': []}

def test_disabled_and_missing_config_do_not_call_network(fake_rossko, monkeypatch):
    monkeypatch.setenv('ROSSKO_ENABLED', '0')
    assert rossko.search_brands('ZZ99123')['status'] == 'disabled'
    monkeypatch.setenv('ROSSKO_ENABLED', '1')
    monkeypatch.delenv('ROSSKO_KEY1')
    assert rossko.search_brands('ZZ99123')['status'] == 'unavailable'
    assert not fake_rossko

def test_reject_invalid_article_and_pause(fake_rossko):
    assert rossko.search_brands('ZZ<99123')['status'] == 'skipped'
    assert rossko.search_brands('ZZ99123')['status'] == 'ok'
    assert rossko.search_brands('ZZ99123')['status'] == 'ok'
    assert len(fake_rossko) == 2

def test_network_and_redirect_are_not_followed_or_exposed(fake_rossko, monkeypatch):
    class Bad:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *a, **kw): raise requests.ConnectionError('secret-key-one')
    monkeypatch.setattr(rossko.requests, 'Session', Bad)
    result = rossko.search_brands('ZZ99123')
    assert result['status'] == 'unavailable' and 'secret' not in str(result)
    monkeypatch.setattr(rossko, '_last_request', 0)
    class Redirect(Bad):
        def post(self, *a, **kw): return Response(status=302)
    monkeypatch.setattr(rossko.requests, 'Session', Redirect)
    assert rossko.search_brands('ZZ99123')['status'] == 'unavailable'

def test_resolve_merges_crm_and_supplier_even_if_crm_has_match(env, fake_rossko):
    session, tools, _ = env
    session.data['wms/get_filtered_stocks'].append({'STOCK_ID': 42, 'WAREHOUSE_ID': 1, 'SKU': 'ZZ99123', 'BRAND': 'ABCORP', 'NAME': 'деталь', 'QUANTITY': 5, 'PRICE': 999999})
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['status'] == 'ambiguous'
    assert {c['brand'] for c in result['candidates']} == {'ABCORP', 'OTHERCO'}
    supplier = next(c for c in result['candidates'] if c['brand'] == 'OTHERCO')
    assert supplier['source'] == 'Росско' and not supplier.get('price') and not supplier.get('qty')
    assert result['rossko'] == {'status': 'ok', 'count': 2}
    assert all(x['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for x in session.calls)

def test_choice_adds_only_free_line_with_user_price(env, fake_rossko):
    session, _tools, pipe = env
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1313 товар ZZ99123 за 5000', [], 19))
    assert result.choices and not result.drafts
    chosen = next(c for c in result.choices[0]['candidates'] if c['brand'] == 'OTHERCO')
    result = pipe.resume_choice(result.choices[0], chosen['index'], user_key=19)
    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert draft['check']['price'] == 5000 and draft['check']['brand'] == 'OTHERCO'
    assert [s['method'] for s in draft['steps']] == ['offer/require/add', 'offer/order/add_free_order']
    payload = draft['steps'][-1]['payload']
    assert not ({'SUPPLIER_ID', 'STOCK', 'PRICE_SUPP_HUM', 'WAREHOUSE_ID'} & payload.keys())
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)
    assert len(fake_rossko) == 1

def test_supplier_order_is_blocked_including_legacy_draft(env):
    session, tools, _ = env
    with pytest.raises(ValueError, match='Заказы поставщикам'):
        tools.draft_add_offer_part(offer_id=1313, name='x', sku='ZZ99123', brand='ABCORP', price=5000, source='supplier', supplier_id=1)
    draft = tools.draft_add_offer_part(offer_id=1313, name='x', sku='ZZ99123', brand='ABCORP', price=5000)
    steps = copy.deepcopy(draft['steps'])
    steps[-1]['method'] = 'offer/order/add'
    tools.drafts.mark(draft['token'], 'pending', steps=steps)
    with pytest.raises(ValueError, match='заказу поставщику'):
        tools.apply_draft(draft['token'])
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)

def test_legacy_nested_supplier_in_free_order_is_blocked(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1313, name='x', sku='ZZ99123', brand='ABCORP', price=5000)
    steps = copy.deepcopy(draft['steps'])
    steps[-1]['payload']['DATA'] = {'SUPPLIER_ID': 12}
    tools.drafts.mark(draft['token'], 'pending', steps=steps)
    with pytest.raises(ValueError, match='заказу поставщику'):
        tools.apply_draft(draft['token'])
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)

def test_parts_test_states_disabled(env, monkeypatch):
    monkeypatch.setenv('ROSSKO_ENABLED', '0')
    _, tools, _ = env
    assert any('Росско: поиск отключён' in check.title for check in run_partcheck(tools, 'ZZ99123'))


def test_apply_selected_rossko_brand_only_free_order_and_no_repeat_search(env, fake_rossko):
    session, tools, pipe = env
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1313 товар ZZ99123 за 5000', [], 19))
    chosen = next(c for c in result.choices[0]['candidates'] if c['brand'] == 'OTHERCO')
    result = pipe.resume_choice(result.choices[0], chosen['index'], user_key=19)
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['OFFER_ID'] = 1313
        return response
    session._add_order = add
    result = tools.apply_draft(result.drafts[0]['token'])
    assert result['verified']
    writes = [c for c in session.calls if c['method'] in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order')]
    assert [c['method'] for c in writes] == ['offer/require/add', 'offer/order/add_free_order']
    assert not ({'SUPPLIER_ID', 'WAREHOUSE_ID', 'STOCK', 'PRICE_SUPP_HUM'} & writes[-1]['payload'].keys())
    assert len(fake_rossko) == 1


def test_legacy_fallback_and_execution_guard_blocked(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1313, name='x', sku='ZZ99123', brand='ABCORP', price=5000)
    steps = copy.deepcopy(draft['steps'])
    steps[-1]['fallback'] = {'method': 'offer/order/add', 'payload': {}}
    tools.drafts.mark(draft['token'], 'pending', steps=steps)
    with pytest.raises(ValueError, match='заказу поставщику'):
        tools.apply_draft(draft['token'])
    with pytest.raises(ValueError, match='Заказ поставщику'):
        tools._call_step({'method': 'offer/order/add'}, {})
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)


def test_manual_add_legacy_method_refused(env):
    session, tools, _ = env
    tools.manual_order_method = 'offer/order/add'
    with pytest.raises(ValueError, match='Заказы поставщикам'):
        tools.draft_add_offer_part(offer_id=1313, name='x', sku='ZZ99123', brand='ABCORP', price=5000)
    assert all(c['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for c in session.calls)


def test_explicit_brand_never_replaced(env, fake_rossko):
    _, tools, _ = env
    found = tools.resolve_part(query='ZZ99123', sku='ZZ99123', brand='OTHERCO', allow_web=False)
    assert {x['brand'] for x in found['candidates']} == {'OTHERCO'}
    assert len(fake_rossko) == 1


def test_client_defense_blocks_direct_api_calls(env):
    session, tools, _ = env
    with pytest.raises(ValueError, match='запрос не отправлен'):
        tools.client.call('offer/order/add', {'DATA': {}})
    with pytest.raises(ValueError, match='запрос не отправлен'):
        tools.client.call_get('offer/order/add', {})
    with pytest.raises(ValueError, match='запрос не отправлен'):
        tools.client.call('offer/order/add_free_order', {'DATA': {'SUPPLIER_ID': 1}})
    assert not session.calls


def test_mock_mode_never_contacts_rossko(fake_rossko, monkeypatch):
    monkeypatch.setenv('STOCRM_MOCK', '1')
    assert rossko.search_brands('ZZ99123')['status'] == 'disabled'
    assert not fake_rossko
