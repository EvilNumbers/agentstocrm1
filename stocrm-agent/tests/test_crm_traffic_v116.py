"""Live local CRM traffic, with credential redaction and no change to requests."""
import json
from pathlib import Path

import pytest
import requests

from stocrm_agent.client import StocrmClient, StocrmError
from stocrm_agent.diagnostic_journal import DiagnosticJournal, redact_detail, write_text_log
from stocrm_agent.mock import MockResponse, MockSession


def events(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding='utf-8').splitlines()]


def test_post_live_request_and_response_include_details_but_not_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('TG_BOT_TOKEN', '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd')
    sid = 'sid-super-secret-value'
    session = MockSession()
    sent = []
    returned = {'CODE': 200, 'MESSAGE': 'ok', 'RESPONSE': {'OFFER_ID': 1333, 'NAME': 'Иван Петров',
                'PHONE': '89005554433', 'TITLE': 'Диагностика', 'SUM': 1000,
                'SID': sid, 'TOKEN': 'other-secret-123', 'nested': {'API_KEY': 'another-secret'}}}
    def post(url, json=None, timeout=None):
        sent.append((url, json))
        return MockResponse(returned, 200)
    session.post = post
    journal = DiagnosticJournal(tmp_path/'state')
    client = StocrmClient('demo', sid, session=session)
    client.diagnostic = journal
    result = client.call('work/add', {'DATA': {'OFFER_ID': 1333, 'TITLE': 'Диагностика', 'SUM': 1000}}, idempotent=False)
    assert result is returned['RESPONSE'] and result['SID'] == sid  # never mutate the actual response
    assert sent[0][1]['SID'] == sid  # network receives its real credential, not the redacted copy
    data = events(journal)
    request = next(e for e in data if e['event'] == 'crm.http.request')
    response = next(e for e in data if e['event'] == 'crm.http' and 'response_data' in e)
    assert request['call_id'] == response['call_id']
    assert request['method'] == response['method'] == 'work/add'
    assert request['request_data']['value']['DATA']['TITLE'] == 'Диагностика'
    assert request['request_data']['value']['SID'] == '[СКРЫТО]'
    panel = response['response_data']['value']['RESPONSE']
    assert panel['NAME'] == 'Иван Петров' and panel['PHONE'] == '89005554433'
    assert panel['SUM'] == 1000 and panel['SID'] == panel['TOKEN'] == '[СКРЫТО]'
    assert panel['nested']['API_KEY'] == '[СКРЫТО]'
    output = tmp_path/'support_log.txt'
    write_text_log(journal.state, output)
    text = output.read_text(encoding='utf-8-sig')
    assert 'Иван Петров' in text and '89005554433' in text and 'Диагностика' in text
    for secret in (sid, 'other-secret-123', 'another-secret', '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd'):
        assert secret not in text and secret not in journal.path.read_text(encoding='utf-8')
    assert 'персональные' in text


def test_get_pair_sid_is_masked_and_http_failure_body_is_recorded(tmp_path):
    sid = 'very-secret-sid'
    session = MockSession()
    session.get = lambda *args, **kwargs: MockResponse({'CODE': 200, 'RESPONSE': {'NAME': 'Иван'}}, 200)
    journal = DiagnosticJournal(tmp_path/'state')
    client = StocrmClient('demo', sid, session=session)
    client.diagnostic = journal
    client.call_get('work/get_filtered', {'FILTER': {'OFFER_ID': 1333}}, idempotent=True)
    req = next(e for e in events(journal) if e['event'] == 'crm.http.request')
    assert ['SID', '[СКРЫТО]'] in req['request_data']['value']
    assert ['FILTER[OFFER_ID]', 1333] in req['request_data']['value']
    assert any(e['event'] == 'crm.http' and e.get('response_data', {}).get('value', {}).get('RESPONSE', {}).get('NAME') == 'Иван' for e in events(journal))

    calls = []
    def bad_post(url, json=None, timeout=None):
        calls.append(url)
        return MockResponse({'MESSAGE': 'Карточка заблокирована', 'TOKEN': 'server-token'}, 503)
    session.post = bad_post
    with pytest.raises(RuntimeError):  # MockResponse raises RuntimeError for HTTP 500+
        client.call('work/add', {'DATA': {'TITLE': 'Диагностика'}}, idempotent=False)
    assert len(calls) == 1
    body = next(e for e in events(journal) if e['event'] == 'crm.http.error_body')
    assert body['http_status'] == 503
    assert body['response_data']['value']['MESSAGE'] == 'Карточка заблокирована'
    assert body['response_data']['value']['TOKEN'] == '[СКРЫТО]'
    assert sid not in journal.path.read_text(encoding='utf-8')


def test_secret_redaction_handles_unknown_embedded_credentials_and_long_values():
    known = 'x' * 6000
    payload = {'SID': 'not-in-env', 'ACCESS_TOKEN': 'unknown token', 'PRIVATE_KEY': 'private',
               'MESSAGE': 'Authorization: Bearer abcdef12345; password="very secret word"; '
                          'https://username:proxypass@panel/?api_key=abc123',
               'CONTENT': known + ' trailing data', 'FILTER[SID][0]': 'bad',
               'items': [('SID', 'not-in-env'), ('OFFER_ID', 1333)]}
    cleaned, truncated = redact_detail(payload, {known})
    text = json.dumps(cleaned, ensure_ascii=False)
    for secret in ('not-in-env', 'unknown token', 'private', 'abcdef12345', 'very secret word',
                   'username', 'proxypass', 'abc123', 'bad', known[:100]):
        assert secret not in text
    assert cleaned['items'][1] == ['OFFER_ID', 1333]
    assert truncated is False  # known secret was removed before limiting the string


def test_large_response_marked_truncated_and_history_is_immediately_visible(tmp_path):
    journal = DiagnosticJournal(tmp_path/'state')
    huge = {'RESPONSE': [{'NAME': 'проверка' * 100, 'OFFER_ID': i} for i in range(300)]}
    journal.emit('crm.http', method='work/get_filtered', response=huge)
    before_export = events(journal)
    assert len(before_export) == 1 and before_export[0]['response_data']['truncated'] is True
    write_text_log(journal.state, tmp_path/'support_log.txt')
    assert '"truncated":true' in (tmp_path/'support_log.txt').read_text(encoding='utf-8-sig')


def test_timeout_after_send_logs_request_error_and_never_retries_write(tmp_path):
    session = MockSession()
    attempts = []
    def post(url, json=None, timeout=None):
        attempts.append(json)
        raise requests.Timeout('response lost')
    session.post = post
    journal = DiagnosticJournal(tmp_path/'state')
    client = StocrmClient('demo', 'sid-secret123', session=session, retries=4)
    client.diagnostic = journal
    with pytest.raises(StocrmError):
        client.call('work/add', {'DATA': {'TITLE': 'работа'}}, idempotent=False)
    assert len(attempts) == 1
    data = events(journal)
    starts = [e for e in data if e['event'] == 'crm.http.request']
    failures = [e for e in data if e['event'] == 'crm.call.error']
    assert len(starts) == len(failures) == 1 and starts[0]['call_id'] == failures[0]['call_id']
    assert not [e for e in data if e['event'] == 'crm.http' and 'response_data' in e]
    assert starts[0]['request_data']['value']['DATA']['TITLE'] == 'работа'


def test_old_v115_history_migrates_into_live_txt_without_losing_events(tmp_path):
    from stocrm_agent.diagnostic_journal import LEGACY_FILE_NAME
    state = tmp_path/'state'
    state.mkdir()
    old = {'time': '2026-11-01T12:00:00+00:00', 'session': '1234567890abcdef',
           'event': 'bot.request.start', 'input_chars': 11}
    (state / LEGACY_FILE_NAME).write_text(json.dumps(old) + '\n', encoding='utf-8')
    journal = DiagnosticJournal(state)
    assert journal.path.name.endswith('.txt')
    assert not (state / LEGACY_FILE_NAME).exists()
    journal.emit('crm.http', method='work/add', response={'MESSAGE': 'Ответ CRM'})
    data = events(journal)
    assert data[0]['input_chars'] == 11 and data[1]['response_data']['value']['MESSAGE'] == 'Ответ CRM'
    target = tmp_path/'support_log.txt'
    summary = write_text_log(state, target)
    assert summary['records'] == 2 and 'Ответ CRM' in target.read_text(encoding='utf-8-sig')


def test_failed_migration_collects_both_histories_and_warns(tmp_path, monkeypatch):
    from stocrm_agent.diagnostic_journal import LEGACY_FILE_NAME
    state = tmp_path/'state'
    state.mkdir()
    old = {'time': '2026-11-01T12:00:00+00:00', 'session': '1234567890abcdef', 'event': 'bot.request.start'}
    (state / LEGACY_FILE_NAME).write_text(json.dumps(old) + '\n', encoding='utf-8')
    original = __import__('os').replace
    def refuse_migration(src, dst):
        if str(src).endswith(LEGACY_FILE_NAME):
            raise OSError('file in use')
        return original(src, dst)
    monkeypatch.setattr('stocrm_agent.diagnostic_journal.os.replace', refuse_migration)
    journal = DiagnosticJournal(state)
    journal.emit('bot.request.start', input_chars=22)
    target = tmp_path/'support_log.txt'
    result = write_text_log(state, target)
    assert result['records'] == 2 and 'legacy_history_separate' in result['warnings']
    assert (state / LEGACY_FILE_NAME).exists() and journal.path.exists()


def test_export_reapplies_credential_filter_to_tampered_live_file(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_SID', 'test-super-secret')
    journal = DiagnosticJournal(tmp_path/'state')
    item = {'time': '2026-11-01T12:00:00+00:00', 'session': '1234567890abcdef',
            'event': 'crm.http', 'method': 'work/add',
            'response_data': {'value': {'SOME_FIELD': 'test-super-secret', 'TOKEN': 'unknown-value',
                                        'NAME': 'Олег'}, 'truncated': False}}
    with journal.path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    target = tmp_path/'support_log.txt'
    write_text_log(journal.state, target)
    text = target.read_text(encoding='utf-8-sig')
    assert 'test-super-secret' not in text and 'unknown-value' not in text and 'Олег' in text


def test_request_is_written_before_the_http_response_arrives(tmp_path):
    session = MockSession()
    journal = DiagnosticJournal(tmp_path/'state')
    def post(url, json=None, timeout=None):
        current = events(journal)
        assert any(item['event'] == 'crm.http.request' and item['request_data']['value']['DATA']['TITLE'] == 'работа' for item in current)
        assert not any(item['event'] == 'crm.http' and 'response_data' in item for item in current)
        return MockResponse({'CODE': 200, 'RESPONSE': {'WORK_ID': 20}}, 200)
    session.post = post
    client = StocrmClient('demo', 'my-sid-123', session=session)
    client.diagnostic = journal
    assert client.call('work/add', {'DATA': {'TITLE': 'работа'}}) == {'WORK_ID': 20}
    assert any(item['event'] == 'crm.http' and 'response_data' in item for item in events(journal))


def test_network_route_retry_is_logged_without_repeating_writes(tmp_path, monkeypatch):
    journal = DiagnosticJournal(tmp_path/'state')
    client = StocrmClient('demo', 'my-sid-123')
    client.diagnostic = journal
    client._proxy = 'http://localhost:1234'
    client._direct = True
    calls = []
    class FakeSession:
        def post(self, url, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise requests.ConnectionError('route unavailable')
            return MockResponse({'CODE': 200, 'RESPONSE': []}, 200)
    monkeypatch.setattr(client, '_thread_session', lambda: FakeSession())
    monkeypatch.setattr(client, '_release_session', lambda session: None)
    assert client.call('work/get_filtered', {'FILTER': {'OFFER_ID': 1234}}, idempotent=True) == []
    requests_logged = [event for event in events(journal) if event['event'] == 'crm.http.request']
    assert len(calls) == len(requests_logged) == 2
    assert requests_logged[0]['call_id'] == requests_logged[1]['call_id']
    assert all(event['request_data']['value']['SID'] == '[СКРЫТО]' for event in requests_logged)
    client.close()
