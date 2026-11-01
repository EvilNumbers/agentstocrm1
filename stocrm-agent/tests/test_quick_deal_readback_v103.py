"""Задержка readback после статуса: только имитация, записи не повторяются."""
import pytest

from stocrm_agent import quick_deal
from test_quick_deal_v100 import env, send, writes  # noqa: F401


def draft_for(pipe):
    return send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]


def test_stale_status_reads_settle_without_repeating_writes(env, monkeypatch):
    session, tools, pipe = env
    draft = draft_for(pipe)
    assert not writes(session)
    original = tools._search_offer_row
    checks = []
    def stale(oid):
        row, count = original(oid)
        if any(c['method'] == 'offer/status/change' for c in writes(session)) and len(checks) < 2:
            checks.append(oid)
            return {**row, 'STATUS_NAME': 'Запись'}, count
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', stale)
    delays = []
    monkeypatch.setattr(quick_deal.time, 'sleep', delays.append)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result['verified']
    assert len(checks) == 2 and delays == []
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


@pytest.mark.parametrize('field, value', [('BOARD_ID', 9), ('STATUS_NAME', 'Запись'), ('STATUS_NAME', None), ('CONTACT_ID', 9999)])
def test_status_contact_or_board_readback_does_not_block_numbered_items(env, monkeypatch, field, value):
    session, tools, pipe = env
    draft = draft_for(pipe)
    original = tools._search_offer_row
    def changed(oid):
        row, count = original(oid)
        if any(c['method'] == 'offer/status/change' for c in writes(session)):
            return {**row, field: value}, count
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', changed)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied'
    assert result['result']['status_not_read_back'] is True
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_missing_deal_number_after_status_stays_partial_and_blocks_writes(env, monkeypatch):
    session, tools, pipe = env
    draft = draft_for(pipe)
    original = tools._search_offer_row
    def missing(oid):
        row, count = original(oid)
        return (None, count) if any(c['method'] == 'offer/status/change' for c in writes(session)) else (row, count)
    monkeypatch.setattr(tools, '_search_offer_row', missing)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and not result['verified']
    assert 'сделка не найдена' in result['warning']
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert not send(pipe, 'новая быстрая сделка диагностика 1000 рублей').drafts
    assert len(writes(session)) == 2


def test_no_delay_if_immediate_readback_matches(env, monkeypatch):
    session, tools, pipe = env
    draft = draft_for(pipe)
    delays = []
    monkeypatch.setattr(quick_deal.time, 'sleep', delays.append)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and delays == []
    assert len(writes(session)) == 5


def test_newly_created_deal_can_move_status_and_still_complete(env, monkeypatch):
    session, tools, pipe = env
    draft = draft_for(pipe)
    original = tools.client.call
    def moved(method, payload=None, **kwargs):
        response = original(method, payload, **kwargs)
        if method == 'work/add':
            session.data['offers/get_from_filter'][-1]['STATUS_ID'] = 15
        return response
    monkeypatch.setattr(tools.client, 'call', moved)
    delays = []
    monkeypatch.setattr(quick_deal.time, 'sleep', delays.append)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and delays == []
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
