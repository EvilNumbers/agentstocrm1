"""Сценарии списков разных статусов, без настоящей сети и записей."""
import asyncio

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.fastpath import is_status_offers_request, try_answer
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    statuses = session.data['offer/all_statuses']
    statuses['14']['TITLE'] = 'Просчёт запчастей'
    for sid, title in [(41, 'Запись'), (42, 'Заказ'), (43, 'Заказ собран'), (44, 'Просчёт двигателя')]:
        statuses[str(sid)] = {'STATUS_ID': sid, 'BOARD_ID': 2, 'TITLE': title, 'ACTIVE': 'Y'}
    for oid, status, name in [(1328, 41, 'Запись'), (1327, 42, 'Заказ'), (1326, 43, 'Заказ собран'), (1325, 44, 'Просчёт двигателя')]:
        session.data['offers/get_from_filter'].append({'OFFER_ID': oid, 'BOARD_ID': 2, 'STATUS_ID': status,
            'STATUS_NAME': name, 'CONTACT_TITLE': f'Клиент {oid}', 'OFFER_SUM': 100})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text, history=None):
    return asyncio.run(pipe.handle_text(text, [] if history is None else history, 19))


@pytest.mark.parametrize('message,expected,status_id,other', [
    ('Покажи сделки в записи', 1328, 41, [1327, 1326, 1325, 1712, 1654]),
    ('Покажи сделки которые в записи', 1328, 41, [1327, 1326, 1325, 1712, 1654]),
    ('какие сделки в заказе?', 1327, 42, [1328, 1326, 1325, 1712, 1654]),
    ('покажи сделки в просчете', 1712, 14, [1328, 1327, 1326, 1325, 1654]),
    ('покажи сделки на просчете запчастей', 1712, 14, [1328, 1327, 1326, 1325, 1654]),
    ('Покажи собранные заказы', 1326, 43, [1328, 1327, 1325, 1712, 1654]),
    ('Какие заказы собраны?', 1326, 43, [1328, 1327, 1325, 1712, 1654]),
    ('покажи сделки в статусе Просчёт двигателя', 1325, 44, [1328, 1327, 1326, 1712, 1654]),
    ('сделки со статусом Успешно', None, 34, [1328, 1327, 1326, 1325, 1712, 1654]),
])
def test_status_lists_are_not_interchangeable(env, message, expected, status_id, other):
    session, _, pipe = env
    result = send(pipe, message)
    assert is_status_offers_request(message)
    if expected:
        assert f'№{expected}' in result.answer
    else:
        assert 'не найдено подтверждённых' in result.answer
    for oid in other:
        assert f'№{oid}' not in result.answer
    call = next(c for c in session.calls if c['method'] == 'offers/get_from_filter')
    assert call['payload']['FILTER'] == {'BOARD_ID': 2, 'STATUS_ID': status_id}
    assert not result.drafts and not result.applied
    assert all(c['method'] in {'boards/get_all', 'offer/all_statuses', 'offers/get_from_filter'} for c in session.calls)


def test_service_status_still_resolved_before_others(env):
    _, _, pipe = env
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert '№1654' in answer and all(f'№{n}' not in answer for n in (1328, 1327, 1326, 1712))


def test_assembled_missing_does_not_fall_back_to_order(env):
    session, _, pipe = env
    session.data['offer/all_statuses'].pop('43')
    result = send(pipe, 'Покажи собранные заказы')
    assert 'не найден подходящий точный статус' in result.answer.lower()
    assert '№1327' not in result.answer and not result.drafts
    assert not any(c['method'] == 'offers/get_from_filter' for c in session.calls)


def test_similar_estimate_statuses_require_explicit_choice(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Просчёт', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1511, 'BOARD_ID': 2, 'STATUS_ID': 51, 'STATUS_NAME': 'Просчёт'})
    answer = send(pipe, 'Покажи сделки в просчете').answer
    assert 'несколько подходящих статусов' in answer and 'Просчёт запчастей' in answer and 'Просчёт' in answer
    assert '№1712' not in answer and '№1511' not in answer
    exact = send(pipe, 'покажи сделки в статусе Просчёт запчастей в воронке Автосервис').answer
    assert '№1712' in exact and '№1511' not in exact
    other = send(pipe, 'покажи сделки в статусе «Просчёт» в воронке 2').answer
    assert '№1511' in other and '№1712' not in other


def test_duplicate_status_name_in_boards_can_be_resolved(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['45'] = {'STATUS_ID': 45, 'BOARD_ID': 3, 'TITLE': 'Заказ', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1401, 'BOARD_ID': 3, 'STATUS_ID': 45, 'STATUS_NAME': 'Заказ'})
    answer = send(pipe, 'Покажи сделки в заказе').answer
    assert 'несколько подходящих статусов' in answer and 'Автосервис' in answer and 'Магазин запчастей' in answer
    assert '№1401' not in answer and '№1327' not in answer
    chosen = send(pipe, 'Покажи сделки в заказе в воронке Магазин запчастей').answer
    assert '№1401' in chosen and '№1327' not in chosen
    chosen = send(pipe, 'Покажи сделки в статусе Заказ в воронке 2').answer
    assert '№1327' in chosen and '№1401' not in chosen


def test_no_match_for_lookalike_status_or_unknown_title(env):
    _, _, pipe = env
    result = send(pipe, 'покажи сделки в статусе Заказан')
    assert 'не найден подходящий точный статус' in result.answer.lower()
    assert '№1327' not in result.answer


@pytest.mark.parametrize('message', [
    'покажи сделки в заказе и в записи',
    'покажи сделки в записи за сегодня',
    'покажи сделки в заказе клиента Иванова',
    'покажи собранные заказы за сегодня',
    'покажи сделки в работе и в заказе',
    'покажи сделки в ожидании',
    'сколько сделок в заказе',
])
def test_other_or_combined_filters_are_never_silently_dropped(env, message):
    session, _, pipe = env
    pipe.fastpath = False
    class Model:
        async def chat(self, *args, **kw): pytest.fail('model must not guess or silently discard filters')
    pipe.llm = Model()
    assert is_status_offers_request(message)
    result = send(pipe, message)
    assert 'уточните' in result.answer.lower() and 'не подменяю' in result.answer.lower()
    assert not result.drafts and not any(c['method'] == 'offers/get_from_filter' for c in session.calls)


def test_status_route_is_independent_of_fastpath_setting(env):
    _, _, pipe = env
    pipe.fastpath = False
    class Model:
        async def chat(self, *args, **kw): pytest.fail('model must not infer arbitrary active deals')
    pipe.llm = Model()
    assert '№1327' in send(pipe, 'Покажи сделки в заказе').answer
    assert '№1326' in send(pipe, 'Покажи собранные заказы').answer
    assert '№1712' in send(pipe, 'Покажи сделки в просчете').answer


def test_fresh_page_filters_results_and_exposes_incomplete_reads(env, monkeypatch):
    _, tools, _ = env
    seen = []
    def search(**kw):
        seen.append(kw)
        return [dict(offer_id=1001, board_id=2, status_id=42), dict(offer_id=1002, board_id=2, status_id=43),
                dict(offer_id=1003, board_id=3, status_id=42), dict(offer_id=1004, board_id=None, status_id=42)]
    monkeypatch.setattr(tools, 'find_offers', search)
    answer, ids = try_answer('Покажи сделки в заказе', tools)
    assert ids == [1001] and '№1001' in answer
    assert all('№' + str(n) not in answer for n in (1002, 1003, 1004))
    assert 'может быть неполным' in answer and seen[0]['fresh']


def test_no_match_clears_previous_list_and_does_not_write(env):
    session, _, pipe = env
    history = []
    assert '№1327' in send(pipe, 'Покажи сделки в заказе', history).answer
    assert pipe._lists[19] == [1327]
    session.data['offers/get_from_filter'] = [x for x in session.data['offers/get_from_filter'] if x['STATUS_ID'] != 42]
    assert 'не найдено подтверждённых' in send(pipe, 'Покажи сделки в заказе', history).answer
    assert 19 not in pipe._lists
    assert all(c['method'] in {'boards/get_all', 'offer/all_statuses', 'offers/get_from_filter'} for c in session.calls)


@pytest.mark.parametrize('message', ['Переведи сделку 1327 в заказ', 'Создай заказ', 'Собери заказ 1327',
                                     'Запиши клиента в сделку', 'Покажи последние сделки', 'Покажи сделку 1327',
                                     'Покажи сделки в ноябре', 'Покажи сделки в воронке Автосервис'])
def test_write_actions_and_ordinary_reads_not_reclassified(message):
    assert not is_status_offers_request(message)
