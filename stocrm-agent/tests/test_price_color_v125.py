"""Регрессии цены артикула и лишней легенды, без настоящей CRM/Telegram."""
import pytest

from stocrm_agent import parts
from stocrm_agent.bot import quick_parse
from stocrm_agent.bot.pipeline import _single_part_in_request, parts_in_request
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize('amount,expected', [('700', 700), ('1200', 1200), ('700,50', 700.5), ('5', 5)])
def test_screenshot_quick_command_keeps_work_and_article_prices(env, amount, expected):
    session, _, pipe = env
    result = send(pipe, f'Добавь быструю сделку диагностика 950 и артикул fk-2005 {amount}')
    assert not result.drafts and not writes(session)
    assert 'бренд' in result.answer.lower(), result.answer
    result = send(pipe, 'ZEKKERT')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    payload = result.drafts[0]['payload']
    assert payload['work']['price'] == 950
    assert payload['articles'][0]['sku'] == 'FK-2005'
    assert payload['articles'][0]['price'] == expected
    assert payload['articles'][0]['brand'] == 'ZEKKERT'


def test_existing_mixed_keeps_each_bare_price(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 950 и артикул NGK CR9E 700 и товар MANN W914/2 1200')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    payload = result.drafts[0]['payload']
    assert payload['work']['price'] == 950
    assert [(a['sku'], a['brand'], a['price']) for a in payload['articles']] == [('CR9E', 'NGK', 700), ('W914/2', 'MANN', 1200)]


def test_plain_article_paths_support_bare_prices():
    assert _single_part_in_request('добавь в сделку 1900 артикул NGK CR9E 1200')['price'] == 1200
    assert _single_part_in_request('добавь в сделку 1900 артикул NGK CR9E 5')['price'] == 5
    assert _single_part_in_request('добавь в сделку 1900 артикул NGK CR9E за 2 штуки')['price'] is None
    items = parts_in_request('добавь в сделку 1900 артикулы NGK CR9E 700 и MANN W914/2 1200')
    assert [(x['sku'], x['price']) for x in items] == [('CR9E', 700), ('W914/2', 1200)]


@pytest.mark.parametrize('tail', ['2 шт', '2 штуки', '2 комплекта', '0', ''])
def test_quantity_or_zero_is_never_silently_taken_as_a_price(tail):
    plan = quick_parse.parse(f'новая быстрая сделка диагностика 950 и артикул NGK CR9E {tail}')
    assert plan['articles'][0]['price'] is None


def test_quantity_and_price_are_separate():
    plan = quick_parse.parse('новая быстрая сделка диагностика 950 и артикул NGK CR9E 700 2 шт')
    assert plan['articles'][0]['price'] == 700 and plan['articles'][0]['qty'] == 2


def test_numeric_sku_with_bare_number_remains_ambiguous():
    with pytest.raises(ValueError, match='числа'):
        quick_parse.parse('новая быстрая сделка диагностика 950 и артикул 123456 700')
    assert parts.mark_adjacent_sku_prices('артикул 123456 700') == 'артикул 123456 700'


def test_missing_price_still_requests_zero_choice(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь быструю сделку диагностика 950 и артикул NGK CR9E')
    assert not result.drafts and not writes(session)
    assert result.choices and '0 ₽' in result.choices[0]['question']
