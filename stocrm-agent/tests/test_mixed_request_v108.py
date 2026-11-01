"""Смешанная просьба: обе позиции или ничего, без живой CRM."""
import pytest
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize('text', [
    'сделка 1900 диагностика 1000 рублей и добавь артикул NGK CR9E цена 300',
    'Сделка 1900 добавь диагностика 1000 рублей и добавь артикул NGK CR9E цена 300',
    'добавь услугу диагностика за 1000 и запчасть NGK CR9E за 300 в сделку 1900',
    'добавь в сделку №1900 услугу диагностика 1000 и товар артикул NGK CR9E цена 300',
    'добавь товар артикул NGK CR9E за 300 и услугу диагностика 1000 в сделку 1900',
])
@pytest.mark.parametrize('fastpath', [True, False])
def test_mixed_ordinary_deal_prepares_both_without_writing(env, text, fastpath):
    session, _, pipe = env
    pipe.fastpath = fastpath
    existing(session, 1900)
    result = send(pipe, text)
    assert len(result.drafts) == 1 and not result.applied and not writes(session), result.answer
    p = result.drafts[0]['payload']
    assert p['offer_id'] == 1900
    assert p['work'] == {'title': 'диагностика', 'price': 1000}
    assert p['articles'] == [{'sku': 'CR9E', 'brand': 'NGK', 'qty': 1, 'price': 300}]
