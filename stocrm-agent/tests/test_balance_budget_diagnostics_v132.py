"""Регрессии дефицита времени и расширения журнала. Только локальные имитации."""
import json
import time

import pytest

from stocrm_agent.bot.fastpath import try_answer, with_offer_balances
from stocrm_agent.client import StocrmError, _READ_DEADLINE, read_budget, read_budget_remaining_ms
from stocrm_agent.diagnostic_journal import DiagnosticJournal, redact_detail, write_text_log
from stocrm_agent.diagnostics import TRACE_ID
from test_status_lists_v99 import env  # noqa: F401


def records(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding='utf-8').splitlines()]


def test_exhausted_search_budget_does_not_starve_balance_reads(env, monkeypatch, tmp_path):
    session, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state')
    tools.client.diagnostic = journal
    samples = [{'offer_id': 1300+i, 'board_id': 2, 'status': 'Запись',
                'sum_known': True, 'sum': 1000.0} for i in range(6)]
    remaining = []
    def find(**kw):
        if kw['page'] == 1: return samples
        # Разыгрываем исчерпание лимита поиска без реального ожидания 20 секунд.
        _READ_DEADLINE.set(time.monotonic() - 1)
        raise StocrmError('Время поиска ограничено; поиск не завершён')
    def balance(oid, total):
        remaining.append(read_budget_remaining_ms())
        return 100.0 + oid - 1300
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    marker = TRACE_ID.set('req_012345abcdef')
    try:
        answer, ids = try_answer('Покажи сделки в записи', tools)
    finally:
        TRACE_ID.reset(marker)
    assert ids == [1300+i for i in range(6)]
    assert len(remaining) == 6 and min(remaining) > 10000
    assert 'долг: 100 ₽' in answer and 'долг: 105 ₽' in answer
    assert 'может быть неполным' in answer
    data = records(journal)
    search = next(e for e in data if e['event'] == 'status.list.search')
    assert search['reason'] == 'page_error' and search['complete'] is False
    assert search['seen_count'] == 6 and search['trace_id'] == 'req_012345abcdef'
    assert sum(e['event'] == 'balance.list.item' and e['reason'] == 'ok' for e in data) == 6
    assert next(e for e in data if e['event'] == 'balance.list.finish')['verified_count'] == 6
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_an_existing_outer_deadline_is_not_silently_bypassed(env, monkeypatch):
    _, tools, _ = env
    captured = []
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: ([{'offer_id': 1301, 'board_id': 2,
        'status': 'Запись', 'sum_known': True, 'sum': 500}] if kw['page'] == 1 else []))
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda oid, total: captured.append(read_budget_remaining_ms()) or 100)
    with read_budget(1.0):
        answer, _ = try_answer('Покажи сделки в записи', tools)
    assert captured and 0 <= captured[0] <= 1000
    assert 'долг: 100 ₽' in answer


@pytest.mark.parametrize('message', ['Покажи сделки в записи', 'Покажи сделки в заказе',
    'Покажи сделки в просчете', 'Покажи сделки в работе', 'Покажи собранные заказы',
    'Покажи сделки в статусе Просчёт двигателя', 'Покажи ожидание оплаты'])
def test_every_status_uses_the_same_independent_balance_budget(env, message, monkeypatch):
    session, tools, _ = env
    session.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Ожидает оплаты', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2100, 'BOARD_ID': 2, 'STATUS_ID': 51,
        'STATUS_NAME': 'Ожидает оплаты', 'OFFER_SUM': 100})
    seen = []
    def balance(oid, total):
        seen.append((oid, read_budget_remaining_ms()))
        return 7.25
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    answer, ids = try_answer(message, tools)
    assert ids and {o for o, _ in seen} == set(ids)
    assert all(t is not None and t > 10000 for _, t in seen)
    assert 'долг: 7,25 ₽' in answer


def test_balance_unknown_zero_and_errors_are_distinct_and_logged(env, tmp_path, monkeypatch):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state')
    tools.client.diagnostic = journal
    def balance(oid, total):
        if oid == 2: raise StocrmError('Время поиска ограничено')
        return 0 if oid == 1 else None
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    result = with_offer_balances(tools, [
        {'offer_id': 1, 'sum_known': True, 'sum': 0},
        {'offer_id': 2, 'sum_known': True, 'sum': 100},
        {'offer_id': 3, 'sum_known': True, 'sum': 100},
        {'offer_id': 4, 'sum_known': False, 'sum': None},
    ])
    assert [x['debt'] for x in result] == [0, None, None, None]
    events = [e for e in records(journal) if e['event'] == 'balance.list.item']
    assert {e['offer_id']: e['reason'] for e in events} == {1:'ok', 2:'read_error', 3:'balance_unusable', 4:'sum_missing'}
    assert next(e for e in events if e['offer_id']==2)['error_kind'] == 'timeout'
    finish = next(e for e in records(journal) if e['event'] == 'balance.list.finish')
    assert finish['verified_count'] == 1 and finish['failed_count'] == 3


def unnamed_balance(oid):
    return {kind: {'CURRENCY_VALUE': 0, 'CURRENCY_NAME': '', 'CURRENCY_TYPE': kind,
                   'ENTITY_ID': oid, 'ENTITY_TYPE': 'OFFER'}
            for kind in ('CASH', 'BANK_CARD', 'BLACK_CASH', 'BLACK_BANK_CARD', 'CASHLESS', 'SBP')}


@pytest.mark.parametrize('variant,reason', [('good','empty_currency_rub'), ('missing','missing_fields'),
    ('wrong_offer','entity_mismatch'), ('wrong_currency','currency_name'), ('wrong_type','payment_type'),
    ('wrong_value','invalid_value'), ('extra','extra_fields'), ('mixed','mixed_currency'), ('strict','currency_strict')])
def test_balance_schema_diagnostic_reason_not_invented(env, tmp_path, monkeypatch, variant, reason):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    balance = unnamed_balance(1234)
    if variant == 'missing': balance.pop('SBP')
    if variant == 'wrong_offer': balance['CASH']['ENTITY_ID'] = 4321
    if variant == 'wrong_currency': balance['CASH']['CURRENCY_NAME'] = 'USD'
    if variant == 'wrong_type': balance['CASH']['CURRENCY_TYPE'] = 'BANK_CARD'
    if variant == 'wrong_value': balance['CASH']['CURRENCY_VALUE'] = None
    if variant == 'extra': balance['SURPRISE'] = {}
    if variant == 'mixed': balance['RUB_CASH'] = {'CURRENCY_VALUE': 0}
    if variant == 'strict': monkeypatch.setenv('STOCRM_BALANCE_EMPTY_CURRENCY', 'STRICT')
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: balance)
    debt = tools.offer_balance_for_list(1234, 1000)
    assert debt == (1000 if variant == 'good' else None)
    event = next(e for e in records(journal) if e['event'] == 'balance.validate')
    assert event['offer_id'] == 1234 and event['reason'] == reason


def test_raw_status_ids_are_preserved_without_unmasking_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_SID', 'private-session-123')
    cleaned, truncated = redact_detail({'STATUS_ID': '41', 'OFFER_STATUS_ID': 70,
        'SID': 'private-session-123', 'SESSION_ID': 'other-private', 'PASSWORD': 'private',
        'nested': {'STATUS_ID': 'not-an-id-secret', 'api_key': 'key-here'}}, {'private-session-123'})
    assert not truncated and cleaned['STATUS_ID'] == '41' and cleaned['OFFER_STATUS_ID'] == 70
    assert cleaned['SID'] == cleaned['SESSION_ID'] == cleaned['PASSWORD'] == '[СКРЫТО]'
    assert cleaned['nested']['STATUS_ID'] == '[некорректный ID скрыт]'
    journal = DiagnosticJournal(tmp_path/'state')
    journal.emit('crm.http', method='offers/get_from_filter', response=cleaned)
    output = tmp_path/'support_log.txt'; write_text_log(journal.state, output)
    text = output.read_text(encoding='utf-8-sig')
    for secret in ('private-session-123', 'other-private', 'not-an-id-secret', 'key-here'):
        assert secret not in text
    response = next(e for e in records(journal) if e['event'] == 'crm.http')['response_data']['value']
    assert response['STATUS_ID'] == '41' and response['OFFER_STATUS_ID'] == 70


def test_larger_response_keeps_many_rows_and_still_masks_keys(tmp_path):
    journal = DiagnosticJournal(tmp_path/'state')
    rows = [{'OFFER_ID': i+1000, 'STATUS_ID': str(i+1), 'OFFER_SUM': '125.00',
             'NAME': 'Тестовая запись', 'TOKEN': 'secret-field'} for i in range(100)]
    journal.emit('crm.http', method='offers/get_from_filter', response={'DATA': rows})
    record = records(journal)[0]
    assert not record['response_data']['truncated']
    data = record['response_data']['value']['DATA']
    assert len(data) == 100 and data[-1]['OFFER_ID'] == 1099 and data[-1]['STATUS_ID'] == '100'
    assert all(r['TOKEN'] == '[СКРЫТО]' for r in data)
    assert record['response_shape']['fields']['OFFER_SUM']['count'] == 100
    out = tmp_path/'log.txt'; write_text_log(journal.state, out)
    assert 'secret-field' not in out.read_text(encoding='utf-8-sig')


def test_budget_metadata_added_to_client_calls(env, tmp_path):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    with read_budget(5): tools.client.call('boards/get_all', {}, idempotent=True)
    ev = records(journal)
    start = next(e for e in ev if e['event'] == 'crm.call.start')
    finish = next(e for e in ev if e['event'] == 'crm.call.finish')
    assert 0 < start['budget_ms'] <= 5000 and 0 <= finish['budget_after_ms'] <= start['budget_ms']


def test_model_tool_arguments_and_results_are_logged_with_redaction(env, tmp_path, monkeypatch):
    import asyncio
    from test_status_lists_v99 import send
    _, tools, pipe = env
    monkeypatch.setenv('STOCRM_SID', 'super-secret-session-value')
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    class Model:
        def __init__(self): self.calls = 0
        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {'tool_calls': [{'id': 'x', 'function': {'name': 'find_offers',
                    'arguments': json.dumps({'limit': 10, 'token': 'different-token-value', 'sid': 'super-secret-session-value'})}}]}
            return {'content': 'Запрос завершён.'}
    pipe.fastpath = False; pipe.llm = Model()
    result = send(pipe, 'Модельный запрос без списка')
    ev = records(journal)
    detail = next(e for e in ev if e['event'] == 'bot.tool.detail')
    response = next(e for e in ev if e['event'] == 'bot.tool.result')
    assert detail['tool'] == response['tool'] == 'find_offers'
    assert detail['request_data']['value']['limit'] == 10
    assert detail['request_data']['value']['token'] == detail['request_data']['value']['sid'] == '[СКРЫТО]'
    assert 'response_data' in response
    output = tmp_path/'log.txt'; write_text_log(journal.state, output)
    text = output.read_text(encoding='utf-8-sig')
    assert 'different-token-value' not in text and 'super-secret-session-value' not in text
    assert 'Модельный запрос без списка' not in text
    assert not result.drafts and not result.applied


def test_unstructured_tool_output_never_goes_into_detailed_log(env, tmp_path, monkeypatch):
    import asyncio
    from test_status_lists_v99 import send
    from stocrm_agent.bot.llm_tools import ToolOutcome
    _, tools, pipe = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    class Model:
        def __init__(self): self.calls=0
        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {'tool_calls': [{'id': 'x', 'function': {'name': 'boards', 'arguments': '{}'}}]}
            return {'content': 'Готово'}
    pipe.fastpath=False;pipe.llm=Model()
    monkeypatch.setattr(pipe.router, 'execute', lambda *a, **k: ToolOutcome(payload='нестандартный скрытый токен UNSAFE-ABC'))
    send(pipe, 'данные доски')
    raw = journal.path.read_text(encoding='utf8')
    assert 'UNSAFE-ABC' not in raw and 'unstructured_or_truncated' in raw


@pytest.mark.parametrize('mode,reason', [('repeat','repeated_page'), ('row_limit','row_limit'),
                                         ('page_limit','page_limit'), ('unknown','empty_page')])
def test_search_incompleteness_reasons_are_exported(env, tmp_path, monkeypatch, mode, reason):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    def row(ident):
        return {'offer_id': ident, 'board_id': 2, 'status': 'Запись', 'sum_known': True, 'sum': 100}
    def find(**kw):
        page = kw['page']
        if mode == 'repeat': return [row(501)]
        if mode == 'row_limit': return [row(501+i) for i in range(26)]
        if mode == 'page_limit': return [row(500+page)]
        if page == 1: return [row(501), {'offer_id': 0, 'status': 'Запись', 'board_id': 2}]
        return []
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', lambda *args: 0)
    answer, ids = try_answer('Покажи сделки в записи', tools)
    assert 'может быть неполным' in answer
    event = next(e for e in records(journal) if e['event'] == 'status.list.search')
    assert event['reason'] == reason and event['status'] == 'failed'
    if mode == 'unknown':
        page1 = next(e for e in records(journal) if e['event'] == 'status.list.page' and e['page']==1)
        assert page1['unknown_count'] == 1 and page1['matched_count'] == 1
    if mode == 'row_limit': assert len(ids) == 25
    if mode == 'page_limit': assert event['page'] == 20 and event['complete'] is False
    target = tmp_path/'support.txt'; write_text_log(journal.state, target)
    assert f'"reason":"{reason}"' in target.read_text(encoding='utf-8-sig')


def test_expired_external_deadline_leaves_debt_unknown(env, tmp_path, monkeypatch):
    _, tools, _ = env
    journal = DiagnosticJournal(tmp_path/'state'); tools.client.diagnostic = journal
    def find(**kw):
        if kw['page'] == 1:
            time.sleep(.04)
            return [{'offer_id': 1101, 'board_id': 2, 'status': 'Запись', 'sum_known': True, 'sum': 300}]
        raise StocrmError('Время поиска ограничено')
    def balance(*args):
        assert read_budget_remaining_ms() == 0
        raise StocrmError('Время поиска ограничено')
    monkeypatch.setattr(tools, 'find_offers', find)
    monkeypatch.setattr(tools, 'offer_balance_for_list', balance)
    with read_budget(.01):
        answer, ids = try_answer('Покажи сделки в записи', tools)
    assert ids == [1101] and 'долг: не удалось проверить' in answer
    event = next(e for e in records(journal) if e['event'] == 'balance.list.item')
    assert event['reason'] == 'read_error' and event['error_kind'] == 'timeout' and event['budget_ms'] == 0
