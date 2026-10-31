"""Лишь локальные имитации: ни Росско, ни настоящая CRM не вызываются."""
import pytest
import requests

from stocrm_agent import rossko


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setenv('ROSSKO_ENABLED', '1')
    monkeypatch.setenv('ROSSKO_KEY1', 'private-key-one')
    monkeypatch.setenv('ROSSKO_KEY2', 'private-key-two')
    monkeypatch.delenv('STOCRM_MOCK', raising=False)
    monkeypatch.delenv('ROSSKO_DELIVERY_ID', raising=False)
    monkeypatch.delenv('ROSSKO_ADDRESS_ID', raising=False)
    monkeypatch.setattr(rossko, '_last_request', 0)
    calls = []
    class Response:
        status_code = 200
        def __init__(self, content): self.content = content
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield self.content
    class Session:
        error = None
        content = b''
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kw):
            calls.append((url, kw))
            if self.error:
                raise self.error
            return Response(self.content)
    monkeypatch.setattr(rossko.requests, 'Session', Session)
    return Session, calls


def assert_safe(result, expected, calls):
    assert result['status'] == 'unavailable' and not result['brands']
    assert expected in result['error']
    assert 'без ID доставки' in result['error']
    assert 'причина отказа не установлена' in result['error']
    assert 'private-key' not in repr(result) and 'arbitrary-server-text' not in repr(result)
    assert len(calls) == 1
    assert calls[0][0] == rossko._SEARCH_URL
    assert calls[0][1]['allow_redirects'] is False


@pytest.mark.parametrize(('error', 'expected'), [
    (requests.exceptions.SSLError('private-key-one'), 'TLS'),
    (requests.exceptions.ConnectTimeout('private-key-one'), 'тайм-аут'),
    (requests.exceptions.ReadTimeout('private-key-one'), 'тайм-аут'),
    (requests.exceptions.ConnectionError('private-key-one'), 'соединение'),
    (requests.exceptions.RequestException('private-key-one'), 'сетевого запроса'),
    (OSError('private-key-one'), 'Системная ошибка'),
])
def test_network_exception_categories_are_safe(network, error, expected):
    session, calls = network
    session.error = error
    assert_safe(rossko.search_brands('2DUF050N7'), expected, calls)


@pytest.mark.parametrize(('content', 'expected'), [
    (b'<Envelope><Body><Fault><faultstring>private-key-one arbitrary-server-text</faultstring></Fault></Body></Envelope>', 'SOAP-ошибку'),
    (b'<Envelope><Body><GetSearchResponse><SearchResult><success>false</success><message>private-key-one arbitrary-server-text</message></SearchResult></GetSearchResponse></Body></Envelope>', 'сообщает, что поиск не выполнен'),
    (b'<!DOCTYPE Envelope><Envelope>private-key-one</Envelope>', 'запрещённые конструкции XML'),
    (b'<Envelope>private-key-one', 'не удалось прочитать как XML'),
    (b'\xffprivate-key-one', 'не удалось прочитать как XML'),
    (b'<Envelope><Body>private-key-one</Body></Envelope>', 'неожиданный формат'),
    (b'<Envelope><Body><GetSearchResponse><SearchResult><success>private-key-one</success></SearchResult></GetSearchResponse></Body></Envelope>', 'неожиданный формат'),
])
def test_response_error_categories_never_expose_server_data(network, content, expected):
    session, calls = network
    session.content = content
    assert_safe(rossko.search_brands('2DUF050N7'), expected, calls)


def test_delivery_id_removes_no_id_note_without_disclosing_id(network, monkeypatch):
    session, calls = network
    monkeypatch.setenv('ROSSKO_DELIVERY_ID', '987654321')
    session.error = requests.exceptions.SSLError('private-key-one')
    result = rossko.search_brands('2DUF050N7')
    assert result['status'] == 'unavailable' and 'TLS' in result['error']
    assert 'без ID доставки' not in result['error']
    assert '987654321' not in repr(result) and 'private-key' not in repr(result)
    assert len(calls) == 1


def test_partcheck_reports_safe_tls_category_with_local_crm(network, tmp_path, monkeypatch):
    from stocrm_agent.audit import AuditLog
    from stocrm_agent.client import StocrmClient
    from stocrm_agent.drafts import DraftStore
    from stocrm_agent.mock import MockSession
    from stocrm_agent.partcheck import run_partcheck
    from stocrm_agent.tools import Toolset
    session, calls = network
    session.error = requests.exceptions.SSLError('private-key-one')
    crm = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=crm), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    monkeypatch.setattr(tools, 'web_lookup_part', lambda **kw: {'status': 'empty', 'brands': [], 'hits': 0})
    checks = run_partcheck(tools, '2DUF050N7')
    check = next(item for item in checks if item.title == 'Росско: поиск не выполнен')
    assert 'TLS' in check.detail and 'без ID доставки' in check.detail
    assert 'private-key' not in repr(checks) and len(calls) == 1
    assert all(row['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for row in crm.calls)
