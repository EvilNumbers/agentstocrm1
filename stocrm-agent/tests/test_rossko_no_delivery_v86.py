"""Имитации: реальный Росско, CRM и сеть здесь не используются."""
import xml.etree.ElementTree as ET

import pytest

from stocrm_agent import rossko

SOAP = b'''<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>
<GetSearchResponse xmlns="https://api.rossko.ru/"><SearchResult><success>true</success>
<PartsList><Part><brand>ABCORP</brand><partnumber>ZZ99123</partnumber>
<stocks><stock><price>4321</price><count>1000</count></stock></stocks>
<crosses><Part><brand>WRONG</brand><partnumber>ZZ99123</partnumber></Part></crosses>
</Part></PartsList></SearchResult></GetSearchResponse></Body></Envelope>'''


class Response:
    def __init__(self, status=200, content=SOAP):
        self.status_code = status
        self.content = content
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield self.content


@pytest.fixture
def fake(monkeypatch):
    calls = []
    monkeypatch.setenv('ROSSKO_ENABLED', '1')
    monkeypatch.setenv('ROSSKO_KEY1', 'secret-key1')
    monkeypatch.setenv('ROSSKO_KEY2', 'secret-key2')
    monkeypatch.delenv('ROSSKO_DELIVERY_ID', raising=False)
    monkeypatch.delenv('ROSSKO_ADDRESS_ID', raising=False)
    monkeypatch.delenv('STOCRM_MOCK', raising=False)
    monkeypatch.setattr(rossko, '_last_request', 0)
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((self.trust_env, url, kwargs))
            return Response()
    monkeypatch.setattr(rossko.requests, 'Session', Session)
    return calls


def test_request_without_any_delivery_ids_is_search_only(fake):
    result = rossko.search_brands('ZZ99123')
    assert result == {'status': 'ok', 'brands': [{'brand': 'ABCORP', 'sku': 'ZZ99123', 'source': 'Росско'}]}
    assert len(fake) == 1
    trust, url, params = fake[0]
    assert trust is False
    assert url == 'https://api.rossko.ru/service/v2.1/GetSearch'
    assert params['allow_redirects'] is False and params['verify'] is True
    op = next(iter(next(iter(ET.fromstring(params['data'])))))
    assert rossko._local(op.tag) == 'getsearch'
    assert [rossko._local(e.tag) for e in op] == ['key1', 'key2', 'text']
    assert 'secret' not in str(result) and '4321' not in str(result)


def test_no_id_http_failure_is_uncertain_and_never_retried(fake, monkeypatch):
    calls = []
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append(url)
            return Response(status=500)
    monkeypatch.setattr(rossko.requests, 'Session', Session)
    result = rossko.search_brands('ZZ99123')
    assert result['status'] == 'unavailable'
    assert 'без ID доставки' in result['error'] and 'причина отказа не установлена' in result['error']
    assert result['brands'] == [] and len(calls) == 1


def test_no_id_rejected_soap_is_unavailable_not_absent(fake, monkeypatch):
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs): return Response(content=SOAP.replace(b'<success>true', b'<success>false'))
    monkeypatch.setattr(rossko.requests, 'Session', Session)
    result = rossko.search_brands('ZZ99123')
    assert result['status'] == 'unavailable' and 'причина отказа не установлена' in result['error']


def test_optional_delivery_id_and_address_are_supported(fake, monkeypatch):
    monkeypatch.setenv('ROSSKO_DELIVERY_ID', '000000002')
    monkeypatch.setenv('ROSSKO_ADDRESS_ID', '12345')
    result = rossko.search_brands('ZZ99123')
    assert result['status'] == 'ok'
    op = next(iter(next(iter(ET.fromstring(fake[0][2]['data'])))))
    assert [rossko._local(e.tag) for e in op] == ['key1', 'key2', 'text', 'delivery_id', 'address_id']
    assert op[-2].text == '000000002' and op[-1].text == '12345'


def test_bad_ids_never_sent(fake, monkeypatch):
    monkeypatch.setenv('ROSSKO_ADDRESS_ID', '12345')
    assert rossko.search_brands('ZZ99123')['status'] == 'unavailable'
    monkeypatch.setenv('ROSSKO_DELIVERY_ID', 'bad')
    assert rossko.search_brands('ZZ99123')['status'] == 'unavailable'
    assert not fake


def test_empty_keys_do_not_contact_network(fake, monkeypatch):
    monkeypatch.setenv('ROSSKO_KEY1', '')
    result = rossko.search_brands('ZZ99123')
    assert result['status'] == 'unavailable' and 'локально не заданы' in result['error']
    assert not fake


def test_local_config_preserves_other_env_and_clears_ids(tmp_path):
    from stocrm_agent.rossko_config import configure_file
    path = tmp_path / '.env'
    path.write_text('STOCRM_SID=unchanged\nROSSKO_KEY1=old\nROSSKO_DELIVERY_ID=11\nOTHER=keep\n', encoding='utf-8')
    configure_file(path, 'mykey100', 'mykey200')
    values = path.read_text(encoding='utf-8')
    assert 'STOCRM_SID=unchanged' in values and 'OTHER=keep' in values
    assert 'ROSSKO_ENABLED=1\n' in values and 'ROSSKO_KEY1=mykey100\n' in values
    assert 'ROSSKO_KEY2=mykey200\n' in values and 'ROSSKO_DELIVERY_ID=\n' in values
    assert 'ROSSKO_ADDRESS_ID=\n' in values and not (tmp_path / '.state').exists()
    assert not list(tmp_path.glob('.rossko-env-*'))


def test_local_config_does_not_change_on_duplicate_or_bad_key(tmp_path):
    from stocrm_agent.rossko_config import configure_file
    path = tmp_path / '.env'
    text = 'ROSSKO_KEY1=old1\nROSSKO_KEY1=old2\nSTOCRM_SID=keep\n'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        configure_file(path, 'goodkey1', 'goodkey2')
    assert path.read_text(encoding='utf-8') == text
    with pytest.raises(ValueError):
        configure_file(path, 'bad\nkey', 'goodkey2')
    assert path.read_text(encoding='utf-8') == text
    assert not list(tmp_path.glob('.rossko-env-*'))


def test_config_cancel_never_writes_or_displays_keys(tmp_path, capsys):
    from stocrm_agent.rossko_config import run
    path = tmp_path / '.env'
    original = 'STOCRM_SID=keep\n'
    path.write_text(original, encoding='utf-8')
    keys = iter(['secret-key1', 'secret-key2'])
    assert run(path=path, ask=lambda _: 'нет', secret=lambda _: next(keys)) == 0
    assert path.read_text(encoding='utf-8') == original
    printed = capsys.readouterr().out
    assert 'secret-key1' not in printed and 'secret-key2' not in printed


def test_config_uses_existing_keys_without_showing_them(tmp_path, capsys):
    from stocrm_agent.rossko_config import run
    path = tmp_path / '.env'
    path.write_text('ROSSKO_KEY1=secretkey1\nROSSKO_KEY2=secretkey2\n', encoding='utf-8')
    def no_prompt(_):
        raise AssertionError('existing keys must not be requested again')
    assert run(path=path, ask=lambda _: 'ДА', secret=no_prompt) == 0
    text = path.read_text(encoding='utf-8')
    assert 'ROSSKO_ENABLED=1' in text and 'ROSSKO_DELIVERY_ID=\n' in text
    printed = capsys.readouterr().out
    assert 'secretkey1' not in printed and 'secretkey2' not in printed


def test_partcheck_exposes_uncertain_no_id_failure(fake, monkeypatch, tmp_path):
    from stocrm_agent.audit import AuditLog
    from stocrm_agent.client import StocrmClient
    from stocrm_agent.drafts import DraftStore
    from stocrm_agent.mock import MockSession
    from stocrm_agent.partcheck import run_partcheck
    from stocrm_agent.tools import Toolset
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs): return Response(status=500)
    monkeypatch.setattr(rossko.requests, 'Session', Session)
    crm = MockSession()
    tools = Toolset(StocrmClient('demo', 'demo', session=crm), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    monkeypatch.setattr(tools, 'web_lookup_part', lambda **kw: {'status': 'empty', 'brands': [], 'hits': 0})
    checks = run_partcheck(tools, 'ZZ99123')
    check = next(c for c in checks if c.title == 'Росско: поиск не выполнен')
    assert 'без ID доставки' in check.detail and 'причина отказа не установлена' in check.detail
    assert 'secret' not in check.detail
    assert all(row['method'] not in ('offer/require/add', 'offer/order/add', 'offer/order/add_free_order') for row in crm.calls)
