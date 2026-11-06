"""Только локальные имитации; реальные сеть, CRM и Telegram не вызываются."""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import drafts_keyboard
from stocrm_agent.bot.fastpath import try_answer
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_MOCK', '1')
    session = MockSession()
    store = DraftStore(tmp_path/'drafts.json')
    tools = Toolset(StocrmClient('demo', 'demo', session=session), store, AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def work(tools, title='Замена свечей', price=4000):
    return tools.draft_add_offer_work(offer_id=1654, title=title, sum_value=price)


@pytest.mark.parametrize('err', [
    StocrmError('сетевая ошибка: connection reset'),
    StocrmError('internal error unknown field', code=500),
    StocrmError('too many requests', code=429),
    StocrmError('неструктурированный ответ'),
])
def test_uncertain_work_is_never_automatically_repeated(env, monkeypatch, err):
    session, tools, _ = env
    first = work(tools)
    # Второй черновик может быть подготовлен заранее; он тоже должен заблокироваться.
    second = work(tools, 'Другой текст')
    calls = []
    calls_at_write = []
    def send(step, payload, results):
        calls.append(step['method'])
        calls_at_write.append(len(session.calls))
        raise err
    monkeypatch.setattr(tools, '_call_step', send)
    with pytest.raises(StocrmError, match='Результат неизвестен'):
        tools.apply_draft(first['token'])
    saved = tools.drafts.get(first['token'])
    assert saved['status'] == 'partial' and saved['uncertain_network']
    with pytest.raises(ValueError):
        tools.apply_draft(first['token'])
    with pytest.raises(ValueError, match='результат неизвестен'):
        tools.apply_draft(second['token'])
    with pytest.raises(ValueError, match='результат неизвестен'):
        work(tools, 'Замена свечей', price=5000)
    assert calls == ['work/add']
    # Снимок работ читается ДО записи (нужен для сравнения «до/после»), но после
    # неудачной записи нет ни подтверждающего чтения, ни повтора.
    assert calls_at_write and len(session.calls) == calls_at_write[0]


def test_post_lost_after_server_write_is_not_mistaken_for_old_work(env, monkeypatch):
    session, tools, _ = env
    first = work(tools)
    # Имитируем запись на сервере с потерянным ответом; объект на сервере заранее
    # может совпадать с запросом и потому сам по себе ничего не доказывает.
    def uncertain(step, payload, results):
        session.calls.append({'method': 'work/add', 'payload': payload})
        raise StocrmError('сетевая ошибка: ConnectionResetError')
    monkeypatch.setattr(tools, '_call_step', uncertain)
    monkeypatch.setattr(tools, 'offer_works', lambda *a, **kw: [{'title': 'Замена свечей', 'sum': 4000}])
    with pytest.raises(StocrmError): tools.apply_draft(first['token'])
    assert tools.drafts.get(first['token'])['status'] == 'partial'
    with pytest.raises(ValueError): tools.apply_draft(first['token'])
    assert len([c for c in session.calls if c['method'] == 'work/add']) == 1


def test_explicit_400_rejection_can_be_retried(env, monkeypatch):
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: (_ for _ in ()).throw(StocrmError('отклонено', code=400)))
    with pytest.raises(StocrmError): tools.apply_draft(draft['token'])
    state = tools.drafts.get(draft['token'])
    assert state['status'] == 'pending' and not state.get('sent_step_started')


def test_manual_review_and_archive_are_explicit_and_do_not_repeat(env, monkeypatch, tmp_path):
    _, tools, _ = env
    first = work(tools)
    calls = []
    monkeypatch.setattr(tools, '_call_step', lambda *a: (calls.append('write') or (_ for _ in ()).throw(StocrmError('reset'))))
    with pytest.raises(StocrmError): tools.apply_draft(first['token'])
    tools.drafts.archive(first['token'])
    # Блокировка переживает архив и перезапуск.
    new_store = DraftStore(tmp_path/'drafts.json')
    with pytest.raises(ValueError): new_store.assert_work_unblocked({'kind':'added_work','offer_id':1654,'title':'Другая'})
    new_store.resolve_uncertain_manually(first['token'], user_id=100)
    new_store.assert_work_unblocked({'kind':'added_work','offer_id':1654,'title':'Другая'})
    with pytest.raises(ValueError): new_store.claim(first['token'])
    assert calls == ['write']


def test_recent_inflight_cannot_be_manually_unblocked(env):
    _, tools, _ = env
    d = work(tools)
    tools.drafts.claim(d['token'])
    tools.drafts.mark(d['token'], 'applying', sent_step_started=True)
    with pytest.raises(ValueError, match='ещё выполняться'):
        tools.drafts.resolve_uncertain_manually(d['token'], user_id=1)


def test_legacy_pending_after_lost_response_becomes_partial(env):
    _, tools, _ = env
    draft = work(tools)
    tools.drafts.claim(draft['token'])
    tools.drafts.release(draft['token'], reason='связь прервалась, запись не подтверждена')
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError): tools.drafts.claim(draft['token'])
    assert 'wm:' in str(drafts_keyboard([tools.drafts.get(draft['token'])]).model_dump())


def test_crash_after_possible_send_does_not_release(env, monkeypatch):
    _, tools, _ = env
    draft = work(tools)
    def crash(*a): raise KeyboardInterrupt()
    monkeypatch.setattr(tools, '_call_step', crash)
    with pytest.raises(KeyboardInterrupt): tools.apply_draft(draft['token'])
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError): tools.apply_draft(draft['token'])


def test_http_500_does_not_try_fallback_even_if_mentions_unsupported_method(env, monkeypatch):
    _, tools, _ = env
    calls = []
    def call(method, payload):
        calls.append(method)
        raise StocrmError('unknown method; unknown field', code=500)
    monkeypatch.setattr(tools.client, 'call', call)
    with pytest.raises(StocrmError):
        tools._call_step({'method': 'work/add', 'payload': {}, 'fallback': {'method': 'work/other', 'payload': {}}}, {}, [])
    assert calls == ['work/add']


def test_all_card_reads_start_before_slow_identity_finishes(env, monkeypatch):
    _, tools, _ = env
    identity_started = threading.Event()
    release = threading.Event()
    parts_started, works_started, balance_started = threading.Event(), threading.Event(), threading.Event()
    def identity(_):
        identity_started.set()
        assert release.wait(3)
        return {'OFFER_ID':1654, 'OFFER_SUM': 0}, 1
    def read(tool, method, payload=None):
        balance_started.set()
        return {'RUB_CASH': {'CURRENCY_VALUE': 0, 'ENTITY_ID':1654, 'ENTITY_TYPE':'OFFER'}}
    def parts(_):
        parts_started.set()
        return [{'name':'Деталь'}], None
    def works(*a, **kw):
        works_started.set()
        return [{'title':'Работа'}]
    monkeypatch.setattr(tools, '_search_offer_row', identity)
    monkeypatch.setattr(tools, '_read', read)
    monkeypatch.setattr(tools, '_offer_parts_details', parts)
    monkeypatch.setattr(tools, 'offer_works', works)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.offer_card, 1654, with_items=True)
        try:
            assert identity_started.wait(2) and balance_started.wait(2)
            assert parts_started.wait(2) and works_started.wait(2)
        finally:
            release.set()
        card = future.result(3)
    assert card['offer_id'] == 1654 and card['parts'][0]['name'] == 'Деталь' and card['works'][0]['title'] == 'Работа'


def test_unconfirmed_identity_never_displays_parallel_items(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID':9999, 'OFFER_SUM':0}, 1))
    monkeypatch.setattr(tools, '_offer_parts_details', lambda _: ([{'name':'Чужая деталь'}], None))
    card = tools.offer_card(1654, with_items=True)
    assert 'error' in card and 'Чужая деталь' not in str(card)


def test_detail_with_prices_avoids_llm_without_accepting_mutations(env):
    _, tools, pipe = env
    class Model:
        async def chat(self, *a, **kw): pytest.fail('model not needed')
    pipe.llm = Model()
    result = asyncio.run(pipe.handle_text('Покажи подробно сделку 1654 с ценами и услугами', [], 19))
    assert 'Сделка №1654' in result.answer and not result.drafts
    assert try_answer('Покажи подробно сделку 1654 и поменяй цены', tools) is None


def test_failed_card_does_not_restart_via_model(env, monkeypatch):
    _, tools, pipe = env
    monkeypatch.setattr(tools, 'offer_card', lambda *a, **kw: (_ for _ in ()).throw(StocrmError('down')))
    class Model:
        async def chat(self, *a, **kw): pytest.fail('do not start a second search')
    pipe.llm = Model()
    result = asyncio.run(pipe.handle_text('Покажи подробно сделку 1654', [], 19))
    assert 'не удалось' in result.answer.lower() and 'не подтверждено' in result.answer.lower()


def test_success_response_without_id_cannot_confirm_old_service(env, monkeypatch):
    """Ответ без номера и та же строка до/после записи — успех не подтверждается."""
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: {'ok': True})
    monkeypatch.setattr(tools, 'offer_works', lambda *a, **kw: [{'title':'Замена свечей', 'sum':4000}])
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'уже была до записи' in result['warning']
    with pytest.raises(ValueError): work(tools)


def test_existing_success_with_failed_readback_blocks_work_until_review(env, monkeypatch):
    """Снимок до записи удался, после записи чтение упало — ручная проверка, без повтора."""
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: {'WORK_ID': 12345})
    calls = []
    def works(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return []                      # снимок ДО записи: работ ещё нет
        raise StocrmError('network down')  # проверку после записи сделать нельзя
    monkeypatch.setattr(tools, 'offer_works', works)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'перечитать работы' in result['warning']
    assert len(calls) == 2                 # ровно один снимок + одна неудачная проверка
    with pytest.raises(ValueError): work(tools)
    tools.drafts.resolve_uncertain_manually(draft['token'], user_id=1)
    assert work(tools)['status'] == 'pending'


def test_previous_unverified_work_is_migrated_to_manual_review(env):
    _, tools, _ = env
    draft = work(tools)
    tools.drafts.mark(draft['token'], 'applied', check_warning='Не удалось проверить')
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError): work(tools)


def test_lost_response_on_optional_step_stops_following_writes(env, monkeypatch):
    _, tools, _ = env
    steps = [{'method': 'task/create', 'payload': {}},
             {'method': 'task/create', 'payload': {}, 'optional': True},
             {'method': 'task/create', 'payload': {}}]
    draft = tools.drafts.create(tool='test', method='task/create', payload={}, summary='test', steps=steps)
    calls = []
    def call(*args):
        calls.append(1)
        if len(calls) == 2:
            raise StocrmError('connection reset')
        return {'ID': 1}
    monkeypatch.setattr(tools, '_call_step', call)
    with pytest.raises(StocrmError, match='не подтверждён'):
        tools.apply_draft(draft['token'])
    assert len(calls) == 2 and tools.drafts.get(draft['token'])['status'] == 'partial'


def test_work_name_mismatch_does_not_count_as_verified(env, monkeypatch):
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: {'WORK_ID': 777})
    monkeypatch.setattr(tools, 'offer_works', lambda *a, **kw: [{'work_id':777, 'title':'Другая услуга', 'sum':4000}])
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and not result['verified']
    assert 'не видно' in result['warning']


def test_uncertain_events_visible_in_safe_diagnostics(tmp_path):
    import json
    from stocrm_agent.diagnostic_journal import DiagnosticJournal
    journal = DiagnosticJournal(tmp_path/'state')
    journal.audit('write.uncertain', {'method':'work/add', 'tool':'add_offer_work', 'token':'draft_sensitive'})
    journal.audit('bot.uncertain_manual_review', {'token':'draft_sensitive', 'user_id': 55})
    text = journal.path.read_text(encoding='utf-8')
    records = [json.loads(line) for line in text.splitlines()]
    assert [r['event'] for r in records] == ['write.uncertain', 'bot.uncertain_manual_review']
    assert 'draft_sensitive' not in text
