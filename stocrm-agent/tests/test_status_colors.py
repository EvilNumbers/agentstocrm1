"""Локальные регрессии; MockSession не подтверждает поведение реальной CRM."""
import asyncio
import json
import datetime as dt

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.booking_dates import parse_when
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.bot.status_colors import color_marker, with_status_colors, append_llm_colors
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def setup(tmp_path):
    session = MockSession()
    session.data['offer/all_statuses']['14'].update(TITLE='Просчёт', FRONT_COLOR='#f0ce22')
    session.data['offer/all_statuses']['15'].update(TITLE='В работе', FRONT_COLOR='#21af36')
    session.data['offer/all_statuses']['41'] = {'STATUS_ID': 41, 'BOARD_ID': 2, 'TITLE': 'Запись', 'FRONT_COLOR': '#2288ff'}
    session.data['offers/get_from_filter'][1]['STATUS_NAME'] = 'Просчёт'
    session.data['offers/get_from_filter'].append({
        'OFFER_ID': 1730, 'BOARD_ID': 2, 'BOARD_NAME': 'Автосервис',
        'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'CONTACT_TITLE': 'Клиент', 'OFFER_SUM': '1000',
    })
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path / 'drafts.json'), AuditLog(tmp_path / 'audit.jsonl'))
    settings = BotSettings(token='test', policy_path=str(tmp_path / 'policy.json'))
    return session, tools, BotPipeline(tools, None, None, settings)


def run(pipeline, text, history):
    return asyncio.run(pipeline.handle_text(text, history, user_id=19))


def test_status_response_preserves_documented_color(setup):
    _, tools, _ = setup
    assert {x['title']: x['front_color'] for x in tools.statuses(2)}['Просчёт'] == '#f0ce22'


@pytest.mark.parametrize('color,expected', [('#ff0000','🟥'), ('#f90','🟧'), ('#f0ce22','🟨'), ('#21af36','🟩'), ('#2288ff','🟦'), ('#b44aff','🟪'), ('#ffffff','⬜'), ('#000000','⬛'), ('#fff<script>',''), ('red',''), (None,'')])
def test_palette(color, expected):
    assert color_marker(color) == expected


def test_list_and_followup_through_bot_handler(setup):
    session, _, pipeline = setup
    history = []
    answer = run(pipeline, 'покажи последние сделки', history)
    assert '🟩 1. №1654' in answer.answer
    assert '🟨 2. №1712' in answer.answer
    assert '🟦 3. №1730' in answer.answer
    assert pipeline._lists[19] == [1654, 1712, 1730]
    detail = run(pipeline, '2 подробнее', history)
    assert 'Сделка №1712' in detail.answer and '🟨' in detail.answer
    assert not answer.drafts and not detail.drafts
    assert all(not call['method'].endswith(('/add', '/change', '/add_free_order')) for call in session.calls)


def test_missing_and_failed_statuses_do_not_break_list(setup):
    _, tools, pipeline = setup
    def failed(_board):
        raise RuntimeError('недоступен справочник')
    tools.statuses = failed
    answer = run(pipeline, 'покажи последние сделки', [])
    assert '№1654' in answer.answer and 'В работе' in answer.answer
    assert '🟩' not in answer.answer and '🟨' not in answer.answer


def test_color_uses_board_and_unique_name_not_status_id():
    class Stub:
        def statuses(self, board):
            return [{'board_id': 1, 'status_id': 10, 'title': 'Запись', 'front_color': '#f00'},
                    {'board_id': 2, 'status_id': 10, 'title': 'Запись', 'front_color': '#0f0'}]
    rows = [{'board_id': 1, 'status_id': None, 'status': 'Запись'},
            {'board_id': 2, 'status_id': 99, 'status': 'Запись'},
            {'board_id': None, 'status': 'Запись'}, {'board_id': 1, 'status': 'Заказ'}]
    result = with_status_colors(Stub(), rows)
    assert [r.get('status_marker') for r in result] == ['🟥', '🟩', None, None]
    assert all('status_marker' not in row for row in rows)


def test_conflicting_colors_are_not_guessed():
    class Stub:
        def statuses(self, board):
            return [{'board_id': 1, 'title': 'Запись', 'front_color': '#f00'},
                    {'board_id': 1, 'title': 'Запись', 'front_color': '#0f0'}]
    assert 'status_marker' not in with_status_colors(Stub(), [{'board_id': 1, 'status': 'Запись'}])[0]


def test_model_tool_path_uses_current_crm_color(setup):
    _, _, pipeline = setup
    pipeline.fastpath = False
    class Model:
        def __init__(self): self.calls = 0
        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {'tool_calls': [{'id':'a', 'function': {'name':'find_offers', 'arguments':json.dumps({'limit': 10})}}]}
            return {'content': 'Сделка №1654 в работе, сделка №1712 в просчёте. Сумма 1730 рублей.'}
    pipeline.llm = Model()
    response = run(pipeline, 'Какие сделки?', [])
    assert '🟩 №1654 — В работе' in response.answer
    assert '🟨 №1712 — Просчёт' in response.answer
    assert '№1730 —' not in response.answer


def test_no_guess_in_model_result():
    rows = [{'offer_id': 1234, 'status': 'Запись', 'status_marker': '🟦'}]
    assert append_llm_colors('Сумма 1234 рублей', rows) == 'Сумма 1234 рублей'
    assert '🟦 №1234' in append_llm_colors('Сделка №1234', rows)


def test_time_regression():
    for answer in ('11', '11 утра', 'в 11', '11:00', '11.00'):
        assert parse_when(answer, reference='2026-10-31T09:00:00+03:00', today=dt.date(2026, 10, 31)) == '2026-10-31T11:00:00+03:00'
    assert parse_when('11 ноября', reference='2026-10-31', today=dt.date(2026, 10, 31)) == '2026-11-11'
    assert parse_when('11 утра', today=dt.date(2026, 10, 31)) is None


def test_manual_draft_confirmation_and_readback(setup):
    session, tools, _ = setup
    draft = tools.draft_add_offer_part(offer_id=1654, name='Тестовая деталь', sku='ABC123', brand='DAP', qty=2, price=500)
    assert not any(c['method'] == 'offer/order/add_free_order' for c in session.calls)
    assert any(s['method'] == 'offer/order/add_free_order' for s in draft['steps'])
    result = tools.apply_draft(draft['token'])
    assert result.get('status') == 'applied', result
    writes = [c for c in session.calls if c['method'] == 'offer/order/add_free_order']
    assert len(writes) == 1
    pos = session.calls.index(writes[0])
    assert any(c['method'] == 'products/get_filtered_orders' for c in session.calls[pos + 1:])
    item = next(r for r in tools.offer_parts(1654) if r.get('sku') == 'ABC123')
    assert float(item['price']) == 500 and int(item['qty']) == 2


def test_booking_dialog_confirmation_and_readback(setup):
    session, tools, pipeline = setup
    history = []
    first = run(pipeline, 'Запиши сделку 1654 на сегодня', history)
    assert first.pending_booking['field'] == 'change_status'
    assert run(pipeline, 'нет', history).pending_booking['field'] == 'start'
    assert run(pipeline, '11 утра', history).pending_booking['field'] == 'duration_minutes'
    assert run(pipeline, 'час', history).pending_booking['field'] == 'post_id'
    final = run(pipeline, '1', history)
    assert len(final.drafts) == 1 and not session.calendar_cards
    draft = final.drafts[0]
    assert [s['method'] for s in draft['steps']] == ['calendar/card/new']
    assert f'{dt.date.today().isoformat()}T11:00:00+03:00' in draft['summary']
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'applied', result
    assert len(session.calendar_cards) == 1
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)
    idx = next(i for i, c in enumerate(session.calls) if c['method'] == 'calendar/card/new')
    assert any(c['method'] in ('calendar/card/get', 'calendar/post/get_filtered') for c in session.calls[idx + 1:])


def test_native_quantity_mismatch_stops_and_blocks_repeat(setup):
    session, tools, _ = setup
    original = session._add_order
    def wrong_quantity(method, payload):
        result = original(method, payload)
        session._order_rows()[-1]['QUANTITY'] = 1
        return result
    session._add_order = wrong_quantity
    draft = tools.draft_add_offer_part(offer_id=1654, name='Позиция', sku='XYZ-004', brand='DAP', qty=2, price=500)
    first = tools.apply_draft(draft['token'])
    assert first['status'] == 'partial', first
    with pytest.raises(ValueError, match='частично'):
        tools.apply_draft(draft['token'])
    assert sum(c['method'] == 'offer/order/add_free_order' for c in session.calls) == 1


def test_status_color_first_and_short_deal_numbers_still_support_followup(setup):
    session, _, pipeline = setup
    session.data['offers/get_from_filter'].append({
        'OFFER_ID': 97, 'BOARD_ID': 2, 'BOARD_NAME': 'Автосервис',
        'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'CONTACT_TITLE': 'Клиент', 'OFFER_SUM': '1000',
    })
    answer = run(pipeline, 'Покажи сделки в записи', [])
    assert '🟦 1. №1730' in answer.answer and '🟦 2. №97' in answer.answer
    assert pipeline._lists[19] == [1730, 97]
    detail = run(pipeline, '2 подробнее', [])
    assert 'Сделка №97' in detail.answer
    assert not answer.drafts and not detail.drafts
