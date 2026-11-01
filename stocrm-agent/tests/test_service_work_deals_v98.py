"""Список статуса в работе: только имитация CRM, никаких реальных записей."""
import asyncio

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.fastpath import is_working_offers_request, try_answer
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockResponse, MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    return session, tools, pipe


def send(pipe, text, history=None):
    return asyncio.run(pipe.handle_text(text, [] if history is None else history, 19))


def add_status(session, ident, board, title):
    session.data['offer/all_statuses'][str(ident)] = {'STATUS_ID': ident, 'BOARD_ID': board, 'TITLE': title, 'ACTIVE': 'Y'}


def add_offer(session, ident, board, status, title):
    session.data['offers/get_from_filter'].append({'OFFER_ID': ident, 'BOARD_ID': board,
        'STATUS_ID': status, 'STATUS_NAME': title, 'CONTACT_TITLE': f'Клиент {ident}', 'OFFER_SUM': 1000})


@pytest.mark.parametrize('message', ['Покажи сделки в работе', 'сделки в работе', 'покажи все сделки в работе на сервисе',
                                     'Какие сделки в работе?', 'Что в работе?', 'Покажите заказ-наряды в работе',
                                     'Какие у нас сделки сейчас в работе?', 'Покажи сделки в работе сейчас'])
def test_plain_work_list_returns_only_confirmed_status(env, message):
    session, tools, pipe = env
    add_status(session, 41, 2, 'Запись')
    add_status(session, 42, 2, 'Заказ')
    add_offer(session, 1328, 2, 41, 'Запись')
    add_offer(session, 1327, 2, 42, 'Заказ')
    result = send(pipe, message)
    assert is_working_offers_request(message)
    assert '№1654' in result.answer and 'В работе' in result.answer
    assert '№1328' not in result.answer and '№1327' not in result.answer and '№1712' not in result.answer
    assert 'воронка «Автосервис»' in result.answer and not result.drafts and not result.applied
    reads = [c for c in session.calls if c['method'] == 'offers/get_from_filter']
    assert reads and reads[-1]['payload']['FILTER'] == {'BOARD_ID': 2, 'STATUS_ID': 15}
    assert all(c['method'] in {'boards/get_all', 'offer/all_statuses', 'offers/get_from_filter'} for c in session.calls)


def test_full_service_status_has_priority_over_generic_name(env):
    session, _, pipe = env
    add_status(session, 70, 2, 'В работе на сервисе')
    add_offer(session, 1401, 2, 70, 'В работе на сервисе')
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert '№1401' in answer and '№1654' not in answer
    assert 'В работе на сервисе' in answer
    assert next(c for c in session.calls if c['method'] == 'offers/get_from_filter')['payload']['FILTER']['STATUS_ID'] == 70


def test_local_verification_discards_foreign_or_unconfirmed_rows(env, monkeypatch):
    _, tools, _ = env
    rows = [dict(offer_id=1001, board_id=2, status_id=15, status='Запись'),
            dict(offer_id=1002, board_id=2, status_id=14, status='В работе'),
            dict(offer_id=1003, board_id=3, status_id=15),
            dict(offer_id=1004, board_id=None, status_id=15)]
    calls = []
    def search(**kw):
        calls.append(kw)
        return rows
    monkeypatch.setattr(tools, 'find_offers', search)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert ids == [1001] and '№1001' in answer
    assert all('№' + str(n) not in answer for n in (1002, 1003, 1004))
    assert 'может быть неполным' in answer and calls[0]['fresh'] is True
    assert 'В работе' in answer and 'Запись' not in answer


def test_status_absent_or_unavailable_does_not_fallback_to_active(env, monkeypatch):
    session, tools, pipe = env
    session.data['offer/all_statuses']['15']['TITLE'] = 'На рассмотрении'
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert 'не найден точный статус' in answer and '№1654' not in answer
    assert not any(c['method'] == 'offers/get_from_filter' for c in session.calls)
    monkeypatch.setattr(tools, 'statuses', lambda _: (_ for _ in ()).throw(RuntimeError('down')))
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert 'не удалось прочитать' in answer.lower() and '№1654' not in answer


def test_multiple_boards_ask_and_named_board_can_be_selected(env):
    session, _, pipe = env
    add_status(session, 71, 3, 'В работе')
    add_offer(session, 1402, 3, 71, 'В работе')
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert 'несколько подходящих статусов' in answer and 'Автосервис' in answer and 'Магазин запчастей' in answer
    assert '№1654' not in answer and '№1402' not in answer
    selected = send(pipe, 'Покажи сделки в работе в воронке Магазин запчастей').answer
    assert '№1402' in selected and '№1654' not in selected
    assert 'воронка «Магазин запчастей»' in selected
    by_number = send(pipe, 'сделки в работе в воронке 2').answer
    assert '№1654' in by_number and '№1402' not in by_number
    wrong = send(pipe, 'сделки в работе в воронке другая').answer
    assert 'однозначно найти указанную воронку' in wrong


def test_missing_id_in_status_is_not_guessed(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['15']['BOARD_ID'] = None
    result = send(pipe, 'Покажи сделки в работе')
    assert 'не удалось надёжно проверить' in result.answer.lower() and '№1654' not in result.answer


def test_repeated_page_and_truncated_list_are_explicit(env, monkeypatch):
    _, tools, _ = env
    rows = [dict(offer_id=1000 + i, board_id=2, status_id=14) for i in range(100)]
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: rows)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert not ids and 'не найдено подтверждённых' in answer and 'может быть неполным' in answer
    matches = [dict(offer_id=2000 + i, board_id=2, status_id=15) for i in range(30)]
    monkeypatch.setattr(tools, 'find_offers', lambda **kw: matches)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert len(ids) == 25 and 'может быть неполным' in answer and '№2029' not in answer


def test_failed_later_page_preserves_confirmed_and_warns(env, monkeypatch):
    _, tools, _ = env
    rows = [dict(offer_id=2000 + i, board_id=2, status_id=14) for i in range(99)]
    rows.append(dict(offer_id=1654, board_id=2, status_id=15))
    def search(**kw):
        if kw['page'] == 1: return rows
        raise RuntimeError('failed')
    monkeypatch.setattr(tools, 'find_offers', search)
    answer, ids = try_answer('Покажи сделки в работе', tools)
    assert ids == [1654] and 'может быть неполным' in answer


def test_fresh_status_lookup_not_cached_after_external_change(env):
    session, _, pipe = env
    assert '№1654' in send(pipe, 'Покажи сделки в работе').answer
    session.data['offers/get_from_filter'][0]['STATUS_ID'] = 14
    session.data['offers/get_from_filter'][0]['STATUS_NAME'] = 'Просчёт'
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert '№1654' not in answer and 'не найдено подтверждённых' in answer
    assert len([c for c in session.calls if c['method'] == 'offers/get_from_filter']) == 2


def test_model_disabled_fastpath_still_applies_exact_status(env):
    _, _, pipe = env
    pipe.fastpath = False
    class Model:
        async def chat(self, *args, **kwargs):
            pytest.fail('model must not choose arbitrary active deals')
    pipe.llm = Model()
    result = send(pipe, 'Покажи сделки в работе')
    assert '№1654' in result.answer and '№1712' not in result.answer


def test_old_list_cleared_on_no_matches_and_new_list_remembers_selection(env):
    session, _, pipe = env
    history = []
    result = send(pipe, 'Покажи сделки в работе', history)
    assert pipe._lists[19] == [1654]
    detail = send(pipe, '1 подробнее', history)
    assert 'Сделка №1654' in detail.answer
    session.data['offers/get_from_filter'][0]['STATUS_ID'] = 14
    result = send(pipe, 'Покажи сделки в работе', history)
    assert 'не найдено подтверждённых' in result.answer and 19 not in pipe._lists


def test_invalid_raw_crm_rows_do_not_become_empty_list(env, monkeypatch):
    session, _, pipe = env
    original = session.post
    def post(url, json=None, timeout=None):
        if url.endswith('/offers/get_from_filter'):
            return MockResponse({'CODE': 200, 'RESPONSE': {'UNKNOWN': 'unexpected'}}, 200)
        return original(url, json=json, timeout=timeout)
    monkeypatch.setattr(session, 'post', post)
    answer = send(pipe, 'Покажи сделки в работе').answer
    assert 'не найдено подтверждённых' in answer and 'может быть неполным' in answer


@pytest.mark.parametrize('message', ['переведи сделку 1654 в работе', 'покажи сделки в работе клиента Иванова',
                                     'покажи сделки в работе за сегодня', 'покажи сделку 1654', 'покажи последние сделки'])
def test_other_requests_are_not_mistaken_for_service_list(message):
    assert not is_working_offers_request(message)
