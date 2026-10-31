"""Только имитации, без настоящего Росско или CRM."""
import xml.etree.ElementTree as ET

import pytest
import requests

from stocrm_agent import rossko, rossko_delivery
from stocrm_agent.envfile import _parse

SOAP = b'''<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>
<GetCheckoutDetailsResponse xmlns="https://api.rossko.ru/"><CheckoutDetailsResult><success>true</success>
<DeliveryType><delivery><id>000000001</id><name>Pickup</name></delivery><delivery><id>000000002</id><name>Courier</name></delivery></DeliveryType>
<DeliveryAddress><address><id>112233</id><city>Private City</city><street>Private Street</street><Delivery><ids><id>2</id></ids></Delivery></address>
<address><id>445566</id><city>Other City</city><Delivery><ids><id>1</id></ids></Delivery></address></DeliveryAddress>
<PaymentType><payment><id>5</id><name>private-pay-data</name></payment></PaymentType>
<CompanyList><company><name>private-company-data</name></company></CompanyList>
</CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k in ('ROSSKO_ENABLED', 'ROSSKO_KEY1', 'ROSSKO_KEY2', 'ROSSKO_DELIVERY_ID', 'ROSSKO_ADDRESS_ID', 'STOCRM_MOCK'):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path/'.env'
    path.write_bytes(b'\xef\xbb\xbfSTOCRM_MOCK=0\r\nROSSKO_ENABLED=1\r\nROSSKO_KEY1=private-key1\r\nROSSKO_KEY2=private-key2\r\nROSSKO_DELIVERY_ID=9\r\nROSSKO_ADDRESS_ID=8\r\nOTHER=keep\r\n')
    state = tmp_path/'.state'; state.mkdir(); (state/'marker').write_text('keep')
    calls = []
    class Response:
        status_code = 200
        content = SOAP
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield self.content
    class Session:
        trust_env = True
        exception = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((self.trust_env, url, kwargs))
            if self.exception:
                raise self.exception
            return Response()
    monkeypatch.setattr(rossko_delivery.requests, 'Session', Session)
    return path, state, calls, Session, Response


def run_choices(path, *choices):
    it = iter(choices)
    return rossko_delivery.run(path=path, ask=lambda _: next(it))


def test_courier_id_and_compatible_address_are_saved_locally(setup, capsys):
    path, state, calls, _, _ = setup
    assert run_choices(path, '2', '1') == 0
    content = path.read_bytes()
    values = _parse(content.decode('utf-8-sig'))
    assert values['ROSSKO_DELIVERY_ID'] == '000000002' and values['ROSSKO_ADDRESS_ID'] == '112233'
    assert values['ROSSKO_KEY1'] == 'private-key1' and values['OTHER'] == 'keep'
    assert content.startswith(b'\xef\xbb\xbf') and b'\n' not in content.replace(b'\r\n', b'')
    assert (state/'marker').read_text() == 'keep'
    assert len(calls) == 1
    trust, url, kwargs = calls[0]
    assert not trust and url == 'https://api.rossko.ru/service/v2.1/GetCheckoutDetails'
    assert kwargs['verify'] is True and kwargs['allow_redirects'] is False and kwargs['stream'] is True
    operation = next(iter(next(iter(ET.fromstring(kwargs['data'])))))
    assert rossko._local(operation.tag) == 'getcheckoutdetails'
    assert [rossko._local(child.tag) for child in operation] == ['key1', 'key2']
    printed = capsys.readouterr().out
    assert 'Private City' in printed and 'Other City' not in printed
    assert 'private-key' not in printed and 'private-pay-data' not in printed and 'private-company-data' not in printed


def test_pickup_without_address_and_search_uses_chosen_id(setup, monkeypatch):
    path, _, calls, _, response = setup
    assert run_choices(path, '1', '0') == 0
    values = _parse(path.read_text(encoding='utf-8-sig'))
    assert values['ROSSKO_DELIVERY_ID'] == '000000001' and values['ROSSKO_ADDRESS_ID'] == ''
    for key in ('ROSSKO_ENABLED', 'ROSSKO_KEY1', 'ROSSKO_KEY2', 'ROSSKO_DELIVERY_ID', 'ROSSKO_ADDRESS_ID'):
        monkeypatch.setenv(key, values[key])
    response.content = b'<Envelope><Body><GetSearchResponse><SearchResult><success>true</success><PartsList/></SearchResult></GetSearchResponse></Body></Envelope>'
    monkeypatch.setattr(rossko, '_last_request', 0)
    assert rossko.search_brands('2DUF050N7')['status'] == 'empty'
    assert len(calls) == 2 and calls[1][1] == rossko._SEARCH_URL
    op = next(iter(next(iter(ET.fromstring(calls[1][2]['data'])))))
    assert {rossko._local(e.tag): e.text for e in op}['delivery_id'] == '000000001'
    assert 'address_id' not in [rossko._local(e.tag) for e in op]


@pytest.mark.parametrize('choices', [('Q',), ('9',), ('2', 'Q'), ('2', '9'), ('',)])
def test_cancellation_or_invalid_choice_does_not_change_anything(setup, choices):
    path, state, calls, _, _ = setup
    before = path.read_bytes()
    assert run_choices(path, *choices) == 1
    assert path.read_bytes() == before and (state/'marker').read_text() == 'keep'
    assert len(calls) == 1


def test_env_change_while_choosing_does_not_overwrite(setup, capsys):
    path, _, calls, _, _ = setup
    it = iter(['2', '1'])
    def ask(_):
        value = next(it)
        if value == '1':
            path.write_bytes(b'OTHER=changed\n')
        return value
    assert rossko_delivery.run(path=path, ask=ask) == 1
    assert path.read_bytes() == b'OTHER=changed\n' and len(calls) == 1
    assert 'изменился' in capsys.readouterr().out


def test_environment_override_and_mock_stop_before_network(setup, monkeypatch):
    path, _, calls, _, _ = setup
    before = path.read_bytes()
    monkeypatch.setenv('ROSSKO_KEY1', 'other-account')
    assert run_choices(path) == 1 and not calls
    monkeypatch.delenv('ROSSKO_KEY1')
    monkeypatch.setenv('STOCRM_MOCK', '1')
    assert run_choices(path) == 1 and not calls and path.read_bytes() == before


@pytest.mark.parametrize('exception', [requests.exceptions.SSLError('private-key1'), requests.exceptions.Timeout('private-key1'), requests.exceptions.ConnectionError('private-key1'), requests.RequestException('private-key1')])
def test_network_failure_is_safe_and_keeps_old_ids(setup, capsys, exception):
    path, _, calls, session, _ = setup
    before = path.read_bytes()
    session.exception = exception
    assert run_choices(path) == 1
    assert path.read_bytes() == before and len(calls) == 1
    assert 'private-key' not in capsys.readouterr().out


@pytest.mark.parametrize('content', [
    b'<Envelope><Body><Fault><faultstring>private-key1</faultstring></Fault></Body></Envelope>',
    SOAP.replace(b'<success>true', b'<success>false'),
    SOAP.replace(b'000000002', b'000000001'),
    b'<!DOCTYPE foo><Envelope/>', b'<Envelope>', b'\xff', b'<Envelope/>',
    b'x' * 2000001,
])
def test_rejected_or_malformed_responses_leave_settings_unchanged(setup, capsys, content):
    path, _, calls, _, response = setup
    before = path.read_bytes(); response.content = content
    assert run_choices(path) == 1
    assert path.read_bytes() == before and len(calls) == 1
    assert 'private-key' not in capsys.readouterr().out


def test_server_data_control_characters_and_keys_never_printed(setup, capsys):
    path, _, _, _, response = setup
    response.content = SOAP.replace(b'Courier', b'private-key1&#x202e;hidden')
    assert run_choices(path, '2', '0') == 0
    output = capsys.readouterr().out
    assert 'private-key1' not in output and '\u202e' not in output and '[скрыто]' in output


def test_missing_compatibility_never_guessed(setup, capsys):
    path, _, _, _, response = setup
    response.content = SOAP.replace(b'<id>2</id></ids>', b'<id>3</id></ids>')
    assert run_choices(path, '2', '1') == 1
    assert 'Private City' not in capsys.readouterr().out
    assert _parse(path.read_text(encoding='utf-8-sig'))['ROSSKO_DELIVERY_ID'] == '9'


def test_only_id_fields_change_and_duplicates_are_normalized(tmp_path):
    from stocrm_agent.rossko_config import update_delivery_ids
    path = tmp_path/'.env'
    raw = b'OTHER=keep\nROSSKO_DELIVERY_ID=1\nROSSKO_DELIVERY_ID=2\nROSSKO_KEY1=key\nROSSKO_ADDRESS_ID=3\n'
    path.write_bytes(raw)
    update_delivery_ids(path, '00004', '', expected_raw=raw)
    output = path.read_bytes()
    assert output == b'OTHER=keep\nROSSKO_DELIVERY_ID=00004\nROSSKO_KEY1=key\nROSSKO_ADDRESS_ID=\n'


def test_http_error_no_body_no_retry(setup, capsys):
    path, _, calls, _, response = setup
    before = path.read_bytes()
    response.status_code = 500
    response.content = b'private-key1'
    assert run_choices(path) == 1
    printed = capsys.readouterr().out
    assert 'HTTP 500' in printed and 'private-key1' not in printed
    assert path.read_bytes() == before and len(calls) == 1


def test_failed_atomic_replace_preserves_file_and_cleans_temp(setup, monkeypatch, capsys):
    import errno
    from stocrm_agent import rossko_config
    path, _, calls, _, _ = setup
    before = path.read_bytes()
    def denied(*args):
        raise PermissionError(errno.EACCES, 'private-key1')
    monkeypatch.setattr(rossko_config.os, 'replace', denied)
    assert run_choices(path, '1', '0') == 1
    assert path.read_bytes() == before and len(calls) == 1
    assert not list(path.parent.glob('.rossko-env-*'))
    assert 'private-key1' not in capsys.readouterr().out
