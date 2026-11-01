"""Пошаговая запись после одного решения, статус/partial не обходятся."""
from stocrm_agent import quick_deal
from test_quick_deal_v100 import env, send, writes  # noqa: F401


def test_work_and_goods_are_separate_sequential_calls(env):
    session, tools, pipe = env
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300')
    assert len(result.drafts) == 1 and not writes(session)
    outcome = tools.apply_draft(result.drafts[0]['token'])
    assert outcome['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == [
        'offer/new/with_contact', 'offer/status/change', 'work/add',
        'offer/require/add', 'offer/order/add_free_order']
    assert outcome['result']['completed'] == ['создание сделки', 'запрос перевода в «Быстрые сделки» отправлен, ответ получен; текущий статус не проверялся', 'работа «диагностика»', 'артикул CR9E']


def test_missing_status_after_change_does_not_block_items(env, monkeypatch):
    session, tools, pipe = env
    draft = send(pipe, 'новая быстрая сделка диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    original = tools._search_offer_row
    def missing(oid):
        row, count = original(oid)
        if any(call['method'] == 'offer/status/change' for call in writes(session)):
            row = dict(row)
            row.pop('STATUS_NAME', None)
        return row, count
    monkeypatch.setattr(tools, '_search_offer_row', missing)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied' and result['result']['status_not_read_back']
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add', 'offer/require/add', 'offer/order/add_free_order']


def test_offer_diagnostic_does_not_expose_contact_or_raw_status(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID': 1900, 'CONTACT_ID': 5555,
        'BOARD_ID': 2, 'STATUS_ID': None, 'STATUS_NAME': 'Быстрые сделки', 'SECRET': 'private'}, 1))
    result = tools.offer_identity_diagnostic(1900)
    assert result == {'status': 'ok', 'offer_id_matches': True, 'contact_id_present': True,
                      'board_id': 2, 'status_id': None, 'status_id_shape': 'null',
                      'status_name_present': True,
                      'quick_reference': {'status': 'ok', 'board_id': 2, 'status_id': 51, 'name_matches': True},
                      'without_required_fields': {'status': 'unavailable'}}
    assert '5555' not in str(result) and 'private' not in str(result)


def test_offer_diagnostic_unavailable_does_not_claim_absence(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: (_ for _ in ()).throw(RuntimeError('secret')))
    assert tools.offer_identity_diagnostic(1335) == {'status': 'error'}
