"""Support log is local, bounded, safe by construction, and never changes writes."""
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
import requests

from stocrm_agent.audit import AuditLog
from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.diagnostic_journal import (BACKUPS, FILE_NAME, DiagnosticJournal,
                                               DiagnosticLogHandler, shape, write_text_log)
from stocrm_agent.diagnostics import TRACE_ID
from stocrm_agent.mock import MockSession
from test_quick_deal_v100 import env, send, writes  # noqa: F401


def records(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def exported(path):
    text = path.read_text(encoding='utf-8-sig')
    body = text.split('=== СОБЫТИЯ (JSON, по одному на строку) ===\n', 1)[1].split('\n=== ИТОГ СБОРА ===', 1)[0]
    return [json.loads(line) for line in body.splitlines() if line], text



def test_never_export_raw_inputs_secrets_old_logs_or_untrusted_fields(tmp_path):
    state = tmp_path / '.state'
    journal = DiagnosticJournal(state)
    secret = 'sk-SuperSecretExample123456'
    phone = '89005554433'
    message = 'Иван секретная работа'
    (tmp_path / '.env').write_text('STOCRM_SID=' + secret)
    (state / 'audit.jsonl').write_text(secret + phone + message)
    (state / 'bot.log').write_text(secret + phone + message)
    mark = TRACE_ID.set('req_012345abcdef')
    try:
        journal.emit('crm.call.start', method='work/add', request={'SID': secret, 'PHONE': phone,
                     'DATA': {'OFFER_ID': 345, 'TITLE': message, 'PRICE': 1000, 'BRAND': secret},
                     'CUSTOM_KEY': secret}, error_class=secret, tool=secret)
        journal.audit('write.error', {'token': 'draft_sensitive_token', 'error': secret, 'payload': {'NAME': message}})
    finally:
        TRACE_ID.reset(mark)
    # Even a malformed or tampered on-disk entry is filtered again at export.
    with journal.path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': '2026-01-01T00:00:00+00:00', 'event': 'write.error',
                                 'session': '1234567890abcdef', 'error': secret, 'method': 'work/' + secret,
                                 'tool': secret, 'response_shape': {'type': 'object', 'nodes': 1,
                                 'fields': {'NAME': {'count': 1, 'types': ['string'], 'samples': [secret]},
                                            secret: {'count': 1, 'types': ['string'], 'samples': [secret]}}}}) + '\n')
        stream.write('{broken json ' + secret + '\n')
    target = tmp_path / 'support_log.txt'
    result = write_text_log(state, target)
    data, note = exported(target)
    content = json.dumps(data, ensure_ascii=False) + note
    for sensitive in (secret, phone, message, 'draft_sensitive_token'):
        assert sensitive not in content
        assert sensitive not in journal.path.read_text(encoding='utf-8').split('{broken json')[0].splitlines()[0]
    assert result['skipped'] == 1
    assert data[0]['trace_id'] == 'req_012345abcdef'
    fields = data[0]['request_shape']['fields']
    assert fields['OFFER_ID']['samples'] == [345]
    assert fields['PRICE']['samples'] == [1000]
    assert len(fields['TITLE']['samples'][0]) == 12
    assert 'SID' not in fields and 'PHONE' not in fields and 'CUSTOM_KEY' not in fields
    assert data[-1]['method'] == 'other' and data[-1]['tool'] == 'other'
    assert data[-1]['response_shape']['fields']['NAME']['samples'] == []


def test_hmac_is_stable_in_one_session_but_not_recoverable_across_sessions(tmp_path):
    a = DiagnosticJournal(tmp_path/'a')
    b = DiagnosticJournal(tmp_path/'b')
    a.emit('crm.call.start', request={'TITLE': 'Диагностика'})
    a.emit('crm.call.finish', response={'TITLE': 'диагностика'})
    b.emit('crm.call.start', request={'TITLE': 'диагностика'})
    first, second = records(a.path)
    assert first['request_shape']['fields']['TITLE']['samples'] == second['response_shape']['fields']['TITLE']['samples']
    assert records(b.path)[0]['request_shape']['fields']['TITLE']['samples'] != first['request_shape']['fields']['TITLE']['samples']


def test_rotation_is_bounded_and_oldest_records_expire(tmp_path):
    state = tmp_path/'state'
    journal = DiagnosticJournal(state, max_bytes=1024, backups=2)
    for index in range(100):
        journal.emit('bot.request.start', input_chars=index, category='quick')
    files = sorted(state.glob(FILE_NAME + '*'))
    assert len(files) == 3
    assert all(file.stat().st_size <= 1024 for file in files)
    target = tmp_path/'bundle.txt'
    result = write_text_log(state, target)
    events, note = exported(target)
    assert 0 < len(events) < 100 and result['records'] == len(events)
    assert events[-1]['input_chars'] == 99
    assert 'ограничена' in note


def test_collect_while_bot_writes_and_rotates(tmp_path):
    journal = DiagnosticJournal(tmp_path/'state', max_bytes=2048, backups=BACKUPS)
    begun = threading.Event()
    def writer():
        for i in range(700):
            journal.emit('bot.request.start', input_chars=i)
            begun.set()
    thread = threading.Thread(target=writer)
    thread.start()
    begun.wait(3)
    for i in range(5):
        target = tmp_path / f'{i}.txt'
        write_text_log(journal.state, target)
        events, _ = exported(target)
        assert all(event['event'] == 'bot.request.start' for event in events)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert records(journal.path)


def test_existing_state_empty_or_bad_lines_and_relative_output(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('STOCRM_STATE_DIR', 'my-state')
    from stocrm_agent.cli import main
    assert main(['support-log']) == 0
    assert (tmp_path/'support_log.txt').exists()
    assert 'событий пока нет' in capsys.readouterr().out.lower()
    events, note = exported(tmp_path/'support_log.txt')
    assert not events and 'событий: 0' in note


def test_startup_error_before_telegram_is_logged(tmp_path):
    envvars = os.environ.copy()
    envvars.update({'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                    'STOCRM_ENV_FILE': str(tmp_path/'does-not-exist'),
                    'STOCRM_STATE_DIR': str(tmp_path/'state'), 'STOCRM_DOMAIN': '', 'STOCRM_SID': '',
                    'STOCRM_MOCK': '0', 'TG_BOT_TOKEN': ''})
    result = subprocess.run([sys.executable, '-m', 'stocrm_agent.bot'], env=envvars, cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    events = records(tmp_path/'state'/FILE_NAME)
    assert [e['event'] for e in events][:2] == ['runtime.start', 'runtime.start_error']
    assert events[0]['session'] == events[1]['session']


def test_error_logging_does_not_include_exception_text(tmp_path):
    journal = DiagnosticJournal(tmp_path)
    logger = logging.getLogger('stocrm.bot')
    handler = DiagnosticLogHandler(journal)
    logger.addHandler(handler)
    try:
        try:
            raise ValueError('пароль: TopSecret55 и телефон 89000000000')
        except ValueError:
            logger.exception('пароль TopSecret55')
    finally:
        logger.removeHandler(handler)
    content = journal.path.read_text(encoding='utf-8')
    assert 'TopSecret55' not in content and '89000000000' not in content
    event = json.loads(content)
    assert event['event'] == 'runtime.warning' and event['error_class'] == 'ValueError'
    assert event['step_index'] > 0


def test_instrumentation_failure_does_not_prevent_crm_call(tmp_path):
    class Broken:
        def emit(self, *a, **kw):
            raise RuntimeError('disk failed')
    session = MockSession()
    client = StocrmClient('demo', 'secret', session=session)
    client.diagnostic = Broken()
    result = client.call('boards/get_all', {}, idempotent=True)
    assert result is not None
    assert len(session.calls) == 1


def test_write_response_lost_logs_one_send_and_partial_trace(env, tmp_path, monkeypatch):
    session, tools, pipe = env
    journal = DiagnosticJournal(tmp_path/'state')
    tools.audit.diagnostic = journal
    tools.client.diagnostic = journal
    selected = send(pipe, 'Добавь быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    token = selected['token']
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if url.endswith('/work/add'):
            raise requests.Timeout('пароль key-secret ответ потерян')
        return response
    monkeypatch.setattr(session, 'post', post)
    result = tools.apply_draft(token)
    assert result['status'] == 'partial'
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change', 'work/add']
    events = records(journal.path)
    draft_fp = hashlib.sha256(token.encode()).hexdigest()[:12]
    created = next(e for e in events if e['event'] == 'quick.draft')
    attempted = next(e for e in events if e['event'] == 'write.request')
    partial = next(e for e in events if e['event'] == 'quick.partial')
    error = next(e for e in events if e['event'] == 'crm.call.error' and e['method'] == 'work/add')
    assert created['draft_fingerprint'] == attempted['draft_fingerprint'] == partial['draft_fingerprint'] == draft_fp
    assert created['trace_id'].startswith('req_')
    assert attempted['trace_id'] == partial['trace_id'] == error['trace_id']
    assert partial['phase'] == 'work_sent' and partial['status'] == 'partial'
    assert error['error_kind'] == 'timeout' and error['cause_class'] == 'Timeout'
    assert len([e for e in events if e['event'] == 'crm.call.start' and e['method'] == 'work/add']) == 1
    with pytest.raises(ValueError):
        tools.apply_draft(token)
    assert len([c for c in writes(session) if c['method'] == 'work/add']) == 1
    assert 'key-secret' not in journal.path.read_text(encoding='utf-8')


def test_non_json_write_error_does_not_retry_and_has_http_status(tmp_path):
    from stocrm_agent.mock import MockResponse
    session = MockSession()
    class BadResponse(MockResponse):
        def __init__(self, *args):
            super().__init__(*args)
            self.text = '<html>secret</html>'
        def json(self):
            raise ValueError('secret')
    calls = []
    def post(url, json=None, timeout=None):
        calls.append(url)
        return BadResponse({}, 200)
    session.post = post
    journal = DiagnosticJournal(tmp_path)
    client = StocrmClient('demo', 'secret', session=session)
    client.diagnostic = journal
    with pytest.raises(StocrmError):
        client.call('work/add', {'DATA': {'TITLE': 'secret'}})
    assert len(calls) == 1
    events = records(journal.path)
    assert any(e['event'] == 'crm.http' and e['http_status'] == 200 for e in events)
    assert any(e['event'] == 'crm.call.error' and e['method'] == 'work/add' for e in events)
    assert 'secret' not in journal.path.read_text(encoding='utf-8')


def test_export_handles_hostile_numbers_and_deep_shape(tmp_path):
    journal = DiagnosticJournal(tmp_path/'state')
    journal.emit('bot.request.start', input_chars=10**400)
    sample = {'DATA': {'DATA': {'DATA': {'TITLE': 'private'}}}}
    assert shape(sample, b'x', limit=2)['truncated']
    result = write_text_log(tmp_path/'state', tmp_path/'support.txt')
    assert result['records'] == 1
    assert 'input_chars' not in exported(tmp_path/'support.txt')[0][0]


def test_parallel_calls_have_unique_correlation_ids(tmp_path):
    journal = DiagnosticJournal(tmp_path)
    session = MockSession()
    client = StocrmClient('demo', 'secret', session=session)
    client.diagnostic = journal
    client.call('boards/get_all', {}, idempotent=True)
    client.call('boards/get_all', {}, idempotent=True)
    events = records(journal.path)
    starts = [e for e in events if e['event'] == 'crm.call.start']
    finishes = [e for e in events if e['event'] == 'crm.call.finish']
    assert len(starts) == len(finishes) == 2
    ids = {e['call_id'] for e in starts}
    assert len(ids) == 2 and ids == {e['call_id'] for e in finishes}
    assert {e['call_id'] for e in events if e['event'] == 'crm.http'} == ids


def test_successful_quick_deal_logs_checks_and_response_structure(env, tmp_path):
    session, tools, pipe = env
    journal = DiagnosticJournal(tmp_path/'state')
    tools.audit.diagnostic = journal
    tools.client.diagnostic = journal
    selected = send(pipe, 'Добавь быструю сделку диагностика 1000 рублей и добавь артикул NGK CR9E цена 300').drafts[0]
    result = tools.apply_draft(selected['token'])
    assert result['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change',
                                                     'work/add', 'offer/require/add', 'offer/order/add_free_order']
    events = records(journal.path)
    checks = [event for event in events if event['event'] == 'quick.check']
    assert any(e['phase'] == 'work_check' and e['verified'] and e['added_count'] == 1 for e in checks)
    assert any(e['phase'] == 'order_check' and e['verified'] for e in checks)
    assert any(e['event'] == 'quick.success' and e['completed_count'] == 4 for e in events)
    work_reply = next(e for e in events if e['event'] == 'crm.call.finish' and e['method'] == 'work/add')
    assert work_reply['response_shape']['type'] == 'object'
    assert 'WORK_ID' in work_reply['response_shape']['fields']
    # v116 now intentionally records the actual CRM request and response, including item data.
    assert 'диагностика' in journal.path.read_text(encoding='utf-8').lower()
    assert 'CR9E' in journal.path.read_text(encoding='utf-8')


def test_export_cannot_overwrite_state_or_non_txt_files(tmp_path):
    state = tmp_path/'state'
    journal = DiagnosticJournal(state)
    draft = state/'drafts.json'
    draft.write_text('important state')
    with pytest.raises(ValueError):
        write_text_log(state, draft)
    with pytest.raises(ValueError):
        write_text_log(state, state/'journal.txt')
    with pytest.raises(ValueError):
        write_text_log(state, tmp_path/'.env')
    assert draft.read_text() == 'important state' and journal.path.is_file()


def test_text_export_is_not_an_archive_and_has_windows_encoding(tmp_path):
    state = tmp_path/'state'
    journal = DiagnosticJournal(state)
    journal.emit('bot.request.start', input_chars=7)
    target = tmp_path/'support_log.txt'
    write_text_log(state, target)
    raw = target.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf') and b'\r\n' in raw
    assert not zipfile.is_zipfile(target)
    events, text = exported(target)
    assert len(events) == 1 and 'СТОCRM: диагностический журнал' not in text  # Latin STOCRM
    assert 'STOCRM: диагностический журнал' in text and '=== ИТОГ СБОРА ===' in text
    assert 'Версия кода сборщика: 121' in text


def test_failed_replace_preserves_previous_output_and_state(tmp_path, monkeypatch):
    state = tmp_path/'state'
    journal = DiagnosticJournal(state)
    journal.emit('bot.request.start', input_chars=7)
    target = tmp_path/'support_log.txt'
    target.write_text('previous result', encoding='utf-8')
    def fail_replace(*_args):
        raise OSError('disk error')
    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError):
        write_text_log(state, target)
    assert target.read_text(encoding='utf-8') == 'previous result'
    assert journal.path.is_file()
    assert not list(tmp_path.glob('.support-log-*.tmp'))
