"""Операции записи на сервис используют название статуса сделки, не её STATUS_ID."""
import pytest

from test_client_booking import env  # noqa: F401


@pytest.mark.parametrize('status_id', [None, 999])
def test_booking_offer_derives_status_from_name_and_directory(env, status_id):
    session, tools, _ = env
    session.data['offer/all_statuses']['41'] = {'STATUS_ID': 41, 'BOARD_ID': 2, 'TITLE': 'Запись', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'][0].update(STATUS_ID=status_id, STATUS_NAME='Запись')
    offer = tools._booking_offer(1654)
    assert offer['status'] == 'Запись' and offer['status_id'] == 41


def test_booking_ambiguous_name_is_not_resolved_by_row_id(env):
    session, tools, _ = env
    session.data['offer/all_statuses']['41'] = {'STATUS_ID': 41, 'BOARD_ID': 2, 'TITLE': 'Запись', 'ACTIVE': 'Y'}
    session.data['offer/all_statuses']['42'] = {'STATUS_ID': 42, 'BOARD_ID': 2, 'TITLE': 'Запись', 'ACTIVE': 'N'}
    session.data['offers/get_from_filter'][0].update(STATUS_ID=41, STATUS_NAME='Запись')
    assert tools._booking_offer(1654)['status_id'] is None
