"""Без сетевых запросов, ключи тестовые."""
import xml.etree.ElementTree as ET

import pytest

from stocrm_agent import rossko_delivery


@pytest.mark.parametrize(('message', 'expected'), [
    ('Не указан ключ', 'A1'),
    ('Вы не ввели логин и/или пароль', 'A1'),
    ('Неверный ключ авторизации', 'A2'),
    ('Invalid key', 'A2'),
    ('API key not found', 'A1'),
    ('Неверный ключ доступа', 'A2'),
    ('Неверный API key доступ запрещён', 'A4'),
    ('Ключ заблокирован', 'A3'),
    ('Не указан или неверный ключ', 'A4'),
    ('Ошибка авторизации', 'A4'),
    ('Ключ private-key1', 'A4'),
    ('ключ private-INVALID-key2', 'A4'),
])
def test_auth_details_show_only_fixed_category(message, expected):
    output = rossko_delivery._rejection_message(message, ('private-key1', 'private-INVALID-key2'))
    assert 'R01' in output and f'уточнение {expected}' in output
    assert 'private-key1' not in output and 'private-INVALID-key2' not in output
    assert message not in output


def test_auth_rejection_remains_no_network_retry_and_env_unchanged(tmp_path, monkeypatch, capsys):
    for key in ('ROSSKO_ENABLED', 'ROSSKO_KEY1', 'ROSSKO_KEY2', 'ROSSKO_DELIVERY_ID', 'ROSSKO_ADDRESS_ID', 'STOCRM_MOCK'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path/'.env'
    initial = b'ROSSKO_ENABLED=1\nSTOCRM_MOCK=0\nROSSKO_KEY1=test-key1\nROSSKO_KEY2=test-key2\nROSSKO_DELIVERY_ID=42\nOTHER=keep\n'
    path.write_bytes(initial)
    state = tmp_path/'.state'; state.mkdir(); (state/'marker').write_text('keep')
    calls = []
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            yield '<Envelope><Body><GetCheckoutDetailsResponse><CheckoutDetailsResult><success>false</success><message>Не указан ключ test-key1 или test-key2</message></CheckoutDetailsResult></GetCheckoutDetailsResponse></Body></Envelope>'.encode()
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(rossko_delivery.requests, 'Session', Session)
    assert rossko_delivery.run(path=path, ask=lambda _: pytest.fail('no user choice expected')) == 1
    out = capsys.readouterr().out
    assert 'R01' in out and 'A1' in out and 'test-key1' not in out and 'test-key2' not in out
    assert path.read_bytes() == initial and (state/'marker').read_text() == 'keep'
    assert len(calls) == 1 and calls[0][0] == rossko_delivery._URL
    assert calls[0][1]['allow_redirects'] is False and calls[0][1]['verify'] is True
    op = next(iter(next(iter(ET.fromstring(calls[0][1]['data'])))))
    assert rossko_delivery._local(op.tag) == 'getcheckoutdetails'
