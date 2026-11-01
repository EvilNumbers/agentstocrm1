"""Запросы разных разделов при ответе CRM без STATUS_ID."""
import copy

import pytest

from stocrm_agent.mock import MockResponse
from test_status_lists_v99 import env, send  # noqa: F401


@pytest.mark.parametrize(('message', 'expected_id', 'excluded_id'), [
    ('покажи сделки в записи', 1328, 1327),
    ('покажи сделки в заказе', 1327, 1328),
    ('покажи сделки в просчете', 1712, 1328),
    ('покажи собранные заказы', 1326, 1327),
])
def test_sections_use_name_only_when_id_absent(env, monkeypatch, message, expected_id, excluded_id):
    session, _, pipe = env
    for row in session.data['offers/get_from_filter']:
        if row['OFFER_ID'] == 1712:
            row['STATUS_NAME'] = 'Просчёт запчастей'
    original = session.post
    def post(url, json=None, timeout=None):
        response = original(url, json=json, timeout=timeout)
        if not url.endswith('/offers/get_from_filter'):
            return response
        body = copy.deepcopy(response.json())
        if isinstance(body.get('RESPONSE'), list):
            for row in body['RESPONSE']:
                row.pop('STATUS_ID', None)
        return MockResponse(body)
    monkeypatch.setattr(session, 'post', post)
    answer = send(pipe, message).answer
    assert f'№{expected_id}' in answer and f'№{excluded_id}' not in answer
    assert 'по названию' in answer
