"""Волна 136: подтверждение услуги сравнением работ «до/после», без опоры на ID.

Только локальная имитация CRM. Реальных записей нет; ключи, .env и .state
пользователя не читаются.
"""
import json

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_MOCK', '1')
    session = MockSession()
    store = DraftStore(tmp_path / 'drafts.json')
    tools = Toolset(StocrmClient('demo', 'demo', session=session), store,
                    AuditLog(tmp_path / 'audit.jsonl'))
    return session, tools, store


def work(tools, title='Замена свечей', price=2000, hours=1.0):
    return tools.draft_add_offer_work(offer_id=1654, title=title, hours=hours,
                                      sum_value=price)


def add_row(session, *, title='Замена свечей', total='2000.00', hours='1.000',
            work_id=4319, offer_id=1654):
    """Сымитировать запись услуги на «сервере» (локальные данные мока)."""
    session.data['work/get_filtered']['DATA'].append({
        'WORK_ID': work_id, 'OFFER_ID': offer_id, 'TITLE': title, 'TYPE': 'NORMAL',
        'SUM': total, 'WORKING_HOUR': hours, 'PERFORMERS_COUNT': 0,
        'LOCKED': 'N', 'PERFORMER': '', 'PERFORMERS': False,
    })


def test_numeric_panel_response_is_not_a_failure_when_new_row_appears(env, monkeypatch):
    """Случай 06.11 09:31: work/add вернул 4319 (число), услуга в CRM появилась."""
    session, tools, _ = env
    draft = work(tools)

    def numeric_add(step, payload, results):
        add_row(session)
        return 4319                       # панель ответила числом, без WORK_ID

    monkeypatch.setattr(tools, '_call_step', numeric_add)
    result = tools.apply_draft(draft['token'])

    assert result['status'] == 'applied' and result['verified'] is True
    shown = result['result']
    assert shown['TITLE'] == 'Замена свечей' and shown['SUM'] == 2000
    assert shown['OFFER_ID'] == 1654 and shown['PANEL_RESPONSE'] == 4319
    # Номер из ответа — только справка: подтверждение построено на сравнении.
    assert 'WORK_ID' not in shown
    assert tools.drafts.get(draft['token'])['status'] == 'applied'
    # Снимок «до записи» сохранён в состоянии и переживёт перезапуск.
    saved = json.loads(tools.drafts.path.read_text(encoding='utf-8'))
    assert isinstance(saved[draft['token']]['work_baseline'], list)


def test_numeric_response_without_new_row_is_not_confirmed(env, monkeypatch):
    """Число в ответе ничего не доказывает: новой строки нет — ручная проверка."""
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: 4319)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'не видно' in result['warning']
    assert 'Повтор' in result['warning'] or 'повтор' in result['warning']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])          # повтор запрещён
    with pytest.raises(ValueError):
        work(tools)                                # новый черновик тоже блокирован


def test_old_identical_row_does_not_confirm_a_new_record(env, monkeypatch):
    """Даже с WORK_ID в ответе старая совпавшая строка не считается новой услугой."""
    _, tools, _ = env
    draft = work(tools)
    before_and_after = [{'title': 'Замена свечей', 'sum': 2000, 'hours': '1.000'}]
    monkeypatch.setattr(tools, '_call_step', lambda *a: {'WORK_ID': 4319})
    monkeypatch.setattr(tools, 'offer_works', lambda *a, **kw: list(before_and_after))
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'уже была до записи' in result['warning']


def test_work_ids_are_ignored_by_the_comparison(env, monkeypatch):
    """Тот же набор работ с другими ID — это не новая строка (и не доказательство)."""
    _, tools, _ = env
    draft = work(tools)
    monkeypatch.setattr(tools, '_call_step', lambda *a: {'WORK_ID': 999999})
    monkeypatch.setattr(tools, 'offer_works',
                        lambda *a, **kw: [{'work_id': 4319, 'title': 'Замена свечей',
                                           'sum': 2000, 'hours': '1.000'}])
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'уже была до записи' in result['warning']


def test_two_new_matching_rows_are_ambiguous(env, monkeypatch):
    session, tools, _ = env
    draft = work(tools)

    def two_rows(step, payload, results):
        add_row(session, work_id=5001)
        add_row(session, work_id=5002)
        return 5002

    monkeypatch.setattr(tools, '_call_step', two_rows)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'несколько подходящих строк' in result['warning']


def test_panel_sum_differs_from_approved_draft(env, monkeypatch):
    session, tools, _ = env
    draft = work(tools)

    def other_sum(step, payload, results):
        add_row(session, total='2500.00')
        return 4319

    monkeypatch.setattr(tools, '_call_step', other_sum)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'сумма в CRM' in result['warning'] and '2 500' in result['warning']
    assert result['result']['SUM'] == 2500


def test_panel_hours_differ_from_approved_draft(env, monkeypatch):
    session, tools, _ = env
    draft = work(tools, hours=1.0)

    def other_hours(step, payload, results):
        add_row(session, hours='2.000')
        return 4319

    monkeypatch.setattr(tools, '_call_step', other_hours)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'нормо-часы' in result['warning']


def test_missing_sum_in_readback_keeps_manual_review(env, monkeypatch):
    session, tools, _ = env
    draft = work(tools)

    def no_sum(step, payload, results):
        add_row(session, total=None)
        return 4319

    monkeypatch.setattr(tools, '_call_step', no_sum)
    result = tools.apply_draft(draft['token'])
    assert result['status'] == 'partial' and result['verified'] is False
    assert 'не вернул сумму' in result['warning']


def test_unreadable_baseline_stops_before_any_write(env, monkeypatch):
    """Нельзя сравнить «до/после» — запись не начинается вовсе."""
    session, tools, _ = env
    draft = work(tools)
    writes = []
    monkeypatch.setattr(tools, '_call_step',
                        lambda *a: writes.append('write') or 4319)
    monkeypatch.setattr(tools, 'offer_works',
                        lambda *a, **kw: (_ for _ in ()).throw(StocrmError('network down')))
    with pytest.raises((StocrmError, ValueError), match='Не удалось прочитать работы'):
        tools.apply_draft(draft['token'])
    assert writes == []
    state = tools.drafts.get(draft['token'])
    assert state['status'] == 'pending' and not state.get('sent_step_started')


def test_baseline_survives_restart_and_review_confirms_without_writing(env, monkeypatch, tmp_path):
    """Ответ потерян после записи: после перезапуска проверка идёт по сохранённому снимку."""
    session, tools, store = env
    draft = work(tools)
    writes = []

    def lost_response(step, payload, results):
        writes.append(step['method'])
        add_row(session)
        raise StocrmError('сетевая ошибка: connection reset')

    monkeypatch.setattr(tools, '_call_step', lost_response)
    with pytest.raises(StocrmError):
        tools.apply_draft(draft['token'])
    assert tools.drafts.get(draft['token'])['status'] == 'partial'

    # «Перезапуск»: новый объект хранилища читает тот же файл состояния.
    restarted = DraftStore(tmp_path / 'drafts.json')
    tools2 = Toolset(StocrmClient('demo', 'demo', session=session), restarted,
                     AuditLog(tmp_path / 'audit.jsonl'))
    assert restarted.get(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError):
        tools2.apply_draft(draft['token'])          # повтора записи нет

    review = tools2.review_draft(draft['token'])
    assert review['verified'] is True and review['can_resume'] is False
    assert review['details'][0]['title'] == 'Замена свечей'
    assert review['details'][0]['actual'].startswith('2000')
    confirmed = tools2.confirm_draft_result(draft['token'])
    assert confirmed['status'] == 'applied' and confirmed['review']['verified'] is True
    assert writes == ['work/add']


def test_legacy_partial_without_baseline_stays_for_manual_review(env, tmp_path):
    """Старый partial услуги (код 135) нельзя ни повторить, ни «подтвердить» вслепую."""
    _, tools, _ = env
    draft = work(tools)
    tools.drafts.claim(draft['token'])
    tools.drafts.mark(draft['token'], 'partial', failed_at=1.0,
                      check_warning='CRM не вернула номер созданной услуги')
    restarted = DraftStore(tmp_path / 'drafts.json')
    tools2 = Toolset(StocrmClient('demo', 'demo', session=MockSession()), restarted,
                     AuditLog(tmp_path / 'audit.jsonl'))
    assert restarted.get(draft['token'])['status'] == 'partial'
    with pytest.raises(ValueError):
        tools2.apply_draft(draft['token'])
    review = tools2.review_draft(draft['token'])
    assert review['verified'] is False and review['can_resume'] is False
    assert 'не сохранился' in review['problem']
    with pytest.raises(ValueError):
        tools2.confirm_draft_result(draft['token'])
