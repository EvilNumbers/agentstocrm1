"""SOAP 1.1 контракт по WSDL пользователя; настоящий Росско и CRM не вызываются."""
import xml.etree.ElementTree as ET

import pytest

from stocrm_agent import rossko, rossko_delivery

SOAP = '{http://schemas.xmlsoap.org/soap/envelope/}'
NS = '{https://api.rossko.ru/}'
DETAILS = b'''<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>
<GetCheckoutDetailsResponse xmlns="https://api.rossko.ru/"><CheckoutDetailsResult><success>true</success>
<DeliveryType><delivery><id>000000001</id><name>Pickup</name></delivery></DeliveryType>
</CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'''
SEARCH = b'''<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>
<GetSearchResponse xmlns="https://api.rossko.ru/"><SearchResult><success>true</success>
<PartsList><Part><brand>BRAND</brand><partnumber>2DUF050N7</partnumber></Part></PartsList>
</SearchResult></GetSearchResponse></Body></Envelope>'''


@pytest.fixture
def transport(monkeypatch):
    calls = []
    class Response:
        content = DETAILS
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, *args): yield self.content
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs, self.trust_env))
            return Response()
    monkeypatch.setattr(rossko_delivery.requests, 'Session', Session)
    return calls, Response


def test_details_wire_matches_user_wsdl_and_escapes_test_keys(transport):
    calls, _ = transport
    key1, key2 = 'fake-a<&', 'fake-b\"&'
    deliveries, _ = rossko_delivery._lookup(key1, key2)
    assert deliveries[0].id == '000000001' and len(calls) == 1
    url, kwargs, trust = calls[0]
    assert url == 'https://api.rossko.ru/service/v2.1/GetCheckoutDetails'
    assert kwargs['headers']['SOAPAction'] == '"' + url + '"'
    assert kwargs['verify'] is True and kwargs['allow_redirects'] is False and trust is False
    root = ET.fromstring(kwargs['data'])
    assert root.tag == SOAP + 'Envelope'
    body = root.find(SOAP + 'Body'); assert body is not None and len(body) == 1
    operation = body[0]
    assert operation.tag == NS + 'GetCheckoutDetails'
    assert [e.tag for e in operation] == [NS + 'KEY1', NS + 'KEY2']
    assert [e.text for e in operation] == [key1, key2]


def test_search_action_is_quoted_and_operation_is_still_search(transport, monkeypatch):
    calls, response = transport
    response.content = SEARCH
    for k, v in {'ROSSKO_ENABLED':'1', 'ROSSKO_KEY1':'fake-key1', 'ROSSKO_KEY2':'fake-key2', 'ROSSKO_DELIVERY_ID':'000000001'}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv('STOCRM_MOCK', raising=False)
    monkeypatch.setattr(rossko, '_last_request', 0)
    assert rossko.search_brands('2DUF050N7')['status'] == 'ok'
    assert len(calls) == 1
    url, kwargs, _ = calls[0]
    assert url == rossko._SEARCH_URL and kwargs['headers']['SOAPAction'] == '"' + url + '"'
    op = next(iter(next(iter(ET.fromstring(kwargs['data'])))))
    assert op.tag == NS + 'GetSearch'


def test_authorization_refusal_is_not_retried_or_exposed(transport):
    calls, response = transport
    response.content = b'<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult><success>false</success><message>invalid key fake-key1</message></CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'
    with pytest.raises(rossko_delivery.LookupErrorSafe) as exc:
        rossko_delivery._lookup('fake-key1', 'fake-key2')
    assert len(calls) == 1 and 'fake-key1' not in str(exc.value) and 'fake-key2' not in str(exc.value)
