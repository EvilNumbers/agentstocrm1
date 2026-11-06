"""Несколько услуг из одной просьбы — одной заявкой (волна 138).

Случай из журнала 06.11 (сделка №1371): «В сделку 1371 добавь услугу замена масла
ДВС 300 / Замена салонного фильтра 500». Обе услуги стали отдельными черновиками;
нажатие двух «Применить» подряд упиралось в защиту «такая запись уже выполняется или
её результат неизвестен», и в сделку попала только одна услуга из двух.

Теперь услуги из одной просьбы идут одним черновиком с шагами `work/add`: одно
подтверждение, один снимок работ «до/после», одна проверка. Проверки ниже
фиксируют и это, и то, что неоднозначный результат по-прежнему не повторяется.
"""
import asyncio
import json
import time

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.report import format_apply_result
from stocrm_agent.tools import Toolset


TWO_WORKS = [{'title': 'замена масла ДВС', 'sum_value': 300.0},
             {'title': 'Замена салонного фильтра', 'sum_value': 500.0}]


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session),
                    DraftStore(tmp_path / 'drafts.json'),
                    AuditLog(tmp_path / 'audit.jsonl'))
    return session, tools


def writes(session, method='work/add'):
    return [call for call in session.calls if call['method'] == method]


def works_of(session, offer_id=1654):
    return [row for row in session.data['work/get_filtered']['DATA']
            if str(row.get('OFFER_ID')) == str(offer_id)]


def skip_second_write(tools, *, extra_row=None, monkeypatch=None):
    """Пишем только первый шаг: как будто панель приняла не всё."""
    original = tools._call_step
    calls = {'n': 0}

    def step(step_row, payload, results=None):
        calls['n'] += 1
        if calls['n'] == 2:
            if extra_row:
                tools.client.session.data['work/get_filtered']['DATA'].append(dict(extra_row))
            return 0
        return original(step_row, payload, results)

    monkeypatch.setattr(tools, '_call_step', step)


def test_two_services_become_one_draft(env):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    assert len(draft['steps']) == 2
    assert [s['method'] for s in draft['steps']] == ['work/add', 'work/add']
    assert draft['check']['kind'] == 'added_works'
    assert [(i['title'], i['sum']) for i in draft['check']['items']] == [
        ('замена масла ДВС', 300.0), ('Замена салонного фильтра', 500.0)]
    assert '«замена масла ДВС» — 300 ₽' in draft['summary']
    assert '«Замена салонного фильтра» — 500 ₽' in draft['summary']
    assert not writes(session), 'до подтверждения в CRM ничего не пишем'
    assert tools.drafts.list('pending') and len(tools.drafts.list('pending')) == 1


def test_one_apply_adds_both_services(env):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    out = tools.apply_draft(draft['token'])
    assert out['status'] == 'applied' and out['verified'] is True
    rows = out['result']['WORKS']
    assert [row['TITLE'] for row in rows] == ['замена масла ДВС', 'Замена салонного фильтра']
    assert all(row['VERIFIED'] for row in rows)
    assert [row['SUM'] for row in rows] == [300.0, 500.0]
    assert len(writes(session)) == 2
    titles = {row['TITLE'] for row in works_of(session)}
    assert {'замена масла ДВС', 'Замена салонного фильтра'} <= titles
    # один черновик, одно применение — второй «Применить» уже не нужен
    assert tools.drafts.get(draft['token'])['status'] == 'applied'


def test_report_shows_each_service(env):
    _, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    out = tools.apply_draft(draft['token'])
    text = format_apply_result(out['method'], out['result'])
    assert 'замена масла ДВС' in text and '300 ₽' in text
    assert 'Замена салонного фильтра' in text and '500 ₽' in text
    assert 'сравнение работ сделки до и после записи' in text.lower()


def test_missing_second_service_is_partial_and_not_retried(env, monkeypatch):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    skip_second_write(tools, monkeypatch=monkeypatch)
    out = tools.apply_draft(draft['token'])
    assert out['status'] == 'partial' and out['verified'] is False
    assert 'Замена салонного фильтра' in out['warning']
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial' and saved['uncertain_network']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 1        # повторно ничего не отправлено


def test_baseline_read_error_stops_before_any_write(env, monkeypatch):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)

    def broken(offer_id, limit=200):
        raise StocrmError('CRM не ответила на чтение работ')

    monkeypatch.setattr(tools, 'offer_works', broken)
    with pytest.raises(StocrmError):
        tools.apply_draft(draft['token'])
    assert not writes(session)
    assert tools.drafts.get(draft['token'])['status'] == 'pending'


def test_service_that_already_existed_is_manual(env, monkeypatch):
    session, tools = env
    session.data['work/get_filtered']['DATA'].append({
        'WORK_ID': 900001, 'OFFER_ID': 1654, 'TITLE': 'Замена салонного фильтра',
        'SUM': '500.00', 'WORKING_HOUR': '1.000', 'PERFORMERS_COUNT': 0,
        'LOCKED': 'N', 'PERFORMERS': False,
    })
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    skip_second_write(tools, monkeypatch=monkeypatch)
    out = tools.apply_draft(draft['token'])
    assert out['status'] == 'partial'
    assert 'уже была до записи' in out['warning']
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    assert len(writes(session)) == 1


def test_unexpected_extra_row_is_manual(env, monkeypatch):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    skip_second_write(tools, monkeypatch=monkeypatch, extra_row={
        'WORK_ID': 900002, 'OFFER_ID': 1654, 'TITLE': 'Диагностика ходовая',
        'SUM': '0.00', 'WORKING_HOUR': '1.000', 'PERFORMERS_COUNT': 0,
        'LOCKED': 'N', 'PERFORMERS': False,
    })
    out = tools.apply_draft(draft['token'])
    assert out['status'] == 'partial'
    assert 'которых нет в заявке' in out['warning']
    assert len(writes(session)) == 1


def test_review_shows_each_service_after_restart(env, monkeypatch, tmp_path):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    skip_second_write(tools, monkeypatch=monkeypatch)
    tools.apply_draft(draft['token'])
    # «перезапуск»: новый Toolset читает тот же файл состояния
    fresh = Toolset(StocrmClient('demo', 'demo', session=session),
                    DraftStore(tools.drafts.path), AuditLog(tmp_path / 'audit2.jsonl'))
    review = fresh.review_draft(draft['token'])
    assert review['verified'] is False
    assert len(review['details']) == 2
    assert [row['title'] for row in review['details']] == [
        'замена масла ДВС', 'Замена салонного фильтра']
    assert review['details'][0]['verified'] is True
    assert review['details'][1]['verified'] is False
    assert review['can_resume'] is False


def test_concurrent_second_draft_waits_instead_of_unknown(env):
    session, tools = env
    first = tools.draft_add_offer_work(offer_id=1654, title='замена масла ДВС', sum_value=300.0)
    second = tools.draft_add_offer_work(offer_id=1654, title='Замена салонного фильтра', sum_value=500.0)
    # первая карточка «применяется прямо сейчас» — второе нажатие не должно пугать
    tools.drafts.mark(first['token'], 'applying', claimed_at=time.time())
    with pytest.raises(ValueError) as caught:
        tools.apply_draft(second['token'])
    message = str(caught.value)
    assert 'уже применяется другая запись' in message
    assert 'ничего не отправила' in message
    assert not writes(session)
    assert tools.drafts.get(second['token'])['status'] == 'pending'
    # первая запись завершилась — вторую можно применять
    tools.drafts.mark(first['token'], 'applied', applied_at=time.time())
    out = tools.apply_draft(second['token'])
    assert out['status'] == 'applied'
    assert len(writes(session)) == 1


def test_stale_applying_draft_stays_blocked(env):
    session, tools = env
    first = tools.draft_add_offer_work(offer_id=1654, title='замена масла ДВС', sum_value=300.0)
    second = tools.draft_add_offer_work(offer_id=1654, title='Замена салонного фильтра', sum_value=500.0)
    tools.drafts.mark(first['token'], 'applying', claimed_at=time.time() - 3600,
                      sent_step_started=True)
    with pytest.raises(ValueError) as caught:
        tools.apply_draft(second['token'])
    assert 'результат неизвестен' in str(caught.value)
    assert not writes(session)


def test_auto_mode_applies_one_draft_with_both_services(tmp_path):
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session),
                    DraftStore(tmp_path / 'drafts.json'), AuditLog(tmp_path / 'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path / 'p.json')))
    pipe.router.policy.requires_confirmation = lambda name: False
    result = asyncio.run(pipe.handle_text(
        'В сделку 1654 добавь услугу замена масла ДВС 300\nЗамена салонного фильтра 500', [], 19))
    assert result.applied and len(result.applied) == 1
    works = result.applied[0]['result']['WORKS']
    assert [row['TITLE'] for row in works] == ['замена масла ДВС', 'Замена салонного фильтра']
    assert len(writes(session)) == 2
    assert not result.drafts


def test_panel_gets_separate_requests_one_work_each(env):
    """Панель не умеет «все работы одной кучей»: на каждую услугу — свой work/add."""
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    tools.apply_draft(draft['token'])
    calls = writes(session)
    assert len(calls) == 2
    payloads = [call['payload']['DATA'] for call in calls]
    assert [p['TITLE'] for p in payloads] == ['замена масла ДВС', 'Замена салонного фильтра']
    assert [p['SUM'] for p in payloads] == [300.0, 500.0]
    for payload in payloads:
        assert isinstance(payload, dict) and isinstance(payload['OFFER_ID'], int)
        # ни списков работ, ни нескольких услуг в одном запросе
        assert not any(isinstance(value, (list, tuple)) for value in payload.values())


def test_rejected_second_request_names_each_service(env, monkeypatch):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    original = tools._call_step
    calls = {'n': 0}

    def step(step_row, payload, results=None):
        calls['n'] += 1
        if calls['n'] == 2:
            raise StocrmError('Неверные данные работы', method='work/add', code=400)
        return original(step_row, payload, results)

    monkeypatch.setattr(tools, '_call_step', step)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert '«замена масла ДВС» — 300 ₽: добавлена' in message
    assert '«Замена салонного фильтра» — 500 ₽: не добавлена' in message
    assert 'повтор этой заявки заблокирован' in message.lower()
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial'
    assert 'не добавлена' in saved['check_warning']
    assert saved['done_steps']['failed_index'] == 1
    assert len(writes(session)) == 1
    with pytest.raises(Exception):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 1


def test_lost_reply_on_second_request_keeps_result_unknown(env, monkeypatch):
    session, tools = env
    draft = tools.draft_add_offer_works(offer_id=1654, items=TWO_WORKS)
    original = tools._call_step
    calls = {'n': 0}

    def step(step_row, payload, results=None):
        calls['n'] += 1
        if calls['n'] == 2:
            raise ConnectionError('сетевая ошибка: ответ не получен')
        return original(step_row, payload, results)

    monkeypatch.setattr(tools, '_call_step', step)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert 'результат неизвестен' in message
    assert '«замена масла ДВС» — 300 ₽: добавлена' in message
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial' and saved['uncertain_network']
    assert len(writes(session)) == 1
