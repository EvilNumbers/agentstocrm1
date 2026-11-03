"""Параллельные статусные чтения и сохранение проверок; данные искусственные."""
import json
import threading
import time

import pytest

from stocrm_agent.bot.fastpath import try_answer
from stocrm_agent.client import StocrmError, read_budget, read_budget_remaining_ms
from stocrm_agent.diagnostic_journal import DiagnosticJournal, write_text_log
from stocrm_agent.diagnostics import TRACE_ID
from test_status_lists_v99 import env  # noqa: F401


def offer(oid, status='Запись', total=100):
    return {'offer_id': oid, 'board_id': 2, 'status': status, 'sum_known': True, 'sum': total}


def events(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding='utf-8').splitlines()]


def test_balance_is_read_while_next_page_is_loading(env, monkeypatch, tmp_path):
    session, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    page_two_started = threading.Event()
    balance_started = threading.Event()
    calls = []
    def find(**kw):
        calls.append(kw['page'])
        if kw['page'] == 1: return [offer(77701)]
        page_two_started.set()
        assert balance_started.wait(2), 'долг не начали проверять одновременно с поиском'
        return []
    def balance(oid, total):
        balance_started.set()
        assert page_two_started.wait(2), 'следующая страница не читалась параллельно'
        assert oid == 77701 and total == 100
        return 35
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    token = TRACE_ID.set('req_abcdef012345')
    try: answer, ids = try_answer('Покажи сделки в записи', tools)
    finally: TRACE_ID.reset(token)
    assert calls == [1, 2] and ids == [77701]
    assert 'долг: 35 ₽' in answer and 'может быть неполным' not in answer
    data = events(journal)
    check = next(e for e in data if e['event'] == 'balance.list.item')
    assert check['phase'] == 'before_search_end' and check['trace_id'] == 'req_abcdef012345'
    assert next(e for e in data if e['event'] == 'balance.list.finish')['retry_count'] == 0
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)
    out = tmp_path/'log.txt'; write_text_log(journal.state, out)
    assert 'before_search_end' in out.read_text(encoding='utf-8-sig')


def test_early_timeout_is_retried_once_after_search(env, monkeypatch, tmp_path):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    first_checked = threading.Event(); calls = []
    def find(**kw):
        if kw['page'] == 1: return [offer(77702)]
        assert first_checked.wait(2)
        return []
    def balance(oid, total):
        calls.append(read_budget_remaining_ms())
        if len(calls) == 1:
            first_checked.set()
            raise StocrmError('Сетевая ошибка: timeout')
        return 42
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [77702] and 'долг: 42 ₽' in answer and len(calls) == 2
    assert min(calls) > 10000
    checks = [e for e in events(journal) if e['event'] == 'balance.list.item']
    assert [(c['attempt'], c['phase'], c['reason']) for c in checks] == [
        (1, 'before_search_end', 'read_error'), (2, 'after_search_end', 'ok')]
    finish = next(e for e in events(journal) if e['event'] == 'balance.list.finish')
    assert finish['retry_count'] == 1 and finish['verified_count'] == 1 and 'wait_ms' in finish


@pytest.mark.parametrize('failure', ['CRM ограничила частоту запросов', 'Сетевая ошибка: connection refused'])
def test_non_timeout_errors_are_not_retried_or_shown_as_zero(env, monkeypatch, failure):
    _, tools, _ = env
    checked = threading.Event(); calls = []
    def find(**kw):
        if kw['page'] == 1: return [offer(77703)]
        assert checked.wait(2); return []
    def balance(*args):
        calls.append(1); checked.set(); raise StocrmError(failure)
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [77703] and 'долг: не удалось проверить' in answer and len(calls) == 1


def test_only_displayed_confirmed_rows_receive_balance_requests(env, monkeypatch):
    _, tools, _ = env
    visited = []
    rows = [offer(80000+i) for i in range(26)] + [offer(89999, status='Заказ')]
    rows += [{'offer_id': 89998, 'board_id': 2, 'status': None, 'sum_known': True, 'sum': 100}]
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: rows if kw['page'] == 1 else [])
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda oid, total: visited.append(oid) or 0)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [80000+i for i in range(25)] and sorted(visited) == ids
    assert 'может быть неполным' in answer and answer.count('долг: 0 ₽') == 25


def test_concurrency_never_exceeds_four_balance_reads_and_preserves_order(env, monkeypatch):
    _, tools, _ = env
    active = 0; maximum = 0; lock = threading.Lock()
    def balance(oid, total):
        nonlocal active, maximum
        with lock:
            active += 1; maximum = max(active, maximum)
        try:
            time.sleep(.02 if oid % 2 == 0 else .01)
            return oid - 80100
        finally:
            with lock: active -= 1
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: [offer(80100+i) for i in range(8)] if kw['page'] == 1 else [])
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [80100+i for i in range(8)] and 1 < maximum <= 4 and active == 0
    assert all(f'№{oid}' in answer and f'долг: {i} ₽' in answer for i, oid in enumerate(ids))


def test_early_timeout_respects_existing_outer_deadline(env, monkeypatch):
    _, tools, _ = env
    attempts = []
    def find(**kw):
        if kw['page'] == 1: return [offer(77704)]
        time.sleep(.06)
        return []
    def balance(*args):
        attempts.append(read_budget_remaining_ms())
        raise StocrmError('timeout')
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    with read_budget(.04): answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [77704] and 'долг: не удалось проверить' in answer
    assert len(attempts) == 1 and 0 <= attempts[0] <= 40


def test_search_error_keeps_completed_balances_and_partial_warning(env, monkeypatch):
    _, tools, _ = env
    def find(**kw):
        if kw['page'] == 1: return [offer(77705)]
        raise StocrmError('Время поиска ограничено')
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda *args: 12)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [77705] and 'долг: 12 ₽' in answer and 'может быть неполным' in answer


def test_rate_limit_never_retried_even_if_error_mentions_timeout(env, monkeypatch):
    _, tools, _ = env
    checked = threading.Event(); calls = []
    def find(**kw):
        if kw['page'] == 1: return [offer(77706)]
        assert checked.wait(2); return []
    def balance(*args):
        calls.append(1); checked.set(); raise StocrmError('timeout', code=429)
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [77706] and 'долг: не удалось проверить' in answer and len(calls) == 1
