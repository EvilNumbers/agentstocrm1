"""Только имитация CRM: альтернативное чтение не заменяет ID названием."""
import copy

import pytest

from stocrm_agent import quick_deal
from stocrm_agent.bot import quick_read
from stocrm_agent.mock import MockResponse
from test_quick_deal_v100 import env, send, writes  # noqa: F401


def hide_status_with_required_fields(session, monkeypatch):
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('/offers/get_from_filter') and 'REQUIRED_FIELDS' in (json or {}):
            body = copy.deepcopy(response.json())
            rows = body.get('RESPONSE')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        row.pop('STATUS_ID', None)
            return MockResponse(body)
        return response
    monkeypatch.setattr(session, 'post', post)


def test_legacy_read_resolves_id_and_items_write_in_order_after_confirmation(env, monkeypatch):
    session, tools, pipe = env
    hide_status_with_required_fields(session, monkeypatch)
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    assert not writes(session)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result['verified']
    assert [x['method'] for x in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']
    reads = [c for c in session.calls if c['method'] == 'offers/get_from_filter']
    assert any('REQUIRED_FIELDS' not in c['payload'] for c in reads)


@pytest.mark.parametrize('legacy', [None, {}, {'OFFER_ID': 10, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_ID': 51},
    {'OFFER_ID': 1900, 'CONTACT_ID': 42, 'BOARD_ID': 2, 'STATUS_ID': 51},
    {'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 3, 'STATUS_ID': 51},
    {'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_ID': 15},
    {'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_NAME': 'Быстрые сделки'}])
def test_failed_legacy_read_does_not_confirm(env, monkeypatch, legacy):
    session, tools, _ = env
    ids = (5555, 2, 51)
    monkeypatch.setattr(quick_read, 'identity', lambda _: ids)
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2}, 1))
    monkeypatch.setattr(tools, '_search_offer_row_without_required_fields', lambda _: legacy)
    with pytest.raises(ValueError):
        quick_deal._assert_offer(tools, ids, 1900)
    assert not writes(session)


def test_existing_conflicting_or_null_status_never_uses_alternate(env, monkeypatch):
    session, tools, _ = env
    ids = (5555, 2, 51)
    monkeypatch.setattr(quick_read, 'identity', lambda _: ids)
    monkeypatch.setattr(tools, '_search_offer_row_without_required_fields', lambda _: pytest.fail('conflicting status must not be bypassed'))
    for value in (None, 15):
        monkeypatch.setattr(tools, '_search_offer_row', lambda _, v=value: ({'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_ID': v}, 1))
        with pytest.raises(ValueError):
            quick_deal._assert_offer(tools, ids, 1900)
    assert not writes(session)


def test_legacy_lookup_rejects_duplicate_or_other_offers(env, monkeypatch):
    _, tools, _ = env
    same = {'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_ID': 51}
    for rows in ([same, dict(same)], [{**same, 'OFFER_ID': 1901}], []):
        monkeypatch.setattr(tools, '_read_uncached', lambda *args, rows=rows, **kwargs: rows)
        assert tools._search_offer_row_without_required_fields(1900) is None


def test_diagnostic_compares_old_and_new_without_exposing_contact(env, monkeypatch):
    session, tools, _ = env
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1900, 'CONTACT_ID': 5555, 'BOARD_ID': 2, 'STATUS_ID': 51, 'STATUS_NAME': 'Быстрые сделки'})
    hide_status_with_required_fields(session, monkeypatch)
    result = tools.offer_identity_diagnostic(1900)
    assert result['status_id_shape'] == 'поле отсутствует'
    assert result['without_required_fields'] == {'status': 'ok', 'status_id': 51, 'identity_matches': True}
    assert '5555' not in str(result) and not writes(session)
