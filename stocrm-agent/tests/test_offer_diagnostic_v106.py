"""Диагностика не раскрывает данные и не меняет правила записи."""
import pytest

from stocrm_agent.bot import quick_read
from test_quick_deal_v100 import env, writes  # noqa: F401


@pytest.mark.parametrize(('value', 'shape', 'parsed'), [
    (None, 'null', None),
    (True, 'логическое значение', None),
    ('', 'пустая строка', None),
    (' 19 ', 'числовая строка с пробелами', None),
    ('private', 'другая строка', None),
    ('19', 'числовая строка', 19),
    (19, 'целое число', 19),
    (19.0, 'число с десятичной точкой', None),
    ({'secret': 'private'}, 'другой тип', None),
])
def test_diagnostic_describes_type_without_private_values(env, monkeypatch, value, shape, parsed):
    session, tools, _ = env
    row = {'OFFER_ID': 1336, 'CONTACT_ID': 5555, 'BOARD_ID': 2,
           'STATUS_ID': value, 'STATUS_NAME': 'Быстрые сделки', 'SECRET': 'private'}
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: (row, 1))
    result = tools.offer_identity_diagnostic(1336)
    assert result['status_id_shape'] == shape
    assert result['status_id'] == parsed
    assert result['quick_reference']['name_matches'] is True
    assert 'private' not in str(result) and '5555' not in str(result)
    assert writes(session) == []


def test_missing_field_and_unknown_status_name(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID': 1336, 'BOARD_ID': 2}, 1))
    result = tools.offer_identity_diagnostic(1336)
    assert result['status_id_shape'] == 'поле отсутствует'
    assert result['quick_reference']['name_matches'] is None


def test_mismatch_and_unavailable_directory(env, monkeypatch):
    _, tools, _ = env
    monkeypatch.setattr(tools, '_search_offer_row', lambda _: ({'OFFER_ID': 1336, 'STATUS_NAME': 'private'}, 1))
    result = tools.offer_identity_diagnostic(1336)
    assert result['quick_reference']['name_matches'] is False
    assert 'private' not in str(result)
    monkeypatch.setattr(quick_read, 'status_identity', lambda _: (_ for _ in ()).throw(ValueError('private')))
    assert tools.offer_identity_diagnostic(1336)['quick_reference'] == {'status': 'unavailable'}
