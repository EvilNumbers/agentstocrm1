"""Явное создание и добавление в существующую быструю сделку."""
import pytest

from stocrm_agent.bot import quick_parse
from stocrm_agent.bot.app import quick_choice_keyboard
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


@pytest.mark.parametrize('text', [
    'Добавь быструю сделку артикул NGK CR9E цена 300',
    'Добавьте быструю сделку артикул NGK CR9E цена 300',
    'Добавить быструю сделку артикул NGK CR9E цена 300',
    'Создай быструю сделку артикул NGK CR9E цена 300',
    'Добавь мне быструю сделку артикул NGK CR9E цена 300',
    'Новая быстрая сделка артикул NGK CR9E цена 300',
])
def test_explicit_creation_ignores_existing_but_needs_confirmation(env, text):
    session, _, pipe = env
    existing(session, 1900); existing(session, 1901)
    result = send(pipe, text)
    assert len(result.drafts) == 1 and not result.choices, result.answer
    assert result.drafts[0]['payload']['offer_id'] is None
    assert 'Создать новую быструю сделку' in result.drafts[0]['summary']
    assert not writes(session)


def test_empty_new_deal_requires_confirmation_then_creates_without_positions(env):
    session, tools, pipe = env
    existing(session, 1900)
    result = send(pipe, 'Добавь быструю сделку')
    assert len(result.drafts) == 1 and 'Без услуг и товаров' in result.drafts[0]['summary'], result.answer
    assert not writes(session)
    applied = tools.apply_draft(result.drafts[0]['token'])
    assert applied['status'] == 'applied' and applied['result']['OFFER_ID'] != 1900
    assert [c['method'] for c in writes(session)] == ['offer/new/with_contact', 'offer/status/change']
    assert writes(session)[0]['payload']['PHONE'] == '81111111110'


@pytest.mark.parametrize('text', [
    'Добавь в быструю сделку артикул NGK CR9E цена 300',
    'Добавьте в быструю сделку артикул NGK CR9E цена 300',
    'Добавить в быструю сделку артикул NGK CR9E цена 300',
])
def test_add_into_existing_requires_choice_and_no_new_option(env, text):
    session, tools, pipe = env
    existing(session, 1900); existing(session, 1901)
    choice = send(pipe, text).choices[0]
    assert set(choice['offers']) == {1900, 1901} and not choice['allow_new']
    assert len(quick_choice_keyboard('x', choice).inline_keyboard) == 2
    assert 'устарел' in pipe.quick_read_choice(choice, 1, 2).answer
    selected = pipe.quick_read_choice(choice, 1, choice['offers'].index(1901))
    assert selected.drafts and selected.drafts[0]['payload']['offer_id'] == 1901
    assert not writes(session)
    result = tools.apply_draft(selected.drafts[0]['token'])
    assert result['status'] == 'applied'
    assert [c['method'] for c in writes(session)] == ['offer/require/add', 'offer/order/add_free_order']
    assert writes(session)[0]['payload']['OFFER_ID'] == 1901


def test_add_into_no_existing_never_creates_new(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь в быструю сделку артикул NGK CR9E цена 300')
    assert not result.drafts and not result.choices and 'не найдено' in result.answer.lower()
    assert not writes(session)


def test_number_in_existing_command_is_used_directly(env):
    session, tools, pipe = env
    existing(session, 1900); existing(session, 1901)
    result = send(pipe, 'Добавь в быструю сделку №1901 артикул NGK CR9E цена 300')
    assert result.drafts and result.drafts[0]['payload']['offer_id'] == 1901, result.answer
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'
    assert writes(session)[0]['payload']['OFFER_ID'] == 1901


def test_new_with_existing_number_is_ambiguous_and_does_not_write(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь быструю сделку №1901 артикул NGK CR9E цена 300')
    assert not result.drafts and not result.choices and 'уточните' in result.answer.lower()
    assert not writes(session)


def test_add_into_without_positions_asks_for_them(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь в быструю сделку')
    assert not result.drafts and 'артикул' in result.answer.lower()
    assert not writes(session)


def test_unqualified_quick_deal_keeps_old_choice(env):
    session, _, pipe = env
    existing(session, 1900)
    choice = send(pipe, 'быстрая сделка диагностика 1000 рублей').choices[0]
    assert choice['allow_new']
    assert len(quick_choice_keyboard('x', choice).inline_keyboard) == 2


def test_parse_does_not_treat_add_to_as_creation():
    plan = quick_parse.parse('Добавь в быструю сделку артикул NGK CR9E цена 300')
    assert not plan['new'] and plan['existing_only']


@pytest.mark.parametrize('text', [
    'Добавь быструю сделку и артикул NGK CR9E цена 300',
    'Добавь быструю сделку с артикулом NGK CR9E цена 300',
    'Добавь быструю сделку и добавь артикул NGK CR9E цена 300',
])
def test_creation_with_parts_has_no_accidental_work(env, text):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, text)
    assert result.drafts and result.drafts[0]['payload']['work'] is None, result.answer
    assert len(result.drafts[0]['payload']['articles']) == 1
    assert result.drafts[0]['payload']['offer_id'] is None
    assert not writes(session)


def test_creation_with_unspecified_parts_does_not_create_empty_deal(env):
    session, _, pipe = env
    result = send(pipe, 'Добавь быструю сделку и запчасти туда')
    assert not result.drafts and not result.choices
    assert 'артикул' in result.answer.lower()
    assert not writes(session)


@pytest.mark.parametrize('text', [
    'Не добавь быструю сделку артикул NGK CR9E цена 300',
    'Не добавь в быструю сделку артикул NGK CR9E цена 300',
    'Не создавай новую быструю сделку артикул NGK CR9E цена 300',
])
def test_negated_command_does_not_prepare_write(env, text):
    session, _, pipe = env
    result = send(pipe, text)
    assert not result.drafts and not result.choices
    assert not writes(session)
