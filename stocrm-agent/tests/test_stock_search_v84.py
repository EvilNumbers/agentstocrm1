"""Склад — справочный поиск; сценарии записи работают только на MockSession."""
import asyncio
import copy

import pytest

from stocrm_agent import parts, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import render_part_choice
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.partcheck import run_partcheck
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path, monkeypatch):
    session = MockSession()
    template = copy.deepcopy(session.data['offers/get_from_filter'][0])
    session.data['offers/get_from_filter'].append({**template, 'OFFER_ID': 1313})
    rows = session.data['wms/get_filtered_stocks']
    rows.extend([
        {'STOCK_ID': 611, 'WAREHOUSE_ID': 7, 'SKU': 'ZZ99123', 'BRAND': 'ABCORP', 'NAME': 'Подшипник', 'QUANTITY': 1_000_000, 'PRICE': 111, 'PRICE_W_MARKUP': 222},
        {'STOCK_ID': 612, 'WAREHOUSE_ID': 7, 'SKU': 'ZZ99123', 'BRAND': 'OTHERCO', 'NAME': 'Подшипник другой бренд', 'QUANTITY': 3, 'PRICE': 333},
        {'STOCK_ID': 613, 'WAREHOUSE_ID': 8, 'SKU': 'ZZ99123', 'BRAND': 'ABCORP', 'NAME': 'Подшипник', 'QUANTITY': 2, 'PRICE': 111},
        {'STOCK_ID': 614, 'WAREHOUSE_ID': 7, 'SKU': 'ZZ991230', 'BRAND': 'WRONG', 'NAME': 'Другой артикул', 'QUANTITY': 9, 'PRICE': 1},
    ])
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    tools.PART_WRITE_INTERVAL = 0
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    return session, tools, pipe


def send(pipe, text, user=19):
    return asyncio.run(pipe.handle_text(text, [], user))


def writes(session):
    return [c for c in session.calls if c['method'] in ('offer/require/add', 'offer/order/add_free_order', 'offer/order/add') or c['method'].startswith('wms/') and c['method'] != 'wms/get_filtered_stocks' and c['method'] != 'wms/product_cards/get_filtered']


def test_stock_only_search_with_two_brands_and_multiple_warehouses(env):
    session, tools, _ = env
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['status'] == 'ambiguous' and result['best'] is None
    assert result['stock_lookup']['status'] == 'ok' and result['stock_lookup']['rows'] == 3
    assert {c['brand'] for c in result['candidates']} == {'ABCORP', 'OTHERCO'}
    first = next(c for c in result['candidates'] if c['brand'] == 'ABCORP')
    assert first['source'] == 'склад' and first.get('price') is None and first.get('qty') is None
    assert {r['warehouse_id'] for r in first['stock_records']} == {7, 8}
    assert '1000000' in parts.in_stock_note(first)
    assert '611' not in str(first) and not writes(session)
    assert any(x['method'] == 'wms/get_filtered_stocks' for x in session.calls)


def test_explicit_brand_matches_exactly_not_prefix(env):
    session, tools, _ = env
    session.data['wms/get_filtered_stocks'].extend([
        {'STOCK_ID': 620, 'WAREHOUSE_ID': 7, 'SKU': 'AA12345', 'BRAND': 'FE', 'NAME': 'Деталь', 'QUANTITY': 2},
        {'STOCK_ID': 621, 'WAREHOUSE_ID': 7, 'SKU': 'AA12345', 'BRAND': 'FENOX', 'NAME': 'Деталь', 'QUANTITY': 3},
    ])
    result = tools.resolve_part(query='AA12345', sku='AA12345', brand='FE', allow_web=False)
    assert {c['brand'] for c in result['candidates']} == {'FE'}
    assert not writes(session)


def test_stock_result_selection_produces_manual_draft_no_stock_ids(env):
    session, tools, pipe = env
    first = send(pipe, 'Добавь в сделку 1313 товар ZZ99123 за 5000')
    assert first.choices and not first.drafts and not writes(session)
    assert 'справочный поиск' in render_part_choice(first.choices[0])
    picked = next(c for c in first.choices[0]['candidates'] if c['brand'] == 'ABCORP')
    result = pipe.resume_choice(first.choices[0], picked['index'], user_key=19)
    assert len(result.drafts) == 1 and not writes(session)
    draft = result.drafts[0]
    assert (draft['check']['offer_id'], draft['check']['brand'], draft['check']['sku'], draft['check']['price'], draft['check']['qty']) == (1313, 'ABCORP', 'ZZ99123', 5000, 1)
    assert 'STOCK_ID' not in str(draft['steps']) and 'STOCKS_IDS_SYSTEM' not in str(draft['steps'])
    assert '111' not in str(draft['steps']) and '1000000' not in str(draft['steps'])
    assert 'склад' in str(draft.get('warning', '')).lower()
    # MockSession по умолчанию относит новые строки только к сделке 1654;
    # моделируем корректную привязку к 1313, не меняя рабочую CRM.
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['OFFER_ID'] = 1313
        return response
    session._add_order = add
    result = tools.apply_draft(draft['token'])
    assert result['verified']
    calls = [c for c in writes(session) if c['method'] in ('offer/require/add', 'offer/order/add_free_order')]
    assert [c['method'] for c in calls] == ['offer/require/add', 'offer/order/add_free_order']
    assert calls[1]['payload']['PRICE'] == 5000 and calls[1]['payload']['AMOUNT'] == 1
    assert 'STOCK_ID' not in str(calls) and 'STOCKS_IDS_SYSTEM' not in str(calls)


def test_stock_cost_and_sale_not_used_when_user_did_not_name_price(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь в сделку 1313 товар ZZ99123')
    assert result.choices and not result.drafts
    picked = next(c for c in result.choices[0]['candidates'] if c['brand'] == 'ABCORP')
    followup = pipe.resume_choice(result.choices[0], picked['index'], user_key=19)
    assert followup.pending_price and not followup.drafts and not writes(session)


def test_history_and_stock_same_sku_keep_history_price_name_and_provenance(env):
    session, tools, _ = env
    history = session.data['products/get_filtered_orders']['DATA']
    history.append({'ORDER_ID': 994, 'OFFER_ID': 123, 'BRAND': 'ABCORP', 'SKU': 'ZZ99123', 'NAME': 'Имя из истории', 'QUANTITY': 1, 'PRICE': '4500'})
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    candidate = next(x for x in result['candidates'] if x['brand'] == 'ABCORP')
    assert candidate['source'] == 'история' and candidate['name'] == 'Имя из истории'
    assert float(candidate['price']) == 4500 and len(candidate['stock_records']) == 2
    assert not writes(session)


def test_partcheck_includes_stock_and_history_and_not_internet(env):
    session, tools, _ = env
    history = session.data['products/get_filtered_orders']['DATA']
    history.append({'ORDER_ID': 995, 'OFFER_ID': 123, 'BRAND': 'ABCORP', 'SKU': 'ZZ99123', 'NAME': 'Деталь', 'QUANTITY': 1, 'PRICE': '4500'})
    checks = run_partcheck(tools, 'ZZ99123')
    text = '\n'.join(c.line() for c in checks)
    assert 'Склад: строк в проверенной выдаче: 3' in text
    assert 'Интернет-поиск не потребовался или пропущен' in text
    assert 'Поиск по складу отключён' not in text and not writes(session)


def test_stock_unavailable_not_reported_as_empty(env, monkeypatch):
    session, tools, _ = env
    def failed(**kwargs):
        raise StocrmError('нет доступа к складу')
    monkeypatch.setattr(tools, 'stock', failed)
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['stock_lookup']['status'] == 'unavailable'
    assert 'склад' in ' '.join(result['warnings']).lower()
    text = '\n'.join(c.line() for c in run_partcheck(tools, 'ZZ99123'))
    assert 'проверка не выполнена или неполная' in text and not writes(session)


def test_human_brand_search_is_read_only_and_keeps_user_data(env):
    session, _, pipe = env
    result = send(pipe, 'В сделку 1313 добавь производитель ABCORP, артикул ZZ99123 за 5000')
    assert len(result.drafts) == 1 and not writes(session)
    assert any(x['method'] == 'wms/get_filtered_stocks' for x in session.calls)
    assert 'только справочно' in result.answer
    assert result.drafts[0]['check']['brand'] == 'ABCORP' and result.drafts[0]['check']['price'] == 5000
    assert 'STOCK_ID' not in str(result.drafts[0]['steps'])


def test_stock_reference_forbidden_for_supplier_and_legacy_draft(env):
    session, tools, _ = env
    with pytest.raises(ValueError, match='Складские позиции'):
        tools.draft_add_offer_part(offer_id=1313, name='Подшипник', sku='ZZ99123', brand='ABCORP', price=5000, source='supplier', supplier_id=1, stock_ids=[611])
    draft = tools.draft_add_offer_part(offer_id=1313, name='Подшипник', sku='ZZ99123', brand='ABCORP', price=5000)
    steps = copy.deepcopy(draft['steps'])
    steps[-1]['payload']['STOCKS_IDS_SYSTEM'] = [611]
    tools.drafts.mark(draft['token'], 'pending', steps=steps)
    with pytest.raises(ValueError, match='привязан к складской позиции'):
        tools.apply_draft(draft['token'])
    assert not writes(session)


def test_group_stock_lookup_requires_choices_and_one_manual_draft(env):
    session, _, pipe = env
    session.data['wms/get_filtered_stocks'].append(
        {'STOCK_ID': 625, 'WAREHOUSE_ID': 7, 'SKU': 'BB88222', 'BRAND': 'NEWCO', 'NAME': 'Катушка', 'QUANTITY': 4, 'PRICE': 50})
    first = send(pipe, 'В сделку 1313 добавь ZZ99123 за 5000 и BB88222 за 2000')
    assert first.choices and not first.drafts and not writes(session)
    index = next(c['index'] for c in first.choices[0]['candidates'] if c['brand'] == 'ABCORP')
    second = pipe.resume_choice(first.choices[0], index, user_key=19)
    assert second.choices and not second.drafts and not writes(session)
    assert second.choices[0]['candidates'][0]['brand'] == 'NEWCO'
    final = pipe.resume_choice(second.choices[0], second.choices[0]['candidates'][0]['index'], user_key=19)
    assert len(final.drafts) == 1 and not writes(session)
    draft = final.drafts[0]
    assert [(r['sku'], r['price'], r['qty']) for r in draft['check']['items']] == [('ZZ99123', 5000, 1), ('BB88222', 2000, 1)]
    assert 'STOCK_ID' not in str(draft['steps']) and 'STOCKS_IDS_SYSTEM' not in str(draft['steps'])


def test_explicit_add_from_stock_is_refused_not_silently_changed(env):
    session, tools, pipe = env
    result = send(pipe, 'Добавь со склада ZZ99123 в сделку 1313 за 5000')
    assert 'со склада в сделку не добавляю' in result.answer.lower()
    assert not result.drafts and not result.applied and not writes(session)
    with pytest.raises(ValueError, match='Со склада'):
        tools.draft_add_offer_part(offer_id=1313, name='Подшипник', sku='ZZ99123', price=5000, source='stock')


def test_stock_failure_partial_and_invalid_quantities(env, monkeypatch):
    _, tools, _ = env
    def partial(**kw):
        if kw.get('sku') == 'ZZ99123':
            raise StocrmError('ошибка чтения')
        if kw.get('sku') == 'ZZ 99123':
            return [{'sku': 'ZZ99123', 'brand': 'ABCORP', 'name': 'Подшипник', 'warehouse_id': 7, 'qty': 2}]
        return []
    monkeypatch.setattr(tools, 'stock', partial)
    result = tools.resolve_part(query='ZZ99123', sku='ZZ99123', allow_web=False)
    assert result['stock_lookup']['status'] == 'partial'
    assert result['candidates'][0]['brand'] == 'ABCORP'
    assert 'ошибка чтения' in str(result['warnings'])
    assert not parts.in_stock_note({'stock_records': [{'qty': 'nan'}, {'qty': 'inf'}]})


def test_incomplete_stock_lookup_is_visible_in_choice(env, monkeypatch):
    session, tools, pipe = env
    def partial(**kw):
        if kw.get('sku') == 'ZZ99123':
            raise StocrmError('ошибка чтения')
        if kw.get('sku') == 'ZZ 99123':
            return [{'sku': 'ZZ99123', 'brand': 'ABCORP', 'name': 'Подшипник', 'warehouse_id': 7, 'qty': 2}]
        return []
    monkeypatch.setattr(tools, 'stock', partial)
    result = send(pipe, 'Добавь в сделку 1313 товар ZZ99123 за 5000')
    assert 'не завершена полностью' in result.choices[0]['question']
    assert result.choices[0]['candidates'][0]['brand'] == 'ABCORP' and not writes(session)


def test_legacy_unknown_stock_method_is_blocked(env):
    session, tools, _ = env
    draft = tools.draft_add_offer_part(offer_id=1313, name='Подшипник', sku='ZZ99123', brand='ABCORP', price=5000)
    steps = copy.deepcopy(draft['steps'])
    steps[-1]['method'] = 'offer/order/add_from_stock'
    tools.drafts.mark(draft['token'], 'pending', steps=steps)
    with pytest.raises(ValueError, match='складской позиции'):
        tools.apply_draft(draft['token'])
    assert not writes(session)
