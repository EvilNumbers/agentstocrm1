"""Ожидание оплаты — статус из CRM, а не все неоплаченные сделки."""
import pytest
from test_status_lists_v99 import env, send  # noqa: F401


def setup_waiting(session):
    session.data['offer/all_statuses']['55'] = {'STATUS_ID': 55, 'BOARD_ID': 2, 'TITLE': 'ОЖИДАЕТ ОПЛАТЫ', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2000, 'BOARD_ID': 2, 'STATUS_ID': 55,
        'STATUS_NAME': 'ОЖИДАЕТ ОПЛАТЫ', 'PAYMENT_STATUS': 'Предоплата в сделку', 'OFFER_SUM': 100})
    session.data['offers/get_from_filter'].append({'OFFER_ID': 2001, 'BOARD_ID': 2, 'STATUS_ID': 41,
        'STATUS_NAME': 'Запись', 'PAYMENT_STATUS': 'Нет оплаты', 'OFFER_SUM': 100})


@pytest.mark.parametrize('message', [
    'Покажи сделки в ожидании оплаты',
    'Покажи сделки которые ожидают оплаты',
    'сделки ожидают оплаты',
    'покажите сделки ожидающие оплаты',
    'покажи сделки в ожидании оплаты в воронке Автосервис',
])
def test_waiting_payment_is_exact_status_not_generic_unpaid(env, message):
    session, _, pipe = env
    setup_waiting(session)
    result = send(pipe, message)
    assert '№2000' in result.answer and '№2001' not in result.answer
    assert 'ОЖИДАЕТ ОПЛАТЫ' in result.answer and not result.drafts and not result.applied
    call = next(c for c in session.calls if c['method'] == 'offers/get_from_filter')
    assert call['payload']['FILTER'] == {'BOARD_ID': 2}
    assert all(c['method'] in {'boards/get_all', 'offer/all_statuses', 'offers/get_from_filter'} for c in session.calls)


def test_multiple_wait_status_names_request_clarification(env):
    session, _, pipe = env
    setup_waiting(session)
    session.data['offer/all_statuses']['56'] = {'STATUS_ID': 56, 'BOARD_ID': 2, 'TITLE': 'Ожидание оплаты', 'ACTIVE': 'Y'}
    answer = send(pipe, 'покажи сделки в ожидании оплаты').answer
    assert 'несколько подходящих статусов' in answer and '№2000' not in answer


def test_missing_status_does_not_use_payment_status(env):
    session, _, pipe = env
    answer = send(pipe, 'покажи сделки которые ожидают оплаты').answer
    assert 'не найден подходящий точный статус' in answer.lower()
    assert not any(c['method'] == 'offers/get_from_filter' for c in session.calls)


@pytest.mark.parametrize('message', ['покажи сделки в ожидании оплаты за сегодня',
                                     'покажи сделки которые ожидают оплаты клиента Иванова',
                                     'покажи сделки в ожидании оплаты и в записи'])
def test_other_conditions_are_not_silently_dropped(env, message):
    session, _, pipe = env
    setup_waiting(session)
    answer = send(pipe, message).answer
    assert 'уточните' in answer.lower() and 'не подменяю' in answer.lower()
    assert not any(c['method'] == 'offers/get_from_filter' for c in session.calls)
