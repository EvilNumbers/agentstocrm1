"""Не выполняем только товар, молча отбрасывая услугу из одной просьбы."""
import pytest
from test_quick_deal_v100 import env, send, writes  # noqa: F401


@pytest.mark.parametrize('text', [
    'сделка 1337 диагностика 1000 рублей и добавь артикул NGK CR9E цена 300',
    'Сделка 1337 добавь диагностика 1000 рублей и добавь артикул NGK CR9E цена 300',
    'добавь услугу диагностика за 1000 и запчасть NGK CR9E за 300 в сделку 1337',
    'добавь работу замена масла 1000 и товар артикул W914/2 цена 300 в сделку 1337',
])
@pytest.mark.parametrize('fastpath', [True, False])
def test_mixed_ordinary_deal_never_silently_omits_work(env, text, fastpath):
    session, _, pipe = env
    pipe.fastpath = fastpath
    result = send(pipe, text)
    assert 'услугу' in result.answer and 'товар' in result.answer
    assert 'отдельно' in result.answer and 'не записано' in result.answer
    assert not result.drafts and not result.applied and not writes(session)
