"""Только моки: поставщики, модель и CRM в сеть не обращаются."""
import asyncio
import json

import pytest

from stocrm_agent import rossko, profit_liga
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot import pipeline as pipeline_module
from stocrm_agent.bot.llm import LLMError
from stocrm_agent.bot.llm_tools import ToolOutcome
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings, LLM_PRESETS
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_MOCK', '1')
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts'), AuditLog(tmp_path/'audit'))
    monkeypatch.setattr(tools, 'product_cards', lambda **kw: [])
    monkeypatch.setattr(tools, 'stock', lambda **kw: [])
    return session, tools


def row(name='Фильтр', brand='MANN', sku='W914/2'):
    return {'sku': sku, 'brand': brand, 'name': name}


def test_priority_exact_and_never_uses_price(setup, monkeypatch):
    _, tools = setup
    calls = []
    monkeypatch.setattr(rossko, 'search_names', lambda *args: (calls.append('r') or {'status': 'ok', 'brands': [row(brand='OTHER'), row(sku='W914/20'), {**row(), 'price': 1, 'qty': 100}]}))
    monkeypatch.setattr(profit_liga, 'search_names', lambda *args: (calls.append('p') or {'status': 'ok', 'brands': [row('Другое')]}))
    result = tools.lookup_part_name('W914/2', 'MANN')
    assert result['name'] == 'Фильтр' and result['source'] == 'Росско' and calls == ['r']
    draft = tools.draft_add_offer_part(offer_id=1654, name='MANN W914/2', sku='W914/2', brand='MANN', price=500)
    assert draft['check']['name'] == 'Фильтр' and draft['check']['name_source'] == 'Росско'
    assert draft['steps'][-1]['payload']['PRICE'] == 500
    assert 'SUPPLIER_ID' not in draft['steps'][-1]['payload'] and 'Росско' in draft['summary']
    assert not any(c['method'] in {'offer/require/add', 'offer/order/add_free_order'} for c in setup[0].calls)


def test_order_fallback_and_partial_never_claims_name(setup, monkeypatch):
    _, tools = setup
    calls = []
    monkeypatch.setattr(rossko, 'search_names', lambda *a: (calls.append('r') or {'status': 'partial', 'brands': [row('Не доказано')]}))
    monkeypatch.setattr(profit_liga, 'search_names', lambda *a: (calls.append('p') or {'status': 'unsupported', 'brands': []}))
    monkeypatch.setattr(tools, 'product_cards', lambda **kw: (calls.append('panel') or [row('Карточка')]))
    result = tools.lookup_part_name('W914/2', 'MANN')
    assert calls == ['r', 'p', 'panel'] and result['name'] == 'Карточка' and result['source'] == 'STOCRM'
    monkeypatch.setattr(tools, 'stock', lambda **kw: (_ for _ in ()).throw(StocrmError('failed')))
    assert tools.lookup_part_name('W914/2', 'MANN')['status'] == 'unverified'


def test_ambiguous_names_are_not_selected_and_no_write(setup, monkeypatch):
    session, tools = setup
    monkeypatch.setattr(rossko, 'search_names', lambda *a: {'status': 'ok', 'brands': [row('Название один'), row('Название два')]})
    result = tools.lookup_part_name('W914/2', 'MANN')
    assert result['status'] == 'ambiguous' and len(result['names']) == 2
    with pytest.raises(ValueError, match='несколько названий'):
        tools.draft_add_offer_part(offer_id=1654, name='W914/2', sku='W914/2', brand='MANN', price=500)
    assert not any(c['method'] in {'offer/require/add', 'offer/order/add_free_order'} for c in session.calls)


def test_user_name_is_not_replaced_and_no_name_is_marked(setup, monkeypatch):
    _, tools = setup
    monkeypatch.setattr(rossko, 'search_names', lambda *a: {'status': 'ok', 'brands': [row('Фильтр')]})
    d = tools.draft_add_offer_part(offer_id=1654, name='Моё название', sku='W914/2', brand='MANN', price=500)
    assert d['check']['name'] == 'Моё название' and 'Фильтр' in d['warning']
    monkeypatch.setattr(rossko, 'search_names', lambda *a: {'status': 'unavailable', 'brands': []})
    d = tools.draft_add_offer_part(offer_id=1654, name='MANN W914/2', sku='W914/2', brand='MANN', price=500)
    assert 'не удалось подтвердить' in d['warning'] and 'name_source' not in d['check']


def soap(parts_xml):
    return ('<Envelope><Body><GetSearchResponse><SearchResult><success>true</success><PartsList>' + parts_xml + '</PartsList></SearchResult></GetSearchResponse></Body></Envelope>').encode()


def test_rossko_name_parser_excludes_crosses_analogs_and_secrets():
    part = lambda b, s, n: f'<Part><brand>{b}</brand><partnumber>{s}</partnumber><name>{n}</name></Part>'
    data = soap(part('MANN', 'W914/2', 'Фильтр') + part('MANN', 'W914/20', 'Аналог') + part('OTHER', 'W914/2', 'Чужое'))
    names, partial = rossko._parse_names(data, 'W914/2', 'MANN', ('secret',))
    assert [n['name'] for n in names] == ['Фильтр'] and not partial
    names, partial = rossko._parse_names(soap(part('MANN', 'W914/2', 'secret')), 'W914/2', 'MANN', ('secret',))
    assert not names and partial
    names, partial = rossko._parse_names(soap(part('MANN', 'W914/2', 'Фильтр') + part('MANN', 'W914/2', '')), 'W914/2', 'MANN', ())
    assert partial and len(names) == 1


def test_profit_liga_does_not_guess_unverified_field(monkeypatch):
    monkeypatch.setattr(profit_liga, 'search_brands', lambda sku: {'status': 'ok', 'brands': [{'brand': 'MANN', 'sku': sku, 'name': 'неподтверждённое поле'}]})
    assert profit_liga.search_names('W914/2', 'MANN') == {'status': 'unsupported', 'brands': []}


def pipe_for(setup, tmp_path, **kwargs):
    pipe = BotPipeline(setup[1], None, None, BotSettings(token='t', policy_path=str(tmp_path/'policy'), **kwargs))
    pipe.fastpath = False
    return pipe


class Failing:
    def __init__(self, retryable=True): self.retryable = retryable
    async def chat(self, *a, **kw):
        err = LLMError('test')
        err.retryable = self.retryable
        err.status_code = 503 if self.retryable else 401
        raise err


def test_groq_switch_on_first_503_with_separate_settings(setup, tmp_path, monkeypatch):
    pipe = pipe_for(setup, tmp_path, llm_provider='google', llm_api_key='google-key', llm_fallback_provider='groq', llm_fallback_api_key='groq-key')
    pipe.llm = Failing()
    calls = []
    class Groq:
        def __init__(self, s): calls.append(s)
        async def chat(self, messages, tools):
            assert messages[-1]['role'] == 'user' and tools
            return {'content': 'Ответ'}
        async def aclose(self): pass
    monkeypatch.setattr(pipeline_module, 'LLMClient', Groq)
    result = asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42))
    assert 'Groq' in result.answer and 'Ответ' in result.answer and len(calls) == 1
    assert calls[0].llm_provider == 'groq' and calls[0].llm_base_url == LLM_PRESETS['groq'][0]
    assert calls[0].llm_api_key == 'groq-key' and calls[0].llm_reasoning_effort == ''
    assert not setup[0].calls


@pytest.mark.parametrize('kwargs, retryable', [({}, True), ({'llm_fallback_provider':'groq', 'llm_fallback_api_key':'x'}, False)])
def test_no_fallback_without_consent_or_for_bad_request(setup, tmp_path, monkeypatch, kwargs, retryable):
    pipe = pipe_for(setup, tmp_path, **kwargs)
    pipe.llm = Failing(retryable)
    monkeypatch.setattr(pipeline_module, 'LLMClient', lambda s: pytest.fail('fallback must not run'))
    assert asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42)).error


def test_no_fallback_or_repeat_after_a_write(setup, tmp_path, monkeypatch):
    pipe = pipe_for(setup, tmp_path, llm_fallback_provider='groq', llm_fallback_api_key='x')
    calls = []
    class Model:
        async def chat(self, *a, **kw):
            if not calls:
                calls.append('llm')
                return {'tool_calls': [{'id': 'a', 'function': {'name': 'draft_add_note', 'arguments': '{}'}}]}
            raise LLMError('503')
    pipe.llm = Model()
    def execute(name, args):
        calls.append('write')
        return ToolOutcome(payload=json.dumps({'ok': True}), applied={'summary': 'запись'})
    monkeypatch.setattr(pipe.router, 'execute', execute)
    monkeypatch.setattr(pipeline_module, 'LLMClient', lambda s: pytest.fail('fallback must not run after a write'))
    result = asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42))
    assert calls == ['llm', 'write'] and result.applied and 'не повторяйте' in result.answer.lower()


def test_real_fast_path_single_and_group_use_priority(setup, tmp_path, monkeypatch):
    session, tools = setup
    calls = []
    def search(sku, brand):
        calls.append((sku, brand))
        return {'status': 'ok', 'brands': [row('Каталожное название ' + sku, brand, sku)]}
    monkeypatch.setattr(rossko, 'search_names', search)
    monkeypatch.setattr(profit_liga, 'search_names', lambda *a: pytest.fail('lower priority not needed'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='t', policy_path=str(tmp_path/'policy')))
    single = asyncio.run(pipe.handle_text('Добавь в сделку 1654 товар MANN W914/2 за 500', [], 42))
    assert single.drafts, single.answer
    assert 'Каталожное название' in single.drafts[0]['summary'] and 'Росско' in single.drafts[0]['summary']
    assert len(calls) == 1
    many = asyncio.run(pipe.handle_text('Добавь в сделку 1654 MANN W914/2 за 500, MANN W712/75 за 600', [], 43))
    assert many.drafts, many.answer
    checks = tools._checks_of(many.drafts[0])
    assert len(checks) == 2 and all(c['name_source'] == 'Росско' for c in checks)
    assert not any(c['method'] in {'offer/require/add', 'offer/order/add_free_order'} for c in session.calls)


def test_fallback_after_read_is_also_stopped(setup, tmp_path, monkeypatch):
    pipe = pipe_for(setup, tmp_path, llm_fallback_provider='groq', llm_fallback_api_key='x')
    calls = []
    class Model:
        async def chat(self, *a, **kw):
            if not calls:
                calls.append('llm')
                return {'tool_calls': [{'id': 'a', 'function': {'name': 'boards', 'arguments': '{}'}}]}
            raise LLMError('503')
    pipe.llm = Model()
    monkeypatch.setattr(pipe.router, 'execute', lambda *a: (calls.append('read') or ToolOutcome(payload='{}')))
    monkeypatch.setattr(pipeline_module, 'LLMClient', lambda s: pytest.fail('no restart after any tool'))
    result = asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42))
    assert calls == ['llm', 'read'] and result.error and 'не повторяйте' in result.error.lower()


def test_settings_fallback_is_opt_in_and_separate(monkeypatch):
    monkeypatch.setenv('STT_API_KEY', 'stt-key')
    monkeypatch.delenv('LLM_FALLBACK_PROVIDER', raising=False)
    monkeypatch.delenv('LLM_FALLBACK_API_KEY', raising=False)
    settings = BotSettings.from_env(require_token=False)
    assert settings.llm_fallback_provider == '' and settings.llm_fallback_api_key == ''
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'groq')
    monkeypatch.setenv('LLM_FALLBACK_API_KEY', 'own-key')
    settings = BotSettings.from_env(require_token=False)
    assert settings.llm_fallback_provider == 'groq' and settings.llm_fallback_api_key == 'own-key'


def test_punctuation_in_names_cannot_hide_ambiguity(setup, monkeypatch):
    _, tools = setup
    monkeypatch.setattr(rossko, 'search_names', lambda *a: {'status': 'ok', 'brands': [row('Фильтр 1/2'), row('Фильтр 1-2')]})
    assert tools.lookup_part_name('W914/2', 'MANN')['status'] == 'ambiguous'


def test_real_primary_client_retries_503_then_switches(setup, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from stocrm_agent.bot import llm as llm_module
    PrimaryClient = llm_module.LLMClient
    pipe = pipe_for(setup, tmp_path, llm_provider='google', llm_api_key='primary',
                    llm_fallback_provider='groq', llm_fallback_api_key='backup')
    pipe.llm = PrimaryClient(pipe.s)
    attempts = []
    class HTTP:
        async def post(self, url, **kwargs):
            attempts.append((url, kwargs['headers']['Authorization']))
            return SimpleNamespace(status_code=503, text='unavailable', headers={})
    async def get_http(proxy): return HTTP()
    async def route(*a, **kw): return SimpleNamespace(proxy=None)
    async def no_sleep(_): pass
    monkeypatch.setattr(pipe.llm, '_http', get_http)
    monkeypatch.setattr(llm_module, 'acached_route', route)
    monkeypatch.setattr(llm_module.asyncio, 'sleep', no_sleep)
    class Backup:
        def __init__(self, settings): assert settings.llm_api_key == 'backup'
        async def chat(self, messages, tools): return {'content': 'Готов ответ'}
        async def aclose(self): pass
    monkeypatch.setattr(pipeline_module, 'LLMClient', Backup)
    result = asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42))
    assert len(attempts) == 3 and all(header == 'Bearer primary' for _, header in attempts)
    assert 'Groq' in result.answer and 'Готов ответ' in result.answer


def test_fallback_failure_is_bounded_and_named(setup, tmp_path, monkeypatch):
    pipe = pipe_for(setup, tmp_path, llm_fallback_provider='groq', llm_fallback_api_key='x')
    pipe.llm = Failing()
    class BadBackup:
        def __init__(self, settings): pass
        async def chat(self, *a, **kw): raise LLMError('not available')
        async def aclose(self): pass
    monkeypatch.setattr(pipeline_module, 'LLMClient', BadBackup)
    result = asyncio.run(pipe.handle_text('Расскажи что-нибудь', [], 42))
    assert 'Groq' in result.error and 'Инструменты не запускались' in result.error


def test_names_checked_after_manual_brand_selection(setup, tmp_path, monkeypatch):
    from stocrm_agent import websearch
    _, tools = setup
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    monkeypatch.setattr(websearch, 'search_batch', lambda *a: [])
    monkeypatch.setattr(rossko, 'search_names', lambda sku, brand: {'status': 'ok', 'brands': [row('Каталожное ' + sku, brand, sku)]})
    pipe = BotPipeline(tools, None, None, BotSettings(token='t', policy_path=str(tmp_path/'policy')))
    result = pipe._start_part_group([{'sku': 'W914/2', 'price': 500}, {'sku': 'W712/75', 'price': 600}], 1654, 42)
    assert result.choices and not result.drafts
    result = pipe._group_reply(result.choices[0], 'MANN', 42)
    assert result.choices and not result.drafts
    result = pipe._group_reply(result.choices[0], 'MANN', 42)
    assert result.drafts, result.answer
    checks = tools._checks_of(result.drafts[0])
    assert len(checks) == 2 and all(c['name_source'] == 'Росско' for c in checks)
    assert 'каталог и применяемость не подтверждены' not in result.drafts[0]['warning']


def test_ambiguous_name_in_actual_fast_path_stops(setup, tmp_path, monkeypatch):
    session, tools = setup
    monkeypatch.setattr(rossko, 'search_names', lambda *a: {'status': 'ok', 'brands': [row('Название один'), row('Название два')]})
    pipe = BotPipeline(tools, None, None, BotSettings(token='t', policy_path=str(tmp_path/'policy')))
    result = asyncio.run(pipe.handle_text('Добавь в сделку 1654 товар MANN W914/2 за 500', [], 42))
    assert not result.drafts and not result.applied and 'несколько названий' in result.answer
    assert not any(c['method'] in {'offer/require/add', 'offer/order/add_free_order'} for c in session.calls)
