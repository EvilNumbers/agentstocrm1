"""Все ответы имитированы; в настоящие CRM и Росско нет обращений."""
from pathlib import Path

import pytest
import requests

from stocrm_agent import rossko_delivery as d


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ('ROSSKO_ENABLED', 'ROSSKO_KEY1', 'ROSSKO_KEY2', 'ROSSKO_DELIVERY_ID', 'ROSSKO_ADDRESS_ID', 'STOCRM_MOCK'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path/'.env'
    initial = b'STOCRM_MOCK=0\nROSSKO_ENABLED=1\nROSSKO_KEY1=test-key-123\nROSSKO_KEY2=test-key-456\nROSSKO_DELIVERY_ID=7\nOTHER=keep\n'
    path.write_bytes(initial)
    state = tmp_path/'.state'; state.mkdir(); (state/'marker').write_text('keep')
    calls = []
    class Reply:
        status_code = 200
        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        content = b''
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_content(self, *a): yield self.content
    class Session:
        exception = None
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if self.exception: raise self.exception
            return Reply()
    monkeypatch.setattr(d.requests, 'Session', Session)
    return path, initial, state, calls, Reply, Session


def one_report(path):
    files = list((path.parent/'rossko_local_reports').glob('rossko-error-*.txt'))
    assert len(files) == 1
    return files[0].read_text(encoding='utf-8-sig')


def rejection(message):
    return ('<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult>'
            '<success>false</success><message>' + message + '</message>'
            '</CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>').encode()


def test_rejected_message_preserved_in_local_report_and_not_printed(env, capsys):
    path, initial, state, calls, reply, _ = env
    reply.content = rejection('Неверный ключ! private-personal-data test-key-123')
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    output = capsys.readouterr().out
    assert 'R01' in output and 'Подробный локальный отчёт' in output
    assert 'private-personal-data' not in output and 'test-key-123' not in output
    assert (path.parent/'rossko_local_reports'/'.gitignore').read_text() == '*\n'
    text = one_report(path)
    assert 'Неверный ключ! private-personal-data [СКРЫТ КЛЮЧ]' in text
    assert 'success: false' in text and 'HTTP статус: 200' in text
    assert 'Тело ограничено/прервано: нет' in text
    assert 'test-key-123' not in text and 'test-key-456' not in text
    assert path.read_bytes() == initial and (state/'marker').read_text() == 'keep'
    assert len(calls) == 1 and calls[0][0] == d._URL
    assert calls[0][1]['allow_redirects'] is False and calls[0][1]['verify'] is True


def test_http_500_fault_body_is_captured_without_second_request(env, capsys):
    path, initial, _, calls, reply, _ = env
    reply.status_code = 500
    reply.content = b'<Envelope><Body><Fault><faultcode>Server</faultcode><faultstring>PRIVATE-SOAP-DETAIL</faultstring></Fault></Body></Envelope>'
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    output = capsys.readouterr().out
    assert 'HTTP 500' in output and 'PRIVATE-SOAP-DETAIL' not in output
    text = one_report(path)
    assert 'HTTP статус: 500' in text and 'SOAP Fault code: Server' in text
    assert 'SOAP Fault text: PRIVATE-SOAP-DETAIL' in text and 'Тело ограничено/прервано: нет' in text
    assert len(calls) == 1 and path.read_bytes() == initial


def test_network_error_details_redact_known_keys(env, capsys):
    path, initial, _, calls, _, session = env
    session.exception = requests.exceptions.SSLError('TLS diagnostic test-key-123 private-other')
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    assert 'test-key-123' not in capsys.readouterr().out
    text = one_report(path)
    assert 'SSLError' in text and 'TLS diagnostic [СКРЫТ КЛЮЧ] private-other' in text
    assert 'HTTP статус: [нет ответа]' in text and 'test-key-123' not in text
    assert len(calls) == 1 and path.read_bytes() == initial


def test_report_records_partial_body_if_stream_breaks(env, monkeypatch):
    path, _, _, calls, reply, _ = env
    def chunks(self, *args):
        yield b'<partial>hello'
        raise requests.exceptions.ConnectionError('stream broke')
    monkeypatch.setattr(reply, 'iter_content', chunks)
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    text = one_report(path)
    assert '<partial>hello' in text and 'Тело ограничено/прервано: да' in text
    assert 'ConnectionError' in text and len(calls) == 1


def test_oversized_response_marked_and_bounded(env):
    path, _, _, calls, reply, _ = env
    reply.content = b'X' * (d._MAX_RESPONSE + 12345)
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    text = one_report(path)
    assert 'Тело ограничено/прервано: да' in text
    assert len(text) < d._MAX_RESPONSE + 10000 and len(calls) == 1


def test_no_report_on_success_or_user_cancellation(env):
    path, initial, _, calls, reply, _ = env
    reply.content = b'<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult><success>true</success><DeliveryType><delivery><id>1</id><name>Pickup</name></delivery></DeliveryType></CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'
    assert d.run(path=path, ask=lambda _: 'q') == 1
    assert path.read_bytes() == initial and not (path.parent/'rossko_local_reports').exists()
    assert len(calls) == 1


def test_failed_report_save_leaves_original_error_and_settings(env, monkeypatch, capsys):
    path, initial, _, calls, reply, _ = env
    reply.content = rejection('invalid key')
    def denied(*args, **kwargs): raise PermissionError('test-key-123')
    monkeypatch.setattr(d, '_write_local_report', denied)
    assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    out = capsys.readouterr().out
    assert 'R01' in out and 'Не удалось записать' in out and 'test-key-123' not in out
    assert path.read_bytes() == initial and len(calls) == 1


def test_unique_reports_never_overwrite(env):
    path, _, _, calls, reply, _ = env
    reply.content = rejection('invalid key')
    for _ in range(2):
        assert d.run(path=path, ask=lambda _: pytest.fail('no prompt')) == 1
    assert len(list((path.parent/'rossko_local_reports').glob('rossko-error-*.txt'))) == 2
    assert len(calls) == 2  # только два явных запуска человеком, без повтора внутри запуска


def test_known_xml_escaped_keys_are_masked_but_personal_data_remains_local(tmp_path):
    trace = d.LookupTrace(attempted=True, status=200, body=b'<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult><success>false</success><message>invalid key a&amp;b and personal-data</message></CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>')
    report = d._write_local_report(tmp_path, trace, 'R01', ('a&b', 'another-key'))
    text = report.read_text(encoding='utf-8-sig')
    assert 'a&b' not in text and 'a&amp;b' not in text and 'another-key' not in text
    assert 'personal-data' in text and '[СКРЫТ КЛЮЧ]' in text


def test_reports_are_excluded_from_git():
    import subprocess
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['git', 'check-ignore', '-q', 'stocrm-agent/rossko_local_reports/rossko-error-example.txt'], cwd=root, check=False)
    assert result.returncode == 0
