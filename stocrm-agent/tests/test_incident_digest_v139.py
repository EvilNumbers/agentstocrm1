"""Компактная выжимка журнала по инциденту (волна 139).

Полный журнал — это все события за всё время. Для разбора одного случая нужен
маленький файл: окно времени, ошибки, записи и всё по одной сделке. Проверки ниже
фиксируют, что выжимка действительно узкая, ничего не отправляет в сеть и не
вытаскивает посторонние секреты.
"""
import datetime as dt
import json

import pytest

from stocrm_agent import incident


def record(event, when, **fields):
    return json.dumps({'time': when, 'session': 'a' * 16, 'event': event, **fields},
                      ensure_ascii=False)


def write_journal(tmp_path, lines):
    state = tmp_path / '.state'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'support-diagnostic.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return state


STAMP = '2026-11-06T15:{minute}:00.000+03:00'


def stamp(minute, second=0, hour=15):
    return f'2026-11-06T{hour:02d}:{minute:02d}:{second:02d}.000+03:00'


def tz(day=6, hour=15, minute=0, second=0):
    return dt.datetime(2026, 11, day, hour, minute, second,
                       tzinfo=dt.timezone(dt.timedelta(hours=3)))


def test_digest_keeps_only_the_window(tmp_path):
    state = write_journal(tmp_path, [
        record('write.error', stamp(10)),
        record('write.error', stamp(45)),
        record('write.error', stamp(5, hour=16)),
    ])
    text, stats = incident.build_digest(state, since=tz(minute=40), until=tz(minute=59, second=59))
    assert stats['errors'] == 1 and stats['window'] == 1
    assert '15:45' in text and '15:10' not in text and '16:05' not in text


def test_errors_and_writes_are_separate_and_ordered(tmp_path):
    state = write_journal(tmp_path, [
        record('bot.apply', stamp(50), tool='add_offer_work', status='applied'),
        record('write.request', stamp(50), tool='add_offer_work'),
        record('write.partial', stamp(51), tool='add_offer_work', step_index=1, failed_index=1),
        record('crm.call.finish', stamp(52), method='work/add', ms=240),
        record('crm.call.finish', stamp(52, 1), method='work/add', ms=5400),
    ])
    text, stats = incident.build_digest(state, since=tz(minute=45), until=tz(minute=59))
    # write.partial — и событие записи, и проблема: он попадает в оба раздела
    assert stats['writes'] == 3 and stats['errors'] == 1
    errors = text.split('1. ОШИБКИ И НЕЗАВЕРШЁННЫЕ ЗАПИСИ (1)')[1].split('2. ЧТО УХОДИЛО')[0]
    assert 'write.partial' in errors and 'bot.apply' not in errors
    writes = text.split('2. ЧТО УХОДИЛО В ПАНЕЛЬ И ЧЕМ ЗАКОНЧИЛОСЬ (3)')[1].split('4. САМЫЕ МЕДЛЕННЫЕ')[0]
    assert writes.index('bot.apply') < writes.index('write.request')
    slow = text.split('4. САМЫЕ МЕДЛЕННЫЕ ВЫЗОВЫ CRM')[1]
    assert slow.index('5400мс') < slow.index('240мс')


def test_offer_section_uses_number_and_tool_payloads(tmp_path):
    state = write_journal(tmp_path, [
        record('write.applied', stamp(50), tool='add_offer_work', offer_id=1371),
        record('write.applied', stamp(50), tool='add_offer_work', offer_id=2000),
        record('bot.tool.detail', stamp(50), tool='offer_card',
               request_data={'value': {'OFFER_ID': 1371}, 'truncated': False}),
    ])
    text, stats = incident.build_digest(state, since=tz(minute=45), until=tz(minute=59),
                                        offer_id=1371)
    assert stats['related'] == 2
    section = text.split('3. ВСЁ ПО СДЕЛКЕ №1371 (2)')[1].split('4. САМЫЕ МЕДЛЕННЫЕ')[0]
    assert 'offer_card' in section and 'write.applied' in section


def test_bad_lines_are_skipped_not_fatal(tmp_path):
    state = write_journal(tmp_path, [
        'не json',
        json.dumps({'time': 'вчера', 'session': 'a' * 16, 'event': 'write.error'}),
        record('write.error', stamp(50)),
    ])
    text, stats = incident.build_digest(state, since=tz(minute=45), until=tz(minute=59))
    assert stats['records'] == 1 and stats['errors'] == 1


def test_secrets_and_unknown_fields_are_not_exported(tmp_path):
    state = write_journal(tmp_path, [
        record('write.error', stamp(50), token='1234567890abcdefSECRET', sid='TOPSECRETSID'),
    ])
    text, _ = incident.build_digest(state, since=tz(minute=45), until=tz(minute=59))
    assert 'TOPSECRET' not in text and '1234567890abcdef' not in text


@pytest.mark.parametrize('value,expected', [
    ('15:55', dt.datetime(2026, 11, 6, 15, 55)),
    ('06.11 15:55', dt.datetime(2026, 11, 6, 15, 55)),
    ('2026-11-06T15:55', dt.datetime(2026, 11, 6, 15, 55)),
])
def test_parse_moment_formats(value, expected):
    now = dt.datetime(2026, 11, 6, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    parsed = incident.parse_moment(value, now)
    assert parsed.replace(tzinfo=None) == expected


def test_parse_moment_night_time_goes_to_yesterday():
    now = dt.datetime(2026, 11, 6, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    parsed = incident.parse_moment('23:50', now)
    assert parsed.date() == dt.date(2026, 11, 5) and parsed.hour == 23


def test_parse_moment_rejects_garbage():
    with pytest.raises(ValueError):
        incident.parse_moment('вчера вечером')


def test_cli_writes_small_file(tmp_path, monkeypatch, capsys):
    state = write_journal(tmp_path, [record('write.error', stamp(50)) for _ in range(50)])
    monkeypatch.chdir(tmp_path)
    out = tmp_path / 'incident.txt'
    code = incident.main(['--state', str(state), '--out', str(out), '--minutes', '600'])
    assert code == 0
    assert out.exists() and out.stat().st_size < 20000
    printed = capsys.readouterr().out
    assert 'Готово' in printed and 'в CRM ничего не отправлялось' in printed


def test_cli_rejects_bad_window(tmp_path):
    state = write_journal(tmp_path, [record('write.error', stamp(50))])
    code = incident.main(['--state', str(state), '--out', str(tmp_path / 'x.txt'),
                          '--from', '16:00', '--to', '15:00'])
    assert code == 2


def test_empty_journal_is_reported_not_crashing(tmp_path, capsys):
    state = tmp_path / '.state'
    state.mkdir()
    code = incident.main(['--state', str(state), '--out', str(tmp_path / 'i.txt'), '--minutes', '5'])
    assert code == 0
    assert 'ВНИМАНИЕ' in capsys.readouterr().out
