"""Чтение после work/add; только MockSession, запись никогда не повторяется."""
import copy
import pytest

from stocrm_agent import quick_deal
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def draft(pipe):
    result = send(pipe, 'Добавь быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300')
    assert result.drafts, result.answer
    assert 'Не меняйте работы сделки одновременно' in result.drafts[0]['summary']
    return result.drafts[0]


def writes_methods(session):
    return [x['method'] for x in writes(session)]


@pytest.mark.parametrize('reply', [None, {}, {'WORKS': []}, True])
def test_missing_response_id_confirms_unique_new_work_and_continues(env, monkeypatch, reply):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        return reply if method == 'work/add' else result
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied', result
    assert 'работа «диагностика»' in result['result']['completed']
    assert 'WORK_ID' not in result['result']
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    assert len([c for c in session.calls if c['method'] == 'work/add']) == 1


def test_multiple_response_ids_are_resolved_only_when_new_id_is_present(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 42, 'OFFER_ID': 1900, 'TITLE': 'Ранее', 'SUM': 100})
    choice = send(pipe, 'Добавь в быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    selected = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        return {'WORKS': [{'WORK_ID': 42}, {'WORK_ID': result['WORK_ID']}]} if method == 'work/add' else result
    monkeypatch.setattr(tools.client, 'call', call)
    assert tools.apply_draft(selected['token'])['status'] == 'applied'
    assert writes_methods(session) == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_no_new_work_stays_partial_with_no_retry(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        if method == 'work/add':
            session.data['work/get_filtered']['DATA'].pop()
            return {}
        return result
    monkeypatch.setattr(tools.client, 'call', call)
    delays = []
    monkeypatch.setattr(quick_deal.time, 'sleep', delays.append)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial' and 'не обнаружена' in result['warning']
    assert delays == [0.5, 1.0, 2.0]
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add']
    with pytest.raises(ValueError):
        tools.apply_draft(selected['token'])
    assert len(writes(session)) == 3


def test_delayed_readback_repeats_reads_only(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original_call, original_read = tools.client.call, tools._read
    def call(method, payload=None, **kw):
        result = original_call(method, payload, **kw)
        return {} if method == 'work/add' else result
    stale = []
    def read(tool, method, payload=None):
        result = original_read(tool, method, payload)
        if tool == 'quick_work_snapshot' and any(c['method'] == 'work/add' for c in writes(session)) and not stale:
            stale.append(True)
            return {'DATA': []}
        return result
    monkeypatch.setattr(tools.client, 'call', call)
    monkeypatch.setattr(tools, '_read', read)
    delays = []
    monkeypatch.setattr(quick_deal.time, 'sleep', delays.append)
    assert tools.apply_draft(selected['token'])['status'] == 'applied'
    assert delays == [0.5]
    assert writes_methods(session).count('work/add') == 1


@pytest.mark.parametrize('mode', ['multiple', 'wrong_title', 'wrong_sum', 'wrong_offer', 'repeated_page'])
def test_unconfirmed_work_blocks_product_and_partial_is_not_retried(env, monkeypatch, mode):
    session, tools, pipe = env
    selected = draft(pipe)
    original_call, original_read = tools.client.call, tools._read
    def call(method, payload=None, **kw):
        result = original_call(method, payload, **kw)
        if method != 'work/add':
            return result
        row = session.data['work/get_filtered']['DATA'][-1]
        if mode == 'multiple':
            session.data['work/get_filtered']['DATA'].append({**row, 'WORK_ID': row['WORK_ID'] + 100})
        elif mode == 'wrong_title':
            row['TITLE'] = 'Другая услуга'
        elif mode == 'wrong_sum':
            row['SUM'] = 10
        elif mode == 'wrong_offer':
            row['OFFER_ID'] = 9999
        return {}
    def read(tool, method, payload=None):
        if mode == 'repeated_page' and tool == 'quick_work_snapshot' and payload['PAGE'] > 1 and any(c['method'] == 'work/add' for c in writes(session)):
            return {'DATA': [copy.deepcopy(session.data['work/get_filtered']['DATA'][-1])]}
        return original_read(tool, method, payload)
    monkeypatch.setattr(tools.client, 'call', call)
    monkeypatch.setattr(tools, '_read', read)
    monkeypatch.setattr(quick_deal.time, 'sleep', lambda _: None)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial', result
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add']
    with pytest.raises(ValueError):
        tools.apply_draft(selected['token'])
    assert not send(pipe, 'Добавь быструю сделку диагностика 1000 рублей').drafts


def test_incomplete_before_snapshot_stops_before_work_add(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools._read
    def read(tool, method, payload=None):
        return {'DATA': [{'OFFER_ID': 1}]} if tool == 'quick_work_snapshot' else original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial'
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change']


def test_network_error_after_work_does_not_retry_or_proceed(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        if method == 'work/add':
            raise TimeoutError('ответ потерян')
        return result
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial'
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_old_v111_pending_draft_is_not_applied_under_new_policy(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='quick_deal', method='quick_deal', payload={'contact_id': 5555,
        'board_id': 2, 'status_id': 51, 'offer_id': None, 'work': {'title': 'Диагностика', 'price': 1000},
        'articles': [], 'confirmation_policy': 'status_name_offer_number_v110'}, summary='Старые условия',
        extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(old['token'])
    assert not writes(session)


def test_work_response_invalid_id_does_not_matter(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        return {'WORK_ID': 0} if method == 'work/add' else result
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied'
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_old_id_from_work_response_is_not_used(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 42, 'OFFER_ID': 1900, 'TITLE': 'Ранее', 'SUM': 100})
    choice = send(pipe, 'Добавь в быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    selected = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        return {'WORK_ID': 42} if method == 'work/add' else result
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied'
    assert writes_methods(session) == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_no_work_id_in_response_or_any_list_row(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 42, 'OFFER_ID': 1900, 'TITLE': 'Старая работа', 'SUM': '500'})
    choice = send(pipe, 'Добавь в быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    selected = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original_call, original_read = tools.client.call, tools._read
    def call(method, payload=None, **kw):
        response = original_call(method, payload, **kw)
        return {} if method == 'work/add' else response
    def read(tool, method, payload=None):
        result = original_read(tool, method, payload)
        if tool == 'quick_work_snapshot':
            result = copy.deepcopy(result)
            for row in result.get('DATA', []):
                row.pop('WORK_ID', None)
        return result
    monkeypatch.setattr(tools.client, 'call', call)
    monkeypatch.setattr(tools, '_read', read)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied', result
    assert 'WORK_ID' not in result['result']
    assert writes_methods(session) == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_preexisting_same_title_and_sum_counts_one_new_row_without_id(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 42, 'OFFER_ID': 1900, 'TITLE': 'Диагностика', 'SUM': '1000.00'})
    choice = send(pipe, 'Добавь в быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    selected = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original = tools._read
    def read(tool, method, payload=None):
        result = original(tool, method, payload)
        if tool == 'quick_work_snapshot':
            result = copy.deepcopy(result)
            for row in result.get('DATA', []):
                row.pop('WORK_ID', None)
        return result
    monkeypatch.setattr(tools, '_read', read)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied', result
    assert writes_methods(session) == ['work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_removed_existing_work_causes_partial(env, monkeypatch):
    session, tools, pipe = env
    existing(session, 1900)
    session.data['work/get_filtered']['DATA'].append({'WORK_ID': 42, 'OFFER_ID': 1900, 'TITLE': 'Ранее', 'SUM': 100})
    choice = send(pipe, 'Добавь в быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').choices[0]
    selected = pipe.quick_read_choice(choice, 1, 0).drafts[0]
    original = tools.client.call
    def call(method, payload=None, **kw):
        result = original(method, payload, **kw)
        if method == 'work/add':
            session.data['work/get_filtered']['DATA'] = [r for r in session.data['work/get_filtered']['DATA'] if r['WORK_ID'] != 42]
        return result
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial' and 'список работ изменился' in result['warning']
    assert writes_methods(session) == ['work/add']


def test_contradictory_deal_number_in_response_stops_chain(env, monkeypatch):
    session, tools, pipe = env
    selected = draft(pipe)
    original = tools.client.call
    def call(method, payload=None, **kw):
        response = original(method, payload, **kw)
        return {'OFFER_ID': 999999} if method == 'work/add' else response
    monkeypatch.setattr(tools.client, 'call', call)
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'partial' and 'другую сделку' in result['warning']
    assert writes_methods(session) == ['offer/new/with_contact', 'offer/status/change', 'work/add']


def test_old_v112_confirmation_is_not_accepted(env):
    session, tools, _ = env
    old = tools.drafts.create(tool='quick_deal', method='quick_deal', payload={'contact_id': 5555,
        'board_id': 2, 'status_id': 51, 'offer_id': None, 'work': {'title': 'Диагностика', 'price': 1000},
        'articles': [], 'confirmation_policy': 'work_diff_confirmation_v112'}, summary='Старые условия',
        extra={'check': {'kind': 'quick_deal'}})
    with pytest.raises(ValueError, match='Старый черновик'):
        tools.apply_draft(old['token'])
    assert not writes(session)
