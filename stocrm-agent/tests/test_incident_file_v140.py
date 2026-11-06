"""Выжимка по присланному файлу и понятная причина отказа записи (волна 140).

Проверки выросли из разбора настоящего `support_log.txt` (06.11.2026, сделка №1371):
в нём 6,6 МБ и 1841 строка, а весь случай — это три события на 15:59. Чтобы такое
находилось за минуту, выжимка должна:

* читать **готовый экспорт** `support_log.txt`, а не только папку `.state`;
* понимать время так, как его писал журнал (на компьютере пользователя это +03:00);
* показывать `trace_id` и отпечаток черновика, иначе две записи не различить;
* по `write.error` объяснять причину отказа, а не `error_kind=other`.

Только локальные файлы: ни сети, ни CRM, ни `.state` живого бота.
"""
import datetime as dt
import json

import pytest

from stocrm_agent import incident
from stocrm_agent.audit import AuditLog
from stocrm_agent.client import StocrmClient
from stocrm_agent.diagnostic_journal import DiagnosticJournal, write_reason
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


def record(event, when, **fields):
    return json.dumps({'time': when, 'session': 'a' * 16, 'event': event, **fields},
                      ensure_ascii=False)


def stamp(second=0, hour=15, minute=59):
    return f'2026-11-06T{hour:02d}:{minute:02d}:{second:02d}.000+03:00'


# ——— выжимка по готовому экспорту ———

def exported_log(tmp_path):
    """Как настоящий `support_log.txt`: шапка текстом, дальше JSON построчно."""
    path = tmp_path / 'support_log.txt'
    path.write_text(
        '\ufeffSTOCRM: диагностический журнал\r\n'
        'Версия кода сборщика: 136 (2026-11-06)\r\n'
        'ВНИМАНИЕ: журнал содержит запросы и ответы CRM.\r\n'
        '=== СОБЫТИЯ (JSON, по одному на строку) ===\r\n'
        + '\r\n'.join([
            record('bot.draft', stamp(56), trace_id='req_021b4ea9c5af',
                   tool='add_offer_work', draft_fingerprint='449a3fb2ff3e'),
            record('bot.draft', stamp(56), trace_id='req_021b4ea9c5af',
                   tool='add_offer_work', draft_fingerprint='d97002bf44c1'),
            record('write.request', stamp(1), trace_id='apply_3d0bbf42a1e7',
                   draft_fingerprint='449a3fb2ff3e'),
            record('write.request', stamp(4), trace_id='apply_5dbddc112e8c',
                   draft_fingerprint='d97002bf44c1'),
            record('write.error', stamp(4), trace_id='apply_5dbddc112e8c',
                   draft_fingerprint='d97002bf44c1', error_kind='other', ms=11),
        ])
        + '\r\n=== ИТОГ СБОРА ===\r\n', encoding='utf-8')

    return path


def test_digest_reads_exported_file_and_skips_header(tmp_path):
    source = exported_log(tmp_path)
    target = tmp_path / 'incident.txt'
    code = incident.main(['--file', str(source), '--out', str(target),
                          '--from', '2026-11-06T15:50', '--to', '2026-11-06T16:05',
                          '--offer', '1371'])
    assert code == 0
    text = target.read_text(encoding='utf-8-sig')
    assert 'Файлы: support_log.txt' in text
    assert 'Прочитано событий: 5' in text
    assert 'write.error' in text
    # Шапка экспорта не считается событием и не попадает в разделы.
    assert 'Версия кода сборщика' not in text
    assert '=== СОБЫТИЯ' not in text


def test_window_is_read_in_the_journal_timezone(tmp_path):
    """«15:59» в логе — это 15:59 по поясу того компьютера, а не читателя файла."""
    source = exported_log(tmp_path)
    target = tmp_path / 'incident.txt'
    assert incident.main(['--file', str(source), '--out', str(target),
                          '--from', '2026-11-06T15:58', '--to', '2026-11-06T16:05']) == 0
    text = target.read_text(encoding='utf-8-sig')
    assert 'в окне: 5' in text
    assert 'apply_5dbddc112e8c' in text


def test_missing_file_is_reported_without_crash(tmp_path, capsys):
    code = incident.main(['--file', str(tmp_path / 'нет-такого.txt'), '--out',
                          str(tmp_path / 'x.txt')])
    assert code == 2
    assert 'Файл не найден' in capsys.readouterr().err


def test_lines_show_trace_and_draft_fingerprint(tmp_path):
    state = tmp_path / '.state'
    state.mkdir()
    (state / 'support-diagnostic.txt').write_text('\n'.join([
        record('write.error', stamp(4), trace_id='apply_5dbddc112e8c',
               draft_fingerprint='d97002bf44c1', error_kind='other'),
    ]) + '\n', encoding='utf-8')
    since = dt.datetime(2026, 11, 6, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    until = dt.datetime(2026, 11, 6, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    text, _ = incident.build_digest(state, since=since, until=until)
    assert 'trace=apply_5dbddc112e8c' in text
    assert 'draft=d97002bf44c1' in text


# ——— причина отказа записи ———

@pytest.mark.parametrize('message, expected', [
    ('Сейчас в этой сделке уже применяется другая запись. Подождите пару секунд',
     'write_busy'),
    ('Такая запись уже выполняется или её результат неизвестен. Проверьте CRM и /drafts; '
     'повтор заблокирован.', 'write_uncertain'),
    ('Заявка ещё собирается; применять отдельные позиции нельзя.', 'write_batch_preparing'),
    ('API умеет создавать заметки, но привязка к видимому блоку сделки не подтверждена.',
     'local_refusal'),
])
def test_write_reason_codes(message, expected):
    assert write_reason(ValueError(message)) == expected


def test_write_reason_defaults_to_crm_error_for_panel_failures():
    class StocrmError(Exception):
        pass

    assert write_reason(StocrmError('Пустой ответ панели')) == 'crm_error'


def test_journal_keeps_error_class_and_reason(tmp_path):
    journal = DiagnosticJournal(tmp_path / 'state')
    journal.audit('write.error', {'token': 'draft_token_123',
                                  'error': 'ValueError', 'error_class': 'ValueError',
                                  'reason': 'write_busy'})
    journal.audit('write.error', {'token': 'draft_token_123',
                                  'error': 'ValueError', 'reason': 'неизвестный код'})
    lines = [json.loads(line) for line in
             (tmp_path / 'state' / 'support-diagnostic.txt').read_text(encoding='utf-8').splitlines()]
    first, second = lines[-2], lines[-1]
    assert first['error_class'] == 'ValueError'
    assert first['reason'] == 'write_busy'
    # Неизвестное значение не проходит в журнал: только из белого списка.
    assert second['reason'] == 'other'


def test_blocked_apply_explains_itself_in_the_journal(tmp_path):
    """Реальный случай 1371: вторая карточка отклонена — в журнале видно, почему."""
    import time

    journal = DiagnosticJournal(tmp_path / 'state')
    tools = Toolset(StocrmClient('demo', 'demo', session=MockSession()),
                    DraftStore(tmp_path / 'drafts.json'),
                    AuditLog(tmp_path / 'audit.jsonl', diagnostic=journal))
    first = tools.draft_add_offer_work(offer_id=1654, title='замена масла ДВС', sum_value=300.0)
    second = tools.draft_add_offer_work(offer_id=1654, title='Замена салонного фильтра',
                                        sum_value=500.0)
    tools.drafts.mark(first['token'], 'applying', claimed_at=time.time())
    with pytest.raises(ValueError):
        tools.apply_draft(second['token'])

    lines = [json.loads(line) for line in
             (tmp_path / 'state' / 'support-diagnostic.txt').read_text(encoding='utf-8').splitlines()]
    errors = [row for row in lines if row.get('event') == 'write.error']
    assert len(errors) == 1
    assert errors[0]['error_class'] == 'ValueError'
    assert errors[0]['reason'] == 'write_busy'
    # Ничего не отправлено в панель: отказ случился до сети.
    assert not tools.client.session.calls
