"""Только локальные имитации, без запросов к настоящему Росско или CRM."""
import xml.etree.ElementTree as ET

import pytest

from stocrm_agent import rossko_delivery


def response(success, message):
    root = ET.Element('Envelope')
    body = ET.SubElement(root, 'Body')
    result = ET.SubElement(ET.SubElement(body, 'GetCheckoutDetailsResponse'), 'CheckoutDetailsResult')
    if success is not None:
        ET.SubElement(result, 'success').text = success
    ET.SubElement(result, 'message').text = message
    return ET.tostring(root, encoding='utf-8')


@pytest.mark.parametrize(('message', 'code'), [
    ('Неверные ключи private-key1 и private-key2', 'R01'),
    ('Вы не ввели логин и/или пароль! private-key1', 'R01'),
    ('unauthorized private-key1', 'R01'),
    ('Превышен лимит private-key1', 'R02'),
    ('IP-адрес private-key1', 'R03'),
    ('непонятный ответ private-key1', 'R04'),
    ('', 'R04'),
])
def test_rejection_is_categorized_without_exposing_message_or_secrets(message, code):
    with pytest.raises(rossko_delivery.LookupErrorSafe) as error:
        rossko_delivery._parse_details(response('false', message), ('private-key1', 'private-key2'))
    text = str(error.value)
    assert f'Код {code}' in text
    assert 'private-key1' not in text and 'private-key2' not in text
    assert 'непонятный ответ' not in text


@pytest.mark.parametrize(('success', 'code'), [(None, 'R05'), ('', 'R06'), ('unknown-private-key1', 'R06')])
def test_missing_or_unknown_status_is_not_called_a_rejection(success, code):
    with pytest.raises(rossko_delivery.LookupErrorSafe) as error:
        rossko_delivery._parse_details(response(success, 'private-key1'), ('private-key1', 'private-key2'))
    assert f'Код {code}' in str(error.value) and 'private-key' not in str(error.value)


def test_success_one_still_succeeds():
    xml = b'<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult><success>1</success><DeliveryType><delivery><id>0001</id><name>Pickup</name></delivery></DeliveryType></CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'
    deliveries, addresses = rossko_delivery._parse_details(xml, ('secret1', 'secret2'))
    assert deliveries[0].id == '0001' and not addresses


def test_rejection_run_is_single_read_only_call_and_preserves_settings(tmp_path, monkeypatch, capsys):
    for name in ('ROSSKO_ENABLED', 'ROSSKO_KEY1', 'ROSSKO_KEY2', 'ROSSKO_DELIVERY_ID', 'ROSSKO_ADDRESS_ID', 'STOCRM_MOCK'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / '.env'
    raw = b'STOCRM_MOCK=0\nROSSKO_ENABLED=1\nROSSKO_KEY1=private-key1\nROSSKO_KEY2=private-key2\nROSSKO_DELIVERY_ID=9\nOTHER=keep\n'
    path.write_bytes(raw)
    state = tmp_path / '.state'
    state.mkdir()
    (state / 'marker').write_text('keep')
    calls = []
    class Reply:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, *args):
            yield response('false', 'Неверный ключ private-key1; personal-address')
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Reply()
    monkeypatch.setattr(rossko_delivery.requests, 'Session', Session)
    assert rossko_delivery.run(path=path, ask=lambda _: pytest.fail('unexpected prompt')) == 1
    output = capsys.readouterr().out
    assert 'Код R01' in output
    assert 'private-key1' not in output and 'private-key2' not in output and 'personal-address' not in output
    assert path.read_bytes() == raw and (state / 'marker').read_text() == 'keep'
    assert len(calls) == 1 and calls[0][0] == rossko_delivery._URL
    assert calls[0][1]['allow_redirects'] is False and calls[0][1]['verify'] is True
    op = next(iter(next(iter(ET.fromstring(calls[0][1]['data'])))))
    assert rossko_delivery._local(op.tag) == 'getcheckoutdetails'
