"""Только локальные имитации: нет запросов к поставщику или реальной CRM."""
import json
import subprocess
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
import requests

from stocrm_agent import profit_liga as p, profit_liga_report as report
from stocrm_agent.audit import AuditLog
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.partcheck import run_partcheck
from stocrm_agent.tools import Toolset

KEY = 'private+key/with space&123'
ARTICLE = 'ZZ99123'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('STOCRM_ENV_FILE', raising=False)
    monkeypatch.delenv('STOCRM_MOCK', raising=False)
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '1')
    monkeypatch.setenv('PROFIT_LIGA_KEY', KEY)
    monkeypatch.setenv('ROSSKO_ENABLED', '0')
    monkeypatch.setattr(p, '_last_request', 0.0)
    envfile = tmp_path / '.env'
    raw = b'PROFIT_LIGA_ENABLED=1\nPROFIT_LIGA_KEY=should-not-be-read-in-test\nOTHER=keep\n'
    envfile.write_bytes(raw)
    state = tmp_path / '.state'
    state.mkdir()
    (state / 'marker').write_text('unchanged')
    calls = []
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/json'}
        content = b'[]'
        failure = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            yield self.content
            if self.failure:
                raise self.failure
    class Session:
        failure = None
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs, self.trust_env))
            if self.failure: raise self.failure
            return Response()
    monkeypatch.setattr(p.requests, 'Session', Session)
    return tmp_path, raw, calls, Response, Session


def files(path):
    return sorted((path / report.REPORT_DIR).glob('profit-liga-error-*.txt'))


def contents(path):
    paths = files(path)
    assert len(paths) == 1
    return paths[0].read_text(encoding='utf-8-sig')


def test_http_error_full_body_local_only_without_second_request(env, capsys):
    path, raw, calls, response, _ = env
    response.status_code = 401
    response.content = ('{"message":"Bad secret ' + KEY + ' PRIVATE-CUSTOMER"}').encode()
    result = p.search_brands(ARTICLE)
    assert result['status'] == 'unavailable' and 'HTTP 401' in result['error']
    assert 'profit_liga_local_reports' in result['error']
    assert KEY not in str(result) and 'PRIVATE-CUSTOMER' not in str(result)
    assert KEY not in capsys.readouterr().out
    text = contents(path)
    assert 'HTTP статус: 401' in text and 'Bad secret [СКРЫТ КЛЮЧ] PRIVATE-CUSTOMER' in text
    assert 'Тело прочитано полностью: да' in text and 'Тело ограничено/прервано: нет' in text
    assert KEY not in text and len(calls) == 1
    assert calls[0][0] == p._SEARCH_URL and calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['verify'] is True and calls[0][2] is False
    assert (path/'.env').read_bytes() == raw and (path/'.state'/'marker').read_text() == 'unchanged'
    assert (path/report.REPORT_DIR/'.gitignore').read_text() == '*\n'


def test_network_exception_masks_raw_and_encoded_secrets(env):
    path, _, calls, _, session = env
    session.failure = requests.exceptions.ConnectionError(
        'details ' + KEY + ' url /search/products?secret=' + quote_plus(KEY) + '&article=ZZ99123 ' + quote(KEY, safe=''))
    result = p.search_brands(ARTICLE)
    text = contents(path)
    assert result['status'] == 'unavailable' and KEY not in str(result)
    assert 'ConnectionError' in text and 'details [СКРЫТ КЛЮЧ]' in text
    assert 'HTTP статус: [нет ответа]' in text
    for value in (KEY, quote_plus(KEY), quote(KEY, safe='')):
        assert value not in text
    assert len(calls) == 1


@pytest.mark.parametrize('code', [302, 400, 403, 429, 500])
def test_error_status_and_body_are_recorded(env, code):
    path, _, calls, response, _ = env
    response.status_code = code
    response.content = b'{"error":"some server explanation"}'
    result = p.search_brands(ARTICLE)
    text = contents(path)
    assert result['status'] == 'unavailable' and f'HTTP {code}' in result['error']
    assert f'HTTP статус: {code}' in text and 'some server explanation' in text
    assert len(calls) == 1 and not calls[0][1]['allow_redirects']


def test_incomplete_stream_records_received_prefix(env):
    path, _, calls, response, _ = env
    response.status_code = 503
    response.content = b'{"partial":"details'
    response.failure = requests.exceptions.ConnectionError('stream broken ' + KEY)
    result = p.search_brands(ARTICLE)
    text = contents(path)
    assert 'HTTP 503' in result['error'] and result['status'] == 'unavailable'
    assert '{"partial":"details' in text and 'stream broken [СКРЫТ КЛЮЧ]' in text
    assert 'Тело ограничено/прервано: да' in text and len(calls) == 1


def test_response_limited_to_two_megabytes(env):
    path, _, calls, response, _ = env
    response.content = b'x' * (p._MAX_RESPONSE + 1000)
    result = p.search_brands(ARTICLE)
    text = contents(path)
    assert result['status'] == 'unavailable' and 'слишком большой' in result['error']
    assert 'Тело ограничено/прервано: да' in text
    assert len(text) < p._MAX_RESPONSE + 10000 and len(calls) == 1


def test_format_error_and_partial_have_detailed_report(env):
    path, _, calls, response, _ = env
    response.content = b'{"error":"server private message"}'
    result = p.search_brands(ARTICLE)
    assert result['status'] == 'unavailable'
    assert 'server private message' not in str(result) and 'server private message' in contents(path)
    response.content = json.dumps([{'article': ARTICLE, 'brand': 'ABC'}, {'brand': 'missing sku'}]).encode()
    result = p.search_brands(ARTICLE)
    assert result['status'] == 'partial' and result['brands'][0]['brand'] == 'ABC'
    assert len(files(path)) == 2 and len(calls) == 2
    assert any('missing sku' in f.read_text(encoding='utf-8-sig') for f in files(path))


def test_success_empty_disabled_and_missing_config_do_not_create_reports(env, monkeypatch):
    path, _, calls, response, _ = env
    response.content = b'[]'
    assert p.search_brands(ARTICLE)['status'] == 'empty'
    response.content = json.dumps([{'article': ARTICLE, 'brand': 'ABC'}]).encode()
    assert p.search_brands(ARTICLE)['status'] == 'ok'
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '0')
    assert p.search_brands(ARTICLE)['status'] == 'disabled'
    monkeypatch.setenv('PROFIT_LIGA_ENABLED', '1')
    monkeypatch.delenv('PROFIT_LIGA_KEY')
    assert p.search_brands(ARTICLE)['status'] == 'unavailable'
    assert len(calls) == 2 and not (path/report.REPORT_DIR).exists()


def test_report_write_failure_is_safe_and_keeps_original_error(env, monkeypatch):
    path, raw, calls, response, _ = env
    response.status_code = 401
    response.content = b'secret response'
    def denied(*args, **kwargs): raise PermissionError(KEY)
    monkeypatch.setattr(report, 'write_report', denied)
    result = p.search_brands(ARTICLE)
    assert result['status'] == 'unavailable' and 'HTTP 401' in result['error']
    assert 'Не удалось сохранить' in result['error'] and KEY not in str(result)
    assert len(calls) == 1 and (path/'.env').read_bytes() == raw


def test_parts_test_displays_safe_report_location_not_server_body(env):
    path, _, calls, response, _ = env
    response.status_code = 403
    response.content = b'{"error":"SENSITIVE-DATA"}'
    session = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(path/'drafts.json'), AuditLog(path/'audit.jsonl'))
    checks = run_partcheck(tools, ARTICLE)
    view = '\n'.join(c.line() for c in checks)
    assert 'profit_liga_local_reports' in view and 'HTTP 403' in view
    assert KEY not in view and 'SENSITIVE-DATA' not in view
    assert 'SENSITIVE-DATA' in contents(path) and len(calls) == 1
    assert not any(c['method'] in ('offer/order/add', 'offer/order/add_free_order', 'offer/require/add') for c in session.calls)


def test_report_is_next_to_explicit_env_and_open_folder(env, monkeypatch, capsys):
    path, _, _, response, _ = env
    other = path/'custom'; other.mkdir(); (other/'settings.env').write_text('X=1')
    monkeypatch.setenv('STOCRM_ENV_FILE', str(other/'settings.env'))
    response.status_code = 401
    p.search_brands(ARTICLE)
    assert len(files(other)) == 1 and not files(path)
    opened = []
    monkeypatch.setattr(report.os, 'startfile', lambda file: opened.append(file), raising=False)
    assert report.open_reports() == 0
    assert opened == [str(other/report.REPORT_DIR)]
    assert KEY not in capsys.readouterr().out


def test_open_missing_reports_does_not_create_folder_or_call_network(env, capsys):
    path, _, calls, _, _ = env
    assert report.open_reports() == 1
    assert 'пока нет' in capsys.readouterr().out
    assert not (path/report.REPORT_DIR).exists() and not calls


def test_repo_ignore_rule():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['git', 'check-ignore', '-q', 'stocrm-agent/profit_liga_local_reports/profit-liga-error-test.txt'], cwd=root, check=False)
    assert result.returncode == 0


def test_stream_fails_before_first_byte_marks_incomplete(env, monkeypatch):
    path, _, _, response, _ = env
    def chunks(self, size):
        raise requests.exceptions.Timeout('before body')
        yield b''  # generator, never reached
    monkeypatch.setattr(response, 'iter_content', chunks)
    assert p.search_brands(ARTICLE)['status'] == 'unavailable'
    text = contents(path)
    assert 'Тело прочитано полностью: нет' in text and 'Тело ограничено/прервано: да' in text


def test_mask_common_url_json_and_html_key_encodings():
    import html
    key = 'секрет&"/ ключ'
    values = [key, quote(key, safe=''), quote_plus(key), html.escape(key),
              json.dumps(key, ensure_ascii=True)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1]]
    text = ' || '.join(values)
    masked = report._mask_key(text, key)
    for value in values:
        assert value not in masked
    assert '[СКРЫТ КЛЮЧ]' in masked
