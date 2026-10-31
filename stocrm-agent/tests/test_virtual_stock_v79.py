"""Количество строки и остаток склада: API имитируется, настоящая CRM не меняется."""
import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.report import format_apply_result
from stocrm_agent.tools import Toolset


@pytest.fixture
def setup_tools(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session),
                    DraftStore(tmp_path / 'drafts.json'), AuditLog(tmp_path / 'audit.jsonl'))
    tools.PART_WRITE_INTERVAL = 0
    return session, tools


def test_report_reads_quantity_from_order_not_nested_stock():
    result = {'ORDER_ID': 8297, 'QUANTITY_CURRENT': 1,
              'FORMATTED_STOCK': {'NAME': 'Фильтр', 'AMOUNT': 1_000_000,
                                  'QUANTITY': 1_000_000, 'CURRENT_VOLUME': 1_000_000}}
    text = format_apply_result('offer/order/add', result)
    assert 'количество 1' in text
    assert '1 000 000' not in text and 'больше, чем нужно' not in text
    assert text.startswith('✅')


def test_report_does_not_guess_quantity_from_stock_when_line_field_absent():
    result = {'ORDER_ID': 8297, 'FORMATTED_STOCK': {'NAME': 'Фильтр', 'AMOUNT': 1_000_000}}
    text = format_apply_result('offer/order/add', result)
    assert 'количество' not in text and '1 000 000' not in text


def test_legitimate_large_line_quantity_is_not_automatically_wrong():
    text = format_apply_result('offer/order/add', {'ORDER_ID': 8297, 'QUANTITY_CURRENT': 1000})
    assert 'количество 1 000' in text
    assert '⚠️' not in text


def test_readback_prefers_line_quantity_over_ambiguous_quantity(setup_tools):
    session, tools = setup_tools
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH88011', sku='CH88011', price=1200)
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        row = session._order_rows()[-1]
        row['QUANTITY'] = 1_000_000
        row['QUANTITY_CURRENT'] = 1
        row['FORMATTED_STOCK'] = {'AMOUNT': 1_000_000, 'CURRENT_VOLUME': 1_000_000}
        return response
    session._add_order = add
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'applied' and outcome['verified']
    assert tools.offer_parts(1654)[0]['qty'] == 1
    assert not any(call['method'] in tools.price_methods for call in session.calls)


def test_ambiguous_mismatch_is_unverified_not_a_false_factual_error(setup_tools):
    session, tools = setup_tools
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH88011', sku='CH88011', price=1200)
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1_000_000
        return response
    session._add_order = add
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    assert 'не удалось надёжно проверить количество' in outcome['warning']
    assert '1000000' not in outcome['warning'] and 'записал позицию по-своему' not in outcome['warning']
    review = tools.review_draft(draft['token'])
    assert not review['verified'] and '1000000' not in str(review)
    with pytest.raises(ValueError):
        tools.confirm_draft_result(draft['token'])


def test_explicit_line_quantity_mismatch_is_still_detected(setup_tools):
    session, tools = setup_tools
    draft = tools.draft_add_offer_part(offer_id=1654, name='CH88011', sku='CH88011', price=1200)
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        row = session._order_rows()[-1]
        row['QUANTITY_CURRENT'] = 2
        row['QUANTITY'] = 1_000_000
        return response
    session._add_order = add
    outcome = tools.apply_draft(draft['token'])
    assert outcome['status'] == 'partial' and not outcome['verified']
    assert 'количество в строке 2 вместо 1' in outcome['warning']
    assert '1000000' not in outcome['warning']


def test_batch_ignores_nested_virtual_stock_and_completes(setup_tools, monkeypatch):
    import asyncio
    from stocrm_agent import websearch
    from stocrm_agent.bot.pipeline import BotPipeline
    from stocrm_agent.bot.settings import BotSettings

    session, tools = setup_tools
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path='/nonexistent/policy.json'))
    monkeypatch.setattr(tools, 'resolve_part', lambda **kwargs: {'status': 'not_found', 'candidates': []})
    monkeypatch.setattr(websearch, 'search_batch', lambda *args, **kwargs: [])
    send = lambda text: asyncio.run(pipe.handle_text(text, [], 19))
    send('Добавь в сделку 1654 CH88011 за 1200 и CH88014E за 4200')
    send('добавь как указано')
    draft = send('добавь как указано').drafts[0]
    original = session._add_order
    def add(method, payload):
        response = original(method, payload)
        row = session._order_rows()[-1]
        row['FORMATTED_STOCK'] = {'AMOUNT': 1_000_000, 'CURRENT_VOLUME': 1_000_000}
        row['QUANTITY_CURRENT'] = row['QUANTITY']
        return response
    session._add_order = add
    result = tools.apply_draft(draft['token'])
    assert result['verified'] and result['queue_completed'] == 2
    assert all('1 000 000' not in format_apply_result('offer/order/add_free_order', item)
               for item in result['result'])
