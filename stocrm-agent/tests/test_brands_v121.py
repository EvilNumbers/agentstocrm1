"""Каталог брендов проверяется на распознанном тексте, не на живом STT."""
from collections import defaultdict

import pytest

from stocrm_agent import parts
from stocrm_agent.bot import quick_parse
from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401


def test_all_aliases_survive_normalization_and_every_brand_has_cyrillic_name():
    grouped = defaultdict(set)
    for alias, canonical in parts.BRAND_ALIASES.items():
        grouped[parts.brand_key(alias)].add(canonical)
    assert all(len(values) == 1 for values in grouped.values())
    for canonical in set(parts.BRAND_ALIASES.values()):
        assert any(value == canonical and any('а' <= char.lower() <= 'я' or char.lower() == 'ё' for char in alias)
                   for alias, value in parts.BRAND_ALIASES.items()), canonical


def test_each_alias_recognized_from_text_and_manual_and_explicit():
    for alias, canonical in parts.BRAND_ALIASES.items():
        assert parts.find_brand_in_text(f'товар {alias} AB1234') == canonical, alias
        assert parts.explicit_brand_in_text(f'бренд {alias} артикул AB1234') == canonical, alias
        assert parts.manual_brand(alias) == canonical, alias


@pytest.mark.parametrize(('spoken', 'brand'), [
    ('Zimmerman', 'ZIMMERMANN'), ('циммерман', 'ZIMMERMANN'), ('зиммерманн', 'ZIMMERMANN'),
    ('отто циммерман', 'ZIMMERMANN'), ('петронас', 'PETRONAS'), ('зиккерман', 'ZIKKERMAN'),
    ('зикмар', 'ZIKMAR'), ('кольбеншмидт', 'KOLBENSCHMIDT'), ('феродо', 'FERODO'),
    ('френкит', 'FRENKIT'), ('лузар', 'LUZAR'), ('белмаг', 'BELMAG'), ('си эс двадцать', 'CS20'),
    ('си воркс', 'CWORKS'), ('петро канада', 'PETRO-CANADA'), ('ZF', 'ZF'), ('зф', 'ZF'),
])
def test_examples(spoken, brand):
    assert parts.find_brand_in_text(f'товар {spoken} AB1234') == brand
    assert parts.manual_brand(spoken) == brand


def test_distinct_similar_brands_do_not_merge():
    for left, right in [('ZIMMERMANN', 'ZIKKERMAN'), ('ZIKMAR', 'ZIKKERMAN'),
                        ('PETRONAS', 'PETRO-CANADA'), ('FEBI', 'FEBEST'), ('GTR', 'CTR')]:
        assert not parts.exact_brand_match(left, right)
    assert parts.canonical_brand('ZIMMERMANNUNKNOWN') == 'ZIMMERMANNUNKNOWN'


def test_vin_context_not_a_brand():
    assert parts.find_brand_in_text('найди запчасти по вин коду') is None
    assert parts.find_brand_in_text('найди по вин') is None
    assert parts.find_brand_in_text('свеча вин AB1234') == 'WEEN'
    assert parts.find_brand_in_text('колодки без АБС AB1234') is None


def test_alphanumeric_brand_not_misread_as_sku():
    assert [x[0] for x in parts.sku_spans('бренд BESF1TS артикул AB1234 за 300')] == ['AB1234']
    assert [x[0] for x in parts.sku_spans('товар CS20 AB1234 за 300')] == ['AB1234']
    assert [x[0] for x in parts.sku_spans('артикул CS20 за 300')] == ['CS20']
    assert not parts.sku_spans('товар BESF1TS за 300')


def test_two_new_russian_brands_in_one_draft(env):
    session, tools, pipe = env
    existing(session, 1900)
    text = 'в сделку 1900 добавь услугу диагностика 500 и товар циммерман 100.3300.20 за 700 и товар петронас AB1234 за 800'
    result = send(pipe, text)
    assert len(result.drafts) == 1 and not writes(session), result.answer
    assert [(p['brand'], p['sku'], p['price']) for p in result.drafts[0]['payload']['articles']] == [
        ('ZIMMERMANN', '100.3300.20', 700), ('PETRONAS', 'AB1234', 800)]
    assert tools.apply_draft(result.drafts[0]['token'])['status'] == 'applied'
    assert [c['payload']['BRAND'] for c in writes(session) if c['method'] == 'offer/order/add_free_order'] == ['ZIMMERMANN', 'PETRONAS']


def test_no_substring_false_positive():
    assert parts.find_brand_in_text('патронник AB1234') is None
    assert parts.find_brand_in_text('петронасик AB1234') is None


def test_multiple_alphanumeric_brands_keep_their_skus(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и товар CS20 AB1234 за 300 и товар BESF1TS CD5678 за 400')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    assert [(x['sku'], x['brand']) for x in result.drafts[0]['payload']['articles']] == [('AB1234', 'CS20'), ('CD5678', 'BESFITS')]


def test_numeric_brand_only_in_brand_context():
    assert parts.find_brand_in_text('товар 555 AB1234') == '555'
    assert parts.find_brand_in_text('товар AB1234 за 555') is None
    assert parts.find_brand_in_text('услуга диагностика 555') is None
    assert parts.explicit_brand_in_text('бренд 555 артикул AB1234') == '555'
    assert parts.explicit_brand_in_text('бренд 555ABC артикул AB1234') is None
    assert parts.manual_brand('три пятёрки') == '555'


def test_numeric_brand_in_composite(env):
    session, _, pipe = env
    existing(session, 1900)
    result = send(pipe, 'в сделку 1900 добавь услугу диагностика 500 и товар 555 AB1234 за 300')
    assert len(result.drafts) == 1 and not writes(session), result.answer
    assert result.drafts[0]['payload']['articles'][0]['brand'] == '555'
